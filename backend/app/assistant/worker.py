"""Execute durable runs under the assistant-only PostgreSQL identity."""

import asyncio
import contextlib
import json
import os
import signal
import socket
from datetime import UTC, datetime
from uuid import uuid4

from app.admin.directory import active_account
from app.agents.builders import build_agent
from app.agents.runtime import get_runtime_config
from app.assistant.events import _chunk, _serialize_events, _serialize_run
from app.assistant.queue import RunLeaseLost, append_event, claim_run, finish_run, renew_run
from app.auth.jwt import verify_access_token
from app.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.session import engine, get_session_factory, validate_runtime_database
from app.logging import configure_logging, get_logger
from app.observability import llm_turn, new_trace_id
from app.observability.turns import TurnObservation
from app.workspace.client import trusted_workspace_token

logger = get_logger(__name__)


async def _execute(run, owner, abort):
    sessions = get_session_factory("assistant")
    session_id = run.payload.get("agno_session_id") or run.session_id
    observation = TurnObservation(
        trace_id=new_trace_id(),
        user_id=str(run.user_id),
        session_id=session_id,
        kind="confirmation_turn" if run.kind == "confirmation" else "chat_turn",
    )
    final_status = "completed"
    error = None
    agno_run_id = run.payload.get("run_id")
    model = get_settings().agent_model
    agent = None
    try:
        if run.cancel_requested:
            abort["reason"] = "cancelled"
            raise asyncio.CancelledError
        if run.token_expires_at <= datetime.now(UTC):
            raise ValueError("Authentication expired before execution")
        token = decrypt_secret(run.token_enc) if run.token_enc else ""
        user = verify_access_token(token)
        if user.id != run.user_id:
            raise ValueError("Run identity does not match authenticated token")
        async with sessions() as db:
            if await renew_run(db, run.id, owner):
                abort["reason"] = "cancelled"
                raise asyncio.CancelledError
            if await active_account(db, run.user_id) is None:
                abort["reason"] = "cancelled"
                raise asyncio.CancelledError
            runtime = await get_runtime_config(db)
        agent = build_agent(run.user_id, runtime)
        model = runtime.model_id
        approval = decrypt_secret(run.approval_token_enc) if run.approval_token_enc else None
        with trusted_workspace_token(token, approval_token=approval), llm_turn(observation.trace_id, run.session_id):
            if run.kind == "confirmation":
                persisted = agent.get_run_output(run.payload["run_id"], session_id=session_id, user_id=str(run.user_id))
                if persisted is None or not persisted.is_paused:
                    raise ValueError("The persisted run is no longer awaiting confirmation")
                requirements = [item for item in persisted.active_requirements if item.needs_confirmation]
                if len(requirements) != 1 or requirements[0].id != run.payload["requirement_id"]:
                    raise ValueError("The exact confirmation is no longer pending")
                if run.payload["approved"]:
                    requirements[0].confirm()
                else:
                    from app.agents.scholar.hooks import record_confirmation_rejection

                    execution = requirements[0].tool_execution
                    await record_confirmation_rejection(
                        user_id=str(run.user_id),
                        session_id=session_id,
                        run_id=run.payload["run_id"],
                        tool_name=execution.tool_name,
                        arguments=execution.tool_args or {},
                    )
                    requirements[0].reject(note="Rejected by the student")
                events = agent.acontinue_run(
                    run_id=run.payload["run_id"],
                    requirements=persisted.requirements,
                    session_id=session_id,
                    user_id=str(run.user_id),
                    dependencies=run.payload.get("dependencies", {}),
                    stream=True,
                    stream_events=True,
                )
                source = _serialize_events(events, model, str(run.user_id), observation)
            else:
                source = _serialize_run(
                    agent,
                    run.payload["text"],
                    session_id,
                    str(run.user_id),
                    model,
                    run.payload.get("dependencies", {}),
                    observation,
                )
            async with contextlib.aclosing(source):
                async for chunk in source:
                    if chunk.startswith(b"data: {"):
                        extension = json.loads(chunk.decode().removeprefix("data: ")).get("devrimo", {})
                        if extension.get("type") == "confirmation_required":
                            agno_run_id = extension.get("run_id")
                    async with sessions() as db:
                        await append_event(db, run.id, owner, chunk)
            final_status = "failed" if observation.failed else "paused" if observation.paused else "completed"
    except RunLeaseLost:
        raise
    except asyncio.CancelledError:
        error = abort.get("reason") or "worker_stopped"
        final_status = "cancelled" if error == "cancelled" else "interrupted"
        observation.cancelled(error)
        async with sessions() as db:
            await append_event(
                db,
                run.id,
                owner,
                _chunk(
                    model,
                    extension={
                        "type": "error",
                        "code": error,
                        "message": "Run stopped; external actions will not be replayed automatically.",
                    },
                    finish="stop",
                ),
            )
    except Exception as exc:
        final_status = "failed"
        error = type(exc).__name__
        observation.stream_failed(exc)
        logger.warning("assistant_run_failed", run_id=str(run.id), error_type=error)
        async with sessions() as db:
            await append_event(
                db,
                run.id,
                owner,
                _chunk(
                    model,
                    extension={
                        "type": "error",
                        "code": "run_failed",
                        "message": "The assistant could not complete this run.",
                    },
                    finish="stop",
                ),
            )
    finally:
        observation.finish()
        # Each durable run builds an isolated model instance. Release its HTTP
        # pool instead of accumulating idle sockets across worker jobs.
        if agent is not None:
            client = getattr(getattr(agent, "model", None), "async_client", None)
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
    async with sessions() as db:
        await finish_run(db, run.id, owner, final_status, error_code=error, agno_run_id=agno_run_id)


async def run_one(owner: str | None = None) -> bool:
    owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    sessions = get_session_factory("assistant")
    async with sessions() as db:
        run = await claim_run(db, owner)
    if run is None:
        return False
    abort = {}
    execution = asyncio.create_task(_execute(run, owner, abort))

    async def heartbeat():
        while True:
            await asyncio.sleep(2)
            async with sessions() as db:
                if await renew_run(db, run.id, owner):
                    abort["reason"] = "cancelled"
                    return
            if run.token_expires_at <= datetime.now(UTC):
                abort["reason"] = "authentication_expired"
                return

    renewal = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
        if renewal in done:
            await renewal
            execution.cancel()
        await execution
    finally:
        renewal.cancel()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, renewal, return_exceptions=True)
    return True


async def main():
    from app.observability.client import initialize, shutdown
    from app.observability.logs import shutdown as shutdown_logs
    from app.observability.runtime import configure_service

    configure_service("devrimo-assistant-worker")
    configure_logging()
    await validate_runtime_database("assistant")
    if not get_settings().workspace_gateway_url:
        raise RuntimeError("WORKSPACE_GATEWAY_URL is required for the assistant worker")
    initialize()
    if get_settings().agent_tracing_enabled:
        from agno.tracing import setup_tracing

        from app.agents.store import get_agno_db

        setup_tracing(db=get_agno_db(), batch_processing=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def consume():
        while not stop.is_set():
            try:
                if not await run_one():
                    await asyncio.sleep(0.5)
            except Exception as exc:
                logger.warning("assistant_worker_iteration_failed", error_type=type(exc).__name__)
                await asyncio.sleep(1)

    tasks = [asyncio.create_task(consume()) for _ in range(get_settings().assistant_worker_concurrency)]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        await asyncio.to_thread(shutdown)
        await asyncio.to_thread(shutdown_logs)


if __name__ == "__main__":
    asyncio.run(main())
