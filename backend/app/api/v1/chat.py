"""The hot path: run one turn on the user's Agno agent and stream it back.

Under Hermes this route was a byte proxy — it forwarded an upstream SSE stream
untouched, which meant the only thing the frontend could ever render was text.
Now the broker owns the loop, so this module owns the wire format, and the
translation from Agno's run events to OpenAI ``chat.completion.chunk`` objects
happens here.

That translation is deliberately additive. ``frontend/lib/api/chat.ts`` reads
``choices[0].delta.content`` and ignores everything else, so tool activity is
emitted as ordinary chunks carrying an empty delta plus a ``devrimo`` extension
object. Today's frontend skips them; the assistant-ui tool components can be
wired to them without changing anything on this side.

Message history is *not* taken from the request body. The client re-sends the
whole thread on every turn, but Agno reloads it from the database against
``session_id`` — replaying the body as well would double every prior turn in
the model's context. Only the newest user message is passed in.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import manager
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.config import get_settings
from app.db.models import ChatSession
from app.db.session import SessionLocal, get_db
from app.logging import get_logger
from app.observability import llm_turn, new_trace_id
from app.observability.client import capture, report_exception
from app.observability.turns import TurnObservation
from app.schemas import ChatCompletionsRequestIn, ChatConfirmationIn

router = APIRouter()
logger = get_logger(__name__)

KEEPALIVE_SECONDS = 15

# Turns whose stream was abandoned still have to release a lock and report an
# outcome, and the task doing that must outlive the request it came from. A
# strong reference is kept because asyncio only holds a weak one, and a
# garbage-collected cleanup task is a turn lock held until the lease expires.
_DETACHED_CLEANUPS: set[asyncio.Task] = set()


def _reject(reason: str, user_id, *, kind: str, **properties) -> None:
    """Record a turn the broker declined to start.

    A busy agent and an empty message are the product working as designed, so
    they are events rather than issues — but they were previously nothing at
    all, which made "how often does a student hit a busy agent?" unanswerable.
    """
    capture(
        "chat_turn_rejected",
        distinct_id=str(user_id),
        reason=reason,
        turn_kind=kind,
        **properties,
    )


class _TurnCleanup:
    """Report a turn and release its resources exactly once.

    Both halves used to live in the streaming generator's ``finally``, after a
    ``yield``. That ordering has a failure mode the audit found: when a student
    closes the tab, the ``yield`` raises, and the turn is never reported and the
    heartbeat is never stopped. An ``await`` in that same ``finally`` is no
    safer, because the enclosing task is already being cancelled.

    So reporting is synchronous and happens first — it cannot raise, and a turn
    that is not observed is worse than a lock released a moment later — and the
    asynchronous release runs in its own task, which the normal path awaits and
    the abandoned path simply lets finish.
    """

    def __init__(self, observation, heartbeat, heartbeat_stop, finalize, user_id) -> None:
        self.observation = observation
        self.heartbeat = heartbeat
        self.heartbeat_stop = heartbeat_stop
        self.finalize = finalize
        self.user_id = str(user_id)
        self._started = False

    def _begin(self) -> bool:
        if self._started:
            return False
        self._started = True
        self.heartbeat_stop.set()
        self.observation.finish()
        return True

    async def run(self) -> None:
        """The normal path: wait for the release, but never block on it forever."""
        if not self._begin():
            return
        # Shielded, so a disconnect arriving mid-cleanup cancels the waiting,
        # not the cleanup: the turn lock is released either way.
        await asyncio.shield(self._spawn())

    def detach(self) -> None:
        """The abandoned path: nothing here may await, so hand it to a task."""
        if not self._begin():
            return
        self._spawn()

    def _spawn(self) -> asyncio.Task:
        task = asyncio.create_task(self._release())
        _DETACHED_CLEANUPS.add(task)
        task.add_done_callback(_DETACHED_CLEANUPS.discard)
        return task

    async def _release(self) -> None:
        try:
            await self.heartbeat
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # A heartbeat that died mid-turn means the lease was not renewed.
            # It used to propagate out of the ``finally`` and take the rest of
            # the cleanup with it.
            logger.warning("turn_heartbeat_failed", user_id=self.user_id, error=str(exc))
            report_exception(exc, distinct_id=self.user_id, handler="turn_heartbeat")
        try:
            await self.finalize()
        except Exception as exc:
            logger.error("turn_finalize_failed", user_id=self.user_id, error=str(exc))
            report_exception(exc, distinct_id=self.user_id, handler="turn_finalize")


def _chunk(model: str, *, delta: dict | None = None, extension: dict | None = None, finish: str | None = None) -> bytes:
    payload: dict = {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if extension is not None:
        # Namespaced so it can never be mistaken for an OpenAI field, and so a
        # client that doesn't know about it simply sees an empty delta.
        payload["devrimo"] = extension
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _tool_name(event) -> str | None:
    tool = getattr(event, "tool", None)
    if tool is None:
        return None
    return getattr(tool, "tool_name", None) or getattr(tool, "name", None)


def _tool_server(tool_name: str | None) -> str | None:
    """The campus server a prefixed tool belongs to (``sais_get_transcript`` -> ``sais``)."""
    if not tool_name:
        return None
    for server in ("course_info", "odtuclass", "webmail", "sais"):
        if tool_name.startswith(f"{server}_"):
            return server
    return None


def _tool_error_detail(event) -> str:
    """The message from a ToolCallErrorEvent.

    ``ToolExecution.tool_call_error`` is a flag, not a message — the text lives
    on the event's ``error`` field, falling back to the tool's own result.
    """
    detail = getattr(event, "error", None)
    if not detail:
        tool = getattr(event, "tool", None)
        detail = getattr(tool, "result", None) if tool is not None else None
    if not detail:
        detail = getattr(event, "content", None)
    return str(detail) if detail else "The tool call failed."


def _confirmation_payload(event) -> list[dict]:
    requirements = []
    for requirement in getattr(event, "active_requirements", []) or []:
        if not getattr(requirement, "needs_confirmation", False):
            continue
        execution = getattr(requirement, "tool_execution", None)
        requirements.append(
            {
                "id": requirement.id,
                "tool": getattr(execution, "tool_name", None),
                "arguments": getattr(execution, "tool_args", None) or {},
            }
        )
    return requirements


async def _serialize_events(
    events,
    model: str,
    user_id: str,
    observation: TurnObservation,
) -> AsyncIterator[bytes]:
    """Agno run events -> OpenAI-compatible SSE, observed as one PostHog trace."""
    from agno.run.agent import RunEvent

    async for event in events:
        name = getattr(event, "event", None)

        if name == RunEvent.run_content.value:
            content = getattr(event, "content", None)
            if isinstance(content, str) and content:
                yield _chunk(model, delta={"role": "assistant", "content": content})

        elif name == RunEvent.tool_call_started.value:
            tool = _tool_name(event)
            observation.tool_started(tool)
            yield _chunk(model, extension={"type": "tool_call_started", "tool": tool, "server": _tool_server(tool)})

        elif name == RunEvent.tool_call_completed.value:
            tool = _tool_name(event)
            yield _chunk(model, extension={"type": "tool_call_completed", "tool": tool, "server": _tool_server(tool)})

        elif name == RunEvent.tool_call_error.value:
            # Previously unhandled: Agno emitted this and the broker dropped it,
            # so a failed tool reached neither the student nor any log. The
            # agent may still recover on its own, so this is reported without
            # ending the turn.
            tool = _tool_name(event)
            detail = _tool_error_detail(event)
            logger.warning("agent_tool_call_error", user_id=user_id, tool=tool, detail=detail)
            observation.tool_failed(tool, detail)
            yield _chunk(
                model,
                extension={"type": "tool_call_error", "tool": tool, "server": _tool_server(tool), "message": detail},
            )

        elif name == RunEvent.run_paused.value:
            observation.paused = True
            yield _chunk(
                model,
                extension={
                    "type": "confirmation_required",
                    "run_id": getattr(event, "run_id", None),
                    "session_id": getattr(event, "session_id", None),
                    "requirements": _confirmation_payload(event),
                },
            )

        elif name == RunEvent.run_completed.value:
            # Token counts, cost and time-to-first-token for the whole turn,
            # broken down per model role.
            observation.metrics = getattr(event, "metrics", None)

        elif name == RunEvent.run_cancelled.value:
            observation.cancelled("run_cancelled")

        elif name == RunEvent.run_error.value:
            detail = getattr(event, "content", None) or "The agent could not complete this turn."
            error_type = getattr(event, "error_type", None)
            logger.error("agent_run_error", user_id=user_id, detail=str(detail), error_type=error_type)
            observation.run_failed(str(detail), error_type)
            yield _chunk(
                model,
                extension={"type": "error", "code": error_type or "run_error", "message": str(detail)},
                finish="stop",
            )
            return

    yield _chunk(model, delta={}, finish="stop")


async def _serialize_run(
    agno_agent,
    text: str,
    session_id: str,
    user_id: str,
    model: str,
    dependencies: dict,
    observation: TurnObservation,
) -> AsyncIterator[bytes]:
    stream = agno_agent.arun(
        input=text,
        session_id=session_id,
        user_id=user_id,
        dependencies=dependencies,
        stream=True,
        stream_events=True,
    )
    async for chunk in _serialize_events(stream, model, user_id, observation):
        yield chunk


async def _with_keepalive(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Interleave SSE comments so no proxy in the path times out a slow tool call.

    Uses a queue so ``asyncio.wait_for`` only ever cancels a ``queue.get()``,
    never the source generator — cancelling an async generator's ``__anext__``
    throws ``CancelledError`` into it and finalises it, silently killing the
    stream mid-tool-call.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _drain() -> None:
        try:
            async for chunk in source:
                await queue.put(chunk)
        finally:
            await queue.put(None)  # sentinel

    task = asyncio.create_task(_drain())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if chunk is None:
                return
            yield chunk
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _get_or_create_chat_session(
    db: AsyncSession, user_id, agent_id, client_session_id: str | None
) -> ChatSession:
    if client_session_id:
        result = await db.execute(select(ChatSession).where(ChatSession.id == client_session_id))
        existing = result.scalar_one_or_none()
        if existing is not None:
            if existing.user_id != user_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat session not found")
            if existing.deleted_at is not None:
                existing.deleted_at = None
            return existing

    session_id = client_session_id or str(uuid4())
    session = ChatSession(id=session_id, user_id=user_id, agent_id=agent_id, agno_session_id=session_id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/completions")
async def chat_completions(
    body: ChatCompletionsRequestIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    agent = await manager.get_or_create_agent(db, user.id)

    latest_user_message = next(
        (m.content for m in reversed(body.messages) if m.role == "user"),
        None,
    )
    if not latest_user_message:
        _reject("empty_message", user.id, kind="chat_turn")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No user message to respond to")

    lock_owner = str(uuid4())
    if not await manager.acquire_turn_lock(db, agent, lock_owner):
        _reject("agent_busy", user.id, kind="chat_turn", chat_session_id=body.session_id)
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is busy with another message")

    lease = None
    try:
        chat_session = await _get_or_create_chat_session(db, user.id, agent.id, body.session_id)
        lease = await manager.lease_for(db, agent)
        from app.agents.scholar.context import build_run_dependencies

        dependencies = await build_run_dependencies(db, user.id, lease.resident)
    except HTTPException as exc:
        # A session that belongs to someone else, for instance: expected, and
        # the turn still never started.
        _reject("setup_rejected", user.id, kind="chat_turn", status_code=exc.status_code)
        if lease is not None:
            await lease.release()
        await manager.release_turn_lock(db, agent, lock_owner)
        raise
    except Exception as exc:
        # Building the run context reaches the database and the campus layer.
        # This path released the lock correctly and reported nothing at all.
        logger.error("chat_turn_setup_failed", user_id=str(user.id), error=str(exc))
        report_exception(exc, distinct_id=str(user.id), handler="chat_turn_setup")
        _reject("setup_failed", user.id, kind="chat_turn", error_type=exc.__class__.__name__)
        if lease is not None:
            await lease.release()
        await manager.release_turn_lock(db, agent, lock_owner)
        raise

    model = get_settings().agent_model
    agno_agent = lease.agent
    agno_session_id = chat_session.agno_session_id or chat_session.id
    chat_session_id = chat_session.id
    user_id = user.id

    trace_id = new_trace_id()

    async def stream() -> AsyncIterator[bytes]:
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(manager.turn_lock_heartbeat(agent.id, lock_owner, heartbeat_stop))
        observation = TurnObservation(
            trace_id=trace_id,
            user_id=str(user_id),
            session_id=chat_session_id,
            kind="chat_turn",
        )
        cleanup = _TurnCleanup(
            observation,
            heartbeat,
            heartbeat_stop,
            lambda: _finalize_turn(user_id, chat_session_id, lock_owner, latest_user_message, lease=lease),
            user_id,
        )
        # Scopes every model call Agno makes for this turn — the answer, the
        # tool-loop follow-ups, compression and learning — into one trace.
        try:
            with llm_turn(trace_id, chat_session_id):
                try:
                    source = _serialize_run(
                        agno_agent,
                        latest_user_message,
                        agno_session_id,
                        str(user_id),
                        model,
                        dependencies,
                        observation,
                    )
                    async for chunk in _with_keepalive(source):
                        yield chunk
                except Exception as exc:  # never let a broken stream leave the lock held
                    logger.error("chat_stream_failed", user_id=str(user_id), error=str(exc))
                    observation.stream_failed(exc)
                    yield _chunk(
                        model,
                        extension={"type": "error", "code": "stream_failed", "message": "Chat stream failed"},
                        finish="stop",
                    )
                yield b"data: [DONE]\n\n"
                # Inside the trace scope, and before the generator can be closed
                # by anything downstream, so the turn is reported even when the
                # response body never reaches the student.
                await cleanup.run()
        except (asyncio.CancelledError, GeneratorExit):
            # The student closed the tab or the connection dropped. Neither is
            # an error, and both used to end the turn reporting nothing.
            observation.cancelled("client_disconnected")
            cleanup.detach()
            raise
        finally:
            cleanup.detach()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.post("/confirmations")
async def confirm_tool_call(
    body: ChatConfirmationIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Approve or reject an Agno-paused tool call, scoped to its owner and session."""
    agent = await manager.get_or_create_agent(db, user.id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == body.session_id,
            ChatSession.user_id == user.id,
            ChatSession.deleted_at.is_(None),
        )
    )
    chat_session = result.scalar_one_or_none()
    if chat_session is None:
        _reject("session_not_found", user.id, kind="confirmation_turn", chat_session_id=body.session_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat session not found")

    lock_owner = str(uuid4())
    if not await manager.acquire_turn_lock(db, agent, lock_owner):
        _reject("agent_busy", user.id, kind="confirmation_turn", chat_session_id=body.session_id)
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is busy with another message")
    try:
        lease = await manager.lease_for(db, agent)
        agno_agent = lease.agent
        agno_session_id = chat_session.agno_session_id or chat_session.id
        run_output = agno_agent.get_run_output(body.run_id, session_id=agno_session_id, user_id=str(user.id))
        if run_output is None or not run_output.is_paused:
            _reject("paused_run_not_found", user.id, kind="confirmation_turn", chat_session_id=chat_session.id)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Paused run not found")
        active_confirmations = [item for item in run_output.active_requirements if item.needs_confirmation]
        if len(active_confirmations) != 1:
            _reject(
                "batched_actions_blocked",
                user.id,
                kind="confirmation_turn",
                chat_session_id=chat_session.id,
                pending_confirmations=len(active_confirmations),
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Batched external actions are blocked; ask the agent to perform one action at a time",
            )
        requirement = next(
            (item for item in active_confirmations if item.id == body.requirement_id),
            None,
        )
        if requirement is None or not requirement.needs_confirmation:
            _reject("confirmation_not_pending", user.id, kind="confirmation_turn", chat_session_id=chat_session.id)
            raise HTTPException(status.HTTP_409_CONFLICT, "Confirmation is no longer pending")
        if body.approved:
            requirement.confirm()
        else:
            from app.agents.scholar.hooks import record_confirmation_rejection

            execution = requirement.tool_execution
            await record_confirmation_rejection(
                user_id=str(user.id),
                session_id=agno_session_id,
                run_id=body.run_id,
                tool_name=execution.tool_name,
                arguments=execution.tool_args or {},
            )
            requirement.reject(note="Rejected by the student")

        from app.agents.scholar.context import build_run_dependencies

        dependencies = await build_run_dependencies(db, user.id, lease.resident)
    except HTTPException:
        if "lease" in locals():
            await lease.release()
        await manager.release_turn_lock(db, agent, lock_owner)
        raise
    except Exception as exc:
        logger.error("confirmation_setup_failed", user_id=str(user.id), error=str(exc))
        report_exception(exc, distinct_id=str(user.id), handler="confirmation_setup")
        _reject("setup_failed", user.id, kind="confirmation_turn", error_type=exc.__class__.__name__)
        if "lease" in locals():
            await lease.release()
        await manager.release_turn_lock(db, agent, lock_owner)
        raise

    model = get_settings().agent_model

    trace_id = new_trace_id()
    chat_session_id = chat_session.id

    async def stream() -> AsyncIterator[bytes]:
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(manager.turn_lock_heartbeat(agent.id, lock_owner, heartbeat_stop))
        observation = TurnObservation(
            trace_id=trace_id,
            user_id=str(user.id),
            session_id=chat_session_id,
            kind="confirmation_turn",
        )
        cleanup = _TurnCleanup(
            observation,
            heartbeat,
            heartbeat_stop,
            lambda: _finalize_turn(user.id, chat_session_id, lock_owner, None, lease=lease, increment=False),
            user.id,
        )
        try:
            with llm_turn(trace_id, chat_session_id):
                try:
                    events = agno_agent.acontinue_run(
                        # Resume from the persisted run id. Passing the separately
                        # loaded RunOutput here makes Agno treat it as an in-memory
                        # continuation and can skip rebinding the approved tool to its
                        # live callable after a process/request boundary.
                        run_id=body.run_id,
                        requirements=run_output.requirements,
                        stream=True,
                        stream_events=True,
                        user_id=str(user.id),
                        session_id=agno_session_id,
                        dependencies=dependencies,
                    )
                    async for chunk in _with_keepalive(_serialize_events(events, model, str(user.id), observation)):
                        yield chunk
                except Exception as exc:
                    logger.error("confirmation_stream_failed", user_id=str(user.id), error=str(exc))
                    observation.stream_failed(exc)
                    yield _chunk(
                        model,
                        extension={"type": "error", "code": "confirmation_failed", "message": "Confirmation failed"},
                        finish="stop",
                    )
                yield b"data: [DONE]\n\n"
                await cleanup.run()
        except (asyncio.CancelledError, GeneratorExit):
            observation.cancelled("client_disconnected")
            cleanup.detach()
            raise
        finally:
            cleanup.detach()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def _finalize_turn(
    user_id,
    chat_session_id: str,
    lock_owner: str,
    first_user_text: str | None,
    *,
    lease=None,
    increment: bool = True,
) -> None:
    if lease is not None:
        await lease.release()
    async with SessionLocal() as db:
        agent = await manager.get_agent(db, user_id)
        if agent is not None:
            await manager.release_turn_lock(db, agent, lock_owner)
            await manager.touch_last_active(db, agent)

        result = await db.execute(select(ChatSession).where(ChatSession.id == chat_session_id))
        session_row = result.scalar_one_or_none()
        if session_row is not None:
            if increment:
                session_row.message_count += 1
            if first_user_text and not session_row.title:
                session_row.title = first_user_text[:80] or None
            await db.commit()
