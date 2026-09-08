from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.admin.directory import touch_account
from app.agents.manager import get_or_create_agent
from app.assistant.models import AssistantRun, AssistantRunEvent
from app.assistant.queue import (
    RunLeaseLost,
    append_event,
    claim_run,
    enqueue_run,
    get_owned_run,
    request_cancel,
    stream_events,
)
from app.assistant.worker import run_one
from app.db.models import ChatSession
from app.db.session import SessionLocal
from tests.conftest import auth_header


async def queued(text="hello", key="one"):
    user = uuid4()
    async with SessionLocal() as db:
        await touch_account(db, user, "test@example.edu")
        agent = await get_or_create_agent(db, user)
        session = ChatSession(id=str(uuid4()), user_id=user, agent_id=agent.id)
        db.add(session)
        await db.commit()
        token = auth_header(user)["Authorization"].removeprefix("Bearer ")
        run = await enqueue_run(
            db,
            user_id=user,
            session_id=session.id,
            kind="chat",
            payload={"text": text, "dependencies": {}},
            access_token=token,
            idempotency_key=key,
        )
        return user, run.id, session.id, token


async def test_run_survives_no_stream_consumer_and_replays_sequence():
    user, run_id, session_id, _ = await queued()
    assert await run_one("worker")
    async with SessionLocal() as db:
        run = await get_owned_run(db, run_id, user)
        assert run.status == "completed"
        assert run.token_enc is None
        session = await db.get(ChatSession, session_id)
        assert session.message_count == 1 and session.title == "hello"
        events = (
            await db.scalars(
                select(AssistantRunEvent).where(AssistantRunEvent.run_id == run_id).order_by(AssistantRunEvent.sequence)
            )
        ).all()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    replay = [chunk async for chunk in stream_events(run_id, user, after=1)]
    assert replay and replay[0].startswith(b"id: 2\n")
    assert b"[DONE]" in replay[-1]
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as rejected:
            await get_owned_run(db, run_id, uuid4())
        assert rejected.value.status_code == 404


async def test_enqueue_idempotency_and_busy_are_database_enforced():
    user, run_id, session_id, token = await queued()
    async with SessionLocal() as db:
        again = await enqueue_run(
            db,
            user_id=user,
            session_id=session_id,
            kind="chat",
            payload={"text": "hello", "dependencies": {"different_snapshot": True}},
            access_token=token,
            idempotency_key="one",
        )
        assert again.id == run_id
        with pytest.raises(HTTPException) as conflict:
            await enqueue_run(
                db,
                user_id=user,
                session_id=session_id,
                kind="chat",
                payload={"text": "changed"},
                access_token=token,
                idempotency_key="one",
            )
        assert conflict.value.status_code == 409
        with pytest.raises(HTTPException) as busy:
            await enqueue_run(
                db,
                user_id=user,
                session_id=session_id,
                kind="chat",
                payload={"text": "another"},
                access_token=token,
                idempotency_key="two",
            )
        assert busy.value.status_code == 409


async def test_cancel_before_start_never_calls_model(monkeypatch):
    user, run_id, _, _ = await queued()

    def forbidden(*args, **kwargs):
        pytest.fail("Cancelled queued run must never build an agent")

    monkeypatch.setattr("app.assistant.worker.build_agent", forbidden)
    async with SessionLocal() as db:
        await request_cancel(db, run_id, user)
    assert await run_one("worker")
    async with SessionLocal() as db:
        assert (await db.get(AssistantRun, run_id)).status == "cancelled"


async def test_worker_loss_fences_events_and_never_replays_a_started_run():
    user, run_id, _, _ = await queued()
    async with SessionLocal() as db:
        run = await claim_run(db, "lost")
        run.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(RunLeaseLost):
            await append_event(db, run_id, "lost", b"data: never\n\n")
        await db.rollback()
        assert await claim_run(db, "replacement") is None
        run = await get_owned_run(db, run_id, user)
        assert run.status == "interrupted" and run.token_enc is None
    replay = b"".join([chunk async for chunk in stream_events(run_id, user)])
    assert b"worker_interrupted" in replay and b"[DONE]" in replay


