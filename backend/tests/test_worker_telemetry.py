import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.observability import client as ph_client


@pytest.fixture
def captured(monkeypatch):
    events: list[tuple[str, dict]] = []
    exceptions: list[tuple[BaseException, dict]] = []
    monkeypatch.setattr(ph_client, "capture", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(ph_client, "report_exception", lambda exc, **kw: exceptions.append((exc, kw)))
    return events, exceptions


def _events(events, name):
    return [properties for event, properties in events if event == name]


def _session_factory():
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Session


def test_safe_job_failure_keeps_type_without_message_or_issue(captured):
    from app.observability.jobs import observed_job

    events, exceptions = captured
    secret = "scraped student name and upstream response body"

    with observed_job("worker_pass", report_exceptions=False, include_failure_reason=False) as observation:
        observation.failed(RuntimeError(secret))

    outcome = _events(events, "background_job_completed")[0]
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["error_type"] == "RuntimeError"
    assert outcome["reason"] is None
    assert secret not in repr(events)
    assert not exceptions


async def test_domain_worker_reports_lifecycle_and_safe_pass_failure(captured, monkeypatch):
    from app.observability.runtime import service_name
    from app.workers import runtime

    events, exceptions = captured
    stop = asyncio.Event()
    monkeypatch.setattr(runtime, "configure_logging", lambda: None)
    monkeypatch.setattr(runtime, "posthog_initialize", lambda: None)
    monkeypatch.setattr(runtime, "posthog_shutdown", lambda: None)
    monkeypatch.setattr(runtime, "posthog_logs_shutdown", lambda: None)
    monkeypatch.setattr(runtime, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(runtime, "validate_runtime_database", AsyncMock())

    secret = "course page body with a student name"

    async def fail_pass(_kind):
        stop.set()
        raise RuntimeError(secret)

    monkeypatch.setattr(runtime, "_pass", fail_pass)
    await runtime.run("catalog", stop_event=stop)

    lifecycle = _events(events, "background_worker_lifecycle")
    assert [(item["worker"], item["state"]) for item in lifecycle] == [
        ("catalog", "started"),
        ("catalog", "stopped"),
    ]
    outcome = _events(events, "background_job_completed")[0]
    assert outcome["job_kind"] == "catalog_worker_pass"
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["error_type"] == "RuntimeError"
    assert outcome["reason"] is None
    assert outcome["retrying"] is True
    assert secret not in repr(events)
    assert len(exceptions) == 1
    issue, properties = exceptions[0]
    assert issue.args == ("catalog_worker_pass failed (RuntimeError)",)
    assert issue.__traceback__ is None
    assert properties["error_type"] == "RuntimeError"
    assert properties["$exception_fingerprint"] == ["background_job", "catalog_worker_pass", "RuntimeError"]
    assert secret not in repr(exceptions)
    assert service_name() == "devrimo-catalog-worker"


async def test_assistant_finalization_failure_is_unknown_and_safe(captured, monkeypatch):
    from app.assistant import worker
    from app.observability import turns

    events, exceptions = captured
    turn_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(turns, "capture", lambda event, **kw: turn_events.append((event, kw)))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    user_id = uuid4()
    run_id = uuid4()
    secret = "database response with private transcript content"
    monkeypatch.setattr(worker, "get_session_factory", lambda _owner: lambda: _Session())
    monkeypatch.setattr(worker, "decrypt_secret", lambda _value: "test-token")
    monkeypatch.setattr(worker, "verify_access_token", lambda _token: SimpleNamespace(id=user_id))
    monkeypatch.setattr(worker, "renew_run", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "active_account", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        worker,
        "get_runtime_config",
        AsyncMock(return_value=SimpleNamespace(model_id="test-model")),
    )
    monkeypatch.setattr(worker, "build_agent", lambda *_args: SimpleNamespace(model=SimpleNamespace()))

    async def stream(*_args):
        yield b"data: {}\n\n"

    monkeypatch.setattr(worker, "_serialize_run", stream)
    monkeypatch.setattr(worker, "append_event", AsyncMock())
    monkeypatch.setattr(worker, "finish_run", AsyncMock(side_effect=RuntimeError(secret)))

    run = SimpleNamespace(
        id=run_id,
        user_id=user_id,
        session_id="session-1",
        kind="chat",
        payload={"text": "hello", "agno_session_id": "session-1", "request_id": "request-1"},
        cancel_requested=False,
        token_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_enc="encrypted-token",
        approval_token_enc=None,
    )

    with pytest.raises(RuntimeError, match="database response"):
        await worker._execute(run, "worker-1", {})

    turn = next(fields for event, fields in turn_events if event == "chat_turn_completed")
    assert turn["outcome"] == "run_error"
    assert turn["durable_status"] == "unknown"
    assert turn["durable_finalization"] == "failed"
    assert turn["request_id"] == "request-1"
    outcome = _events(events, "background_job_completed")[0]
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["durable_status"] == "unknown"
    assert outcome["durable_finalization"] == "failed"
    issue, properties = exceptions[0]
    assert issue.args == ("assistant_run failed (RuntimeError)",)
    assert issue.__traceback__ is None
    assert properties["$exception_fingerprint"] == ["background_job", "assistant_run", "RuntimeError"]
    assert secret not in repr(events)
    assert secret not in repr(exceptions)


async def test_assistant_lease_loss_is_interrupted_without_finalization(captured, monkeypatch):
    from app.assistant import worker
    from app.assistant.queue import RunLeaseLost
    from app.observability import turns

    events, exceptions = captured
    turn_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(turns, "capture", lambda event, **kw: turn_events.append((event, kw)))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    user_id = uuid4()
    run_id = uuid4()
    monkeypatch.setattr(worker, "get_session_factory", lambda _owner: lambda: _Session())
    monkeypatch.setattr(worker, "decrypt_secret", lambda _value: "test-token")
    monkeypatch.setattr(worker, "verify_access_token", lambda _token: SimpleNamespace(id=user_id))
    monkeypatch.setattr(worker, "renew_run", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "active_account", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        worker,
        "get_runtime_config",
        AsyncMock(return_value=SimpleNamespace(model_id="test-model")),
    )
    monkeypatch.setattr(worker, "build_agent", lambda *_args: SimpleNamespace(model=SimpleNamespace()))

    async def stream(*_args):
        yield b"data: {}\n\n"

    monkeypatch.setattr(worker, "_serialize_run", stream)
    monkeypatch.setattr(worker, "append_event", AsyncMock(side_effect=RunLeaseLost("stale lease")))
    finish = AsyncMock()
    monkeypatch.setattr(worker, "finish_run", finish)

    run = SimpleNamespace(
        id=run_id,
        user_id=user_id,
        session_id="session-1",
        kind="chat",
        payload={"text": "hello", "agno_session_id": "session-1", "request_id": "request-lease"},
        cancel_requested=False,
        token_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_enc="encrypted-token",
        approval_token_enc=None,
    )

    with pytest.raises(RunLeaseLost):
        await worker._execute(run, "worker-lease", {})

    finish.assert_not_awaited()
    turn = next(fields for event, fields in turn_events if event == "chat_turn_completed")
    assert turn["outcome"] == "interrupted"
    assert turn["result"] == "expected_failure"
    assert turn["durable_status"] == "unknown"
    assert turn["durable_finalization"] == "lease_lost"
    assert turn["run_id"] == str(run_id)
    assert turn["request_id"] == "request-lease"
    outcome = _events(events, "background_job_completed")[0]
    assert outcome["outcome"] == "expected_failure"
    assert outcome["reason"] == "lease_lost"
    assert outcome["durable_status"] == "unknown"
    assert outcome["durable_finalization"] == "lease_lost"
    assert not exceptions


async def test_assistant_error_event_append_failure_still_finalizes(captured, monkeypatch):
    from app.assistant import worker
    from app.observability import turns

    events, exceptions = captured
    turn_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(turns, "capture", lambda event, **kw: turn_events.append((event, kw)))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    user_id = uuid4()
    run_id = uuid4()
    append_secret = "private database response"
    monkeypatch.setattr(worker, "get_session_factory", lambda _owner: lambda: _Session())
    monkeypatch.setattr(worker, "decrypt_secret", lambda _value: "test-token")
    monkeypatch.setattr(worker, "verify_access_token", lambda _token: SimpleNamespace(id=user_id))
    monkeypatch.setattr(worker, "renew_run", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "active_account", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        worker,
        "get_runtime_config",
        AsyncMock(return_value=SimpleNamespace(model_id="test-model")),
    )
    monkeypatch.setattr(worker, "build_agent", lambda *_args: SimpleNamespace(model=SimpleNamespace()))

    async def stream(*_args):
        raise RuntimeError("model execution failed")
        yield b""

    monkeypatch.setattr(worker, "_serialize_run", stream)
    append_event = AsyncMock(side_effect=RuntimeError(append_secret))
    monkeypatch.setattr(worker, "append_event", append_event)
    finish = AsyncMock()
    monkeypatch.setattr(worker, "finish_run", finish)

    run = SimpleNamespace(
        id=run_id,
        user_id=user_id,
        session_id="session-1",
        kind="chat",
        payload={"text": "hello", "agno_session_id": "session-1", "request_id": "request-append"},
        cancel_requested=False,
        token_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_enc="encrypted-token",
        approval_token_enc=None,
    )

    await worker._execute(run, "worker-append", {})

    append_event.assert_awaited_once()
    finish.assert_awaited_once()
    turn = next(fields for event, fields in turn_events if event == "chat_turn_completed")
    assert turn["outcome"] == "stream_error"
    assert turn["result"] == "unexpected_failure"
    assert turn["durable_status"] == "failed"
    assert turn["durable_finalization"] == "committed"
    assert turn["request_id"] == "request-append"
    outcome = _events(events, "background_job_completed")[0]
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["durable_status"] == "failed"
    assert outcome["durable_finalization"] == "committed"
    assert outcome["error_event_append_error_type"] == "RuntimeError"
    assert append_secret not in repr(events)
    assert append_secret not in repr(exceptions)


async def test_domain_worker_cancels_an_active_pass_on_requested_stop(captured, monkeypatch):
    from app.workers import runtime

    events, _ = captured
    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    monkeypatch.setattr(runtime, "configure_logging", lambda: None)
    monkeypatch.setattr(runtime, "posthog_initialize", lambda: None)
    monkeypatch.setattr(runtime, "posthog_shutdown", lambda: None)
    monkeypatch.setattr(runtime, "posthog_logs_shutdown", lambda: None)
    monkeypatch.setattr(runtime, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(runtime, "validate_runtime_database", AsyncMock())

    async def blocked_pass(_kind):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime, "_pass", blocked_pass)
    running = asyncio.create_task(runtime.run("researcher", stop_event=stop))
    await asyncio.wait_for(started.wait(), 2)
    stop.set()
    await asyncio.wait_for(running, 2)

    assert cancelled.is_set()
    outcome = _events(events, "background_job_completed")[0]
    assert outcome["job_kind"] == "researcher_worker_pass"
    assert outcome["outcome"] == "cancelled"
    assert outcome["reason"] == "shutdown"


async def test_embedding_job_success_and_retryable_failure_are_distinct(captured, monkeypatch):
    from app.knowledge import embedding_worker

    events, exceptions = captured
    sessions = _session_factory()
    generation_id = uuid4()

    async def success(*_args, **_kwargs):
        return None

    monkeypatch.setattr(embedding_worker, "run_job", success)
    await embedding_worker._run_observed_job(generation_id, "embedding-owner", 2, sessions, asyncio.Event())

    success_event = _events(events, "background_job_completed")[0]
    assert success_event["job_kind"] == "knowledge_embedding"
    assert success_event["outcome"] == "success"
    assert success_event["job_status"] == "completed"
    assert success_event["retrying"] is False
    assert success_event["dead"] is False

    async def fail(*_args, **_kwargs):
        raise RuntimeError("provider response contains private scraped content")

    failure = AsyncMock()
    monkeypatch.setattr(embedding_worker, "run_job", fail)
    monkeypatch.setattr(embedding_worker, "fail_index_job", failure)
    await embedding_worker._run_observed_job(generation_id, "embedding-owner", 3, sessions, asyncio.Event())

    failure_event = _events(events, "background_job_completed")[1]
    assert failure_event["outcome"] == "unexpected_failure"
    assert failure_event["error_type"] == "RuntimeError"
    assert failure_event["job_status"] == "failed"
    assert failure_event["retrying"] is True
    assert failure_event["dead"] is False
    assert failure_event["reason"] is None
    assert "private scraped content" not in repr(events)
    assert not exceptions
    failure.assert_awaited_once()


async def test_embedding_lease_loss_is_retryable_without_writing_failure(captured, monkeypatch):
    from app.knowledge import embedding_worker
    from app.knowledge.indexes import IndexLeaseLost

    events, exceptions = captured
    sessions = _session_factory()

    async def lost(*_args, **_kwargs):
        raise IndexLeaseLost("stale private response")

    failure = AsyncMock()
    monkeypatch.setattr(embedding_worker, "run_job", lost)
    monkeypatch.setattr(embedding_worker, "fail_index_job", failure)
    await embedding_worker._run_observed_job(uuid4(), "embedding-owner", 4, sessions, asyncio.Event())

    outcome = _events(events, "background_job_completed")[0]
    assert outcome["outcome"] == "expected_failure"
    assert outcome["reason"] == "lease_lost"
    assert outcome["retrying"] is True
    assert outcome["dead"] is False
    failure.assert_not_awaited()
    assert not exceptions


async def test_embedding_run_job_cancels_processing_when_stopping(captured, monkeypatch):
    from app.knowledge import embedding_worker

    sessions = _session_factory()
    monkeypatch.setattr(embedding_worker, "get_session_factory", lambda _owner: sessions)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process_batch(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(embedding_worker, "process_index_batch", process_batch)
    stop = asyncio.Event()
    task = asyncio.create_task(embedding_worker.run_job(uuid4(), "embedding-owner", 1, stop_event=stop))
    await asyncio.wait_for(started.wait(), 2)
    stop.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert cancelled.is_set()


async def test_embedding_worker_flushes_clients_on_requested_stop(captured, monkeypatch):
    from app.knowledge import embedding_worker

    events, _ = captured
    sessions = _session_factory()
    stop = asyncio.Event()
    monkeypatch.setattr(embedding_worker, "configure_logging", lambda: None)
    monkeypatch.setattr(embedding_worker, "posthog_initialize", lambda: None)
    monkeypatch.setattr(embedding_worker, "posthog_shutdown", lambda: None)
    monkeypatch.setattr(embedding_worker, "posthog_logs_shutdown", lambda: None)
    monkeypatch.setattr(embedding_worker, "validate_runtime_database", AsyncMock())
    monkeypatch.setattr(embedding_worker, "get_session_factory", lambda _owner: sessions)
    dispose = AsyncMock()
    close_client = AsyncMock()
    monkeypatch.setattr(embedding_worker, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(embedding_worker, "close_embedding_client", close_client)

    async def claim(*_args, **_kwargs):
        stop.set()
        return None

    monkeypatch.setattr(embedding_worker, "claim_index_job", claim)
    await embedding_worker.run(stop_event=stop)

    lifecycle = _events(events, "background_worker_lifecycle")
    assert [item["state"] for item in lifecycle] == ["started", "stopped"]
    assert _events(events, "background_job_completed")[0]["job_kind"] == "embedding_worker_pass"
    close_client.assert_awaited_once()
    dispose.assert_awaited_once()