async def test_full_worker_uses_assistant_login_and_cannot_forge_requests(monkeypatch):
    import psycopg
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.agents.store import get_agno_db
    from app.config import get_settings

    user, run_id, _, _ = await queued()
    settings = get_settings()
    url = make_url(settings.database_url).set(drivername="postgresql")
    name = "assistant_execution_" + uuid4().hex[:8]
    password = uuid4().hex
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
        admin.execute(f'GRANT devrimo_assistant TO "{name}"')
        login = url.set(drivername="postgresql+asyncpg", username=name, password=password)
        execution_engine = create_async_engine(login)
        try:
            factory = async_sessionmaker(execution_engine, expire_on_commit=False)
            monkeypatch.setattr("app.assistant.worker.get_session_factory", lambda owner: factory)
            monkeypatch.setattr(settings, "assistant_database_url", login.render_as_string(hide_password=False))
            monkeypatch.setattr(settings, "database_runtime_role", "assistant")
            monkeypatch.setattr(settings, "workspace_gateway_url", "http://example.invalid/mcp/")
            get_agno_db.cache_clear()
            assert await run_one("real-assistant-login")
            async with SessionLocal() as db:
                assert (await get_owned_run(db, run_id, user)).status == "completed"
            from app.db.models import AccountDirectory, AccountStatus

            inactive_user, inactive_run, _, _ = await queued()
            async with SessionLocal() as db:
                account = await db.get(AccountDirectory, inactive_user)
                account.status = AccountStatus.suspended
                await db.commit()
            assert await run_one("real-assistant-login")
            async with SessionLocal() as db:
                assert (await db.get(AssistantRun, inactive_run)).status == "cancelled"
            with psycopg.connect(
                login.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
            ) as worker:
                for statement in (
                    "UPDATE assistant_runs SET payload='{}'::jsonb WHERE false",
                    "UPDATE assistant_runs SET user_id=gen_random_uuid() WHERE false",
                    "UPDATE workspace_mail_approvals SET status='pending' WHERE false",
                    "SELECT * FROM campus_credentials LIMIT 0",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        worker.execute(statement)
        finally:
            get_agno_db().close()
            get_agno_db.cache_clear()
            await execution_engine.dispose()
            admin.execute(f'DROP ROLE "{name}"')


async def test_parallel_worker_claims_only_execute_a_job_once():
    import asyncio

    _, run_id, _, _ = await queued()

    async def claim(owner):
        async with SessionLocal() as db:
            result = await claim_run(db, owner)
            return result.id if result else None

    claims = await asyncio.gather(claim("one"), claim("two"))
    assert claims.count(run_id) == 1 and claims.count(None) == 1


async def test_parallel_enqueue_keeps_one_active_run_per_user():
    import asyncio

    user, run_id, session_id, token = await queued()
    async with SessionLocal() as db:
        await db.delete(await db.get(AssistantRun, run_id))
        await db.commit()

    async def enqueue(key):
        async with SessionLocal() as db:
            try:
                return await enqueue_run(
                    db,
                    user_id=user,
                    session_id=session_id,
                    kind="chat",
                    payload={"text": key},
                    access_token=token,
                    idempotency_key=key,
                )
            except HTTPException as exc:
                return exc.status_code

    results = await asyncio.gather(enqueue("one"), enqueue("two"))
    assert sum(isinstance(result, AssistantRun) for result in results) == 1
    assert results.count(409) == 1


async def test_stop_cancels_queued_job_without_worker_resurrecting_entitlement():
    from app.agents.manager import get_agent, stop
    from app.db.models import AgentStatus

    user, run_id, _, _ = await queued()
    async with SessionLocal() as db:
        await stop(db, await get_agent(db, user))
    assert await run_one("worker")
    async with SessionLocal() as db:
        assert (await get_owned_run(db, run_id, user)).status == "cancelled"
        assert (await get_agent(db, user)).status == AgentStatus.stopped


async def test_destroy_removes_durable_requests_and_events():
    from app.agents.manager import destroy, get_agent

    user, run_id, _, _ = await queued()
    assert await run_one("worker")
    async with SessionLocal() as db:
        await destroy(db, await get_agent(db, user))
        assert await db.get(AssistantRun, run_id) is None
        assert await db.scalar(select(AssistantRunEvent).where(AssistantRunEvent.run_id == run_id)) is None


@pytest.mark.parametrize("status", ["suspended", "deletion_pending", "deleted"])
async def test_inactive_account_never_starts_model(monkeypatch, status):
    from app.db.models import AccountDirectory, AccountStatus

    user, run_id, _, _ = await queued()
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user)
        account.status = AccountStatus(status)
        await db.commit()
    monkeypatch.setattr("app.assistant.worker.build_agent", lambda *a: pytest.fail("Inactive account started model"))
    assert await run_one("worker")
    async with SessionLocal() as db:
        run = await db.get(AssistantRun, run_id)
        assert run.status == "cancelled" and run.token_enc is None


async def test_suspension_cancels_queue_and_reactivation_does_not_replay():
    from app.assistant.queue import cancel_account_runs
    from app.db.models import AccountDirectory, AccountStatus

    user, run_id, session_id, token = await queued()
    other, other_run, _, _ = await queued()
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user)
        account.status = AccountStatus.suspended
        await cancel_account_runs(db, user)
        await db.commit()
        run = await db.get(AssistantRun, run_id)
        assert run.status == "cancelled" and run.token_enc is None
        assert (await db.get(AssistantRun, other_run)).status == "queued"
        with pytest.raises(HTTPException) as rejected:
            await enqueue_run(
                db,
                user_id=user,
                session_id=session_id,
                kind="chat",
                payload={"text": "hello"},
                access_token=token,
                idempotency_key="one",
            )
        assert rejected.value.status_code == 403
        account.status = AccountStatus.active
        await db.commit()
    replay = b"".join([chunk async for chunk in stream_events(run_id, user)])
    assert b"account_inactive" in replay and b"[DONE]" in replay
    assert await run_one("worker")
    async with SessionLocal() as db:
        assert (await db.get(AssistantRun, run_id)).status == "cancelled"
        assert (await get_owned_run(db, other_run, other)).status == "completed"


async def test_suspension_fences_late_completion_and_renewal():
    from app.assistant.queue import cancel_account_runs, finish_run, renew_run
    from app.db.models import AccountDirectory, AccountStatus

    user, run_id, session_id, _ = await queued()
    async with SessionLocal() as db:
        await claim_run(db, "worker")
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user)
        account.status = AccountStatus.suspended
        await cancel_account_runs(db, user)
        await db.commit()
    async with SessionLocal() as db:
        assert await renew_run(db, run_id, "worker")
        await finish_run(db, run_id, "worker", "completed")
        run = await db.get(AssistantRun, run_id)
        assert run.status == "cancelled" and run.token_enc is None
        assert (await db.get(ChatSession, session_id)).message_count == 0


async def test_admin_suspension_revokes_approval_even_when_remote_ban_fails(monkeypatch):
    import httpx

    from app.admin.supabase import SupabaseAdmin
    from app.config import get_settings
    from app.main import app
    from app.workspace.approvals import issue_approval
    from app.workspace.models import WorkspaceMailApproval

    user, run_id, _, _ = await queued()
    operator = uuid4()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(operator))

    async def fail_remote(*args, **kwargs):
        raise RuntimeError("Remote ban unavailable")

    monkeypatch.setattr(SupabaseAdmin, "update_user", fail_remote)
    async with SessionLocal() as db:
        await issue_approval(
            db,
            user,
            "paused-run",
            {"draft": {"to": "recipient@example.edu", "subject": "Approved", "body": "Exact draft"}},
        )
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/users/{user}/suspend",
            headers=auth_header(operator),
            json={"reason": "Security regression test"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "partial"
    async with SessionLocal() as db:
        assert (await db.get(AssistantRun, run_id)).status == "cancelled"
        approval = await db.scalar(select(WorkspaceMailApproval).where(WorkspaceMailApproval.user_id == user))
        assert approval.status == "revoked"


async def test_enqueue_waits_for_suspension_transaction_then_rejects():
    import asyncio

    from app.assistant.queue import cancel_account_runs
    from app.db.models import AccountDirectory, AccountStatus

    user, _, session_id, token = await queued()
    started = asyncio.Event()

    async def enqueue():
        async with SessionLocal() as db:
            started.set()
            with pytest.raises(HTTPException) as rejected:
                await enqueue_run(
                    db,
                    user_id=user,
                    session_id=session_id,
                    kind="chat",
                    payload={"text": "late"},
                    access_token=token,
                    idempotency_key="late",
                )
            assert rejected.value.status_code == 403

    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user)
        account.status = AccountStatus.suspended
        await cancel_account_runs(db, user)
        task = asyncio.create_task(enqueue())
        try:
            await started.wait()
            await asyncio.sleep(0.1)
            assert not task.done()
            await db.commit()
            await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_heartbeat_interrupts_inflight_model_after_suspension(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.assistant.queue import cancel_account_runs
    from app.db.models import AccountDirectory, AccountStatus

    user, run_id, _, _ = await queued()
    started, closed = asyncio.Event(), asyncio.Event()
    close_client = AsyncMock()
    monkeypatch.setattr(
        "app.assistant.worker.build_agent",
        lambda *a: SimpleNamespace(model=SimpleNamespace(async_client=SimpleNamespace(close=close_client))),
    )

    async def blocked_stream(*args):
        try:
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            closed.set()

    monkeypatch.setattr("app.assistant.worker._serialize_run", blocked_stream)
    task = asyncio.create_task(run_one("worker"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with SessionLocal() as db:
            account = await db.get(AccountDirectory, user)
            account.status = AccountStatus.suspended
            await cancel_account_runs(db, user)
            await db.commit()
        assert await asyncio.wait_for(task, timeout=6)
        assert closed.is_set()
        close_client.assert_awaited_once()
        async with SessionLocal() as db:
            assert (await db.get(AssistantRun, run_id)).status == "cancelled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
