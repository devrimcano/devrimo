"""Coverage for the workflows that used to fail without leaving a trace.

Background jobs, the knowledge worker, embedding calls, log forwarding and the
diagnostics of last resort. Each of these previously handled its own failures
and reported nothing, so the symptom was always the same: something stopped
working and the only evidence was a warning line in a container log.
"""

import asyncio
import logging
from uuid import uuid4

import httpx
import pytest

from app.knowledge.embeddings import EmbeddingConfig, _request_embeddings
from app.observability import diagnostics
from app.observability.jobs import observed_job


@pytest.fixture
def captured(monkeypatch):
    """Every ``capture`` and ``report_exception`` made anywhere in the app."""
    events: list[tuple[str, dict]] = []
    exceptions: list[tuple[BaseException, dict]] = []
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(
        "app.observability.client.report_exception",
        lambda exc, **kw: (exceptions.append((exc, kw)), True)[1],
    )
    return events, exceptions


def _event(events, name):
    return next(kw for event, kw in events if event == name)


# --- background jobs --------------------------------------------------------


def test_a_successful_job_reports_one_terminal_outcome(captured):
    events, exceptions = captured

    with observed_job("directory_sync") as job:
        job.succeeded(users=12)

    outcome = _event(events, "background_job_completed")
    assert outcome["job_kind"] == "directory_sync"
    assert outcome["outcome"] == "success"
    assert outcome["users"] == 12
    assert outcome["duration_seconds"] >= 0
    assert not exceptions


def test_an_expected_job_failure_is_an_event_and_not_an_issue(captured):
    """A lost lease is a multi-worker deployment working, not a defect."""
    events, exceptions = captured

    with observed_job("knowledge_ingestion", job_id="job-1", source_id="src-1") as job:
        job.expected_failure("lease_lost", detail="claimed elsewhere")

    outcome = _event(events, "background_job_completed")
    assert outcome["outcome"] == "expected_failure"
    assert outcome["reason"] == "lease_lost"
    assert outcome["job_id"] == "job-1"
    assert outcome["source_id"] == "src-1"
    assert not exceptions


def test_an_unhandled_job_failure_becomes_an_issue_with_job_context(captured):
    events, exceptions = captured

    with pytest.raises(KeyError):
        with observed_job("knowledge_ingestion", job_id="job-2", source_id="src-2"):
            raise KeyError("adapter field")

    outcome = _event(events, "background_job_completed")
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["error_type"] == "KeyError"

    assert len(exceptions) == 1
    _, properties = exceptions[0]
    assert properties["job_kind"] == "knowledge_ingestion"
    assert properties["job_id"] == "job-2"
    assert properties["source_id"] == "src-2"


def test_a_cancelled_job_is_not_reported_as_a_defect(captured):
    """Every worker restart would otherwise file an issue."""
    events, exceptions = captured

    with pytest.raises(asyncio.CancelledError):
        with observed_job("knowledge_ingestion"):
            raise asyncio.CancelledError()

    assert _event(events, "background_job_completed")["outcome"] == "cancelled"
    assert not exceptions


def test_job_context_reaches_log_lines_inside_the_job():
    import structlog

    seen: dict = {}
    with observed_job("knowledge_ingestion", job_id="job-3", source_id="src-3"):
        seen.update(structlog.contextvars.get_contextvars())

    assert seen["job_kind"] == "knowledge_ingestion"
    assert seen["job_id"] == "job-3"
    assert seen["request_id"]
    # Cleared on the way out, so one job's identity cannot bleed into the next.
    assert "job_id" not in structlog.contextvars.get_contextvars()


# --- the knowledge worker ---------------------------------------------------


class _Lease:
    job_id = uuid4()
    source_id = uuid4()
    kind = "ingest"
    owner = "worker-1"
    attempt = 1


async def test_worker_reports_a_completed_job(captured, monkeypatch):
    from app.knowledge import worker

    events, _ = captured
    monkeypatch.setattr(worker, "run_leased_job", lambda lease: _resolved(7))

    await worker._run_job(_Lease())

    outcome = _event(events, "background_job_completed")
    assert outcome["outcome"] == "success"
    assert outcome["records"] == 7
    assert outcome["ingestion_kind"] == "ingest"


async def test_worker_records_whether_a_failed_job_will_be_retried(captured, monkeypatch):
    """"Will retry in 30 seconds" and "will never run again" were one log line."""
    from app.knowledge import worker

    events, exceptions = captured

    async def explode(lease):
        raise RuntimeError("adapter returned nothing")

    async def fail_job(db, lease, exc):
        return "dead"

    monkeypatch.setattr(worker, "run_leased_job", explode)
    monkeypatch.setattr(worker, "fail_job", fail_job)

    await worker._run_job(_Lease())

    outcome = _event(events, "background_job_completed")
    assert outcome["outcome"] == "unexpected_failure"
    assert outcome["error_type"] == "RuntimeError"
    assert outcome["job_status"] == "dead"
    assert outcome["dead"] is True
    assert outcome["retrying"] is False
    assert len(exceptions) == 1


async def test_worker_treats_a_lost_lease_as_expected(captured, monkeypatch):
    from app.knowledge import worker
    from app.knowledge.ingestion import JobLeaseLost

    events, exceptions = captured

    async def lost(lease):
        raise JobLeaseLost("claimed elsewhere")

    monkeypatch.setattr(worker, "run_leased_job", lost)

    await worker._run_job(_Lease())

    assert _event(events, "background_job_completed")["outcome"] == "expected_failure"
    assert not exceptions


async def _resolved(value):
    return value


# --- embedding calls --------------------------------------------------------


def _config(**overrides) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="remote",
        model="text-embedding-3-small",
        base_url="https://embeddings.test/v1",
        dimensions=1536,
        batch_size=32,
        api_key="test-key",
        **overrides,
    )


def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


async def test_an_embedding_batch_emits_an_ai_event_with_reported_usage(captured, monkeypatch):
    from app.knowledge import embeddings

    events, _ = captured

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1] * 1536}],
                "usage": {"prompt_tokens": 11, "total_tokens": 11},
            },
        )

    monkeypatch.setattr(embeddings, "_embedding_client", lambda: _transport(handler))

    await _request_embeddings(_config(), ["kütüphane saatleri"])

    event = _event(events, "$ai_embedding")
    assert event["$ai_provider"] == "remote"
    assert event["$ai_model"] == "text-embedding-3-small"
    assert event["$ai_input_tokens"] == 11
    assert event["$ai_total_tokens"] == 11
    assert event["batch_size"] == 1
    assert event["$ai_latency"] >= 0
    # No price table for this provider, and saying so beats implying zero.
    assert event["ai_cost"] == "unavailable"


async def test_a_provider_that_reports_no_usage_says_so_explicitly(captured, monkeypatch):
    """"We did not measure this" is not the same fact as "this cost nothing"."""
    from app.knowledge import embeddings

    events, _ = captured

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1536}]})

    monkeypatch.setattr(embeddings, "_embedding_client", lambda: _transport(handler))

    await _request_embeddings(_config(), ["duyurular"])

    event = _event(events, "$ai_embedding")
    assert event["ai_usage"] == "unavailable"
    assert "$ai_input_tokens" not in event


async def test_an_embedding_failure_is_recorded_on_the_event(captured, monkeypatch):
    from app.knowledge import embeddings

    events, _ = captured

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    monkeypatch.setattr(embeddings, "_embedding_client", lambda: _transport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        await _request_embeddings(_config(), ["yurt"])

    event = _event(events, "$ai_embedding")
    assert event["$ai_is_error"] is True
    assert event["error_type"] == "HTTPStatusError"
    assert event["$ai_http_status"] == 503


async def test_a_failed_query_embedding_reports_the_silent_degradation(captured, monkeypatch):
    """Retrieval falls back to lexical ranking and still looks successful."""
    from app.knowledge import embeddings

    _, exceptions = captured

    async def explode(*args, **kwargs):
        raise httpx.ConnectError("embedding host unreachable")

    monkeypatch.setattr(embeddings, "_request_embeddings", explode)
    # The module imports the reporter by name, so that is the name to replace.
    monkeypatch.setattr(
        embeddings,
        "report_exception",
        lambda exc, **kw: (exceptions.append((exc, kw)), True)[1],
    )

    result = await embeddings.embed_query(None, uuid4(), "kayıt haftası", config=_config())

    assert result is None
    assert len(exceptions) == 1
    assert exceptions[0][1]["degraded_to"] == "lexical_only"


# --- log forwarding ---------------------------------------------------------


def test_log_attributes_keep_collections_instead_of_dropping_them(monkeypatch):
    """Every list and dict used to be discarded on the way to OTLP."""
    from app.observability import logs

    emitted: list[dict] = []
    monkeypatch.setattr(logs, "_setup", lambda: type("L", (), {"emit": lambda self, **r: emitted.append(r)})())

    logs.posthog_log_processor(
        None,
        "info",
        {
            "event": "knowledge_job_completed",
            "level": "info",
            "source_ids": ["a", "b"],
            "counts": {"records": 3},
            "records": 3,
        },
    )

    attributes = emitted[0]["attributes"]
    assert attributes["source_ids"] == '["a", "b"]'
    assert attributes["counts"] == '{"records": 3}'
    assert attributes["records"] == 3


def test_long_log_values_are_bounded(monkeypatch):
    from app.observability import logs

    emitted: list[dict] = []
    monkeypatch.setattr(logs, "_setup", lambda: type("L", (), {"emit": lambda self, **r: emitted.append(r)})())

    logs.posthog_log_processor(None, "info", {"event": "x", "level": "info", "detail": "y" * 10_000})

    assert len(emitted[0]["attributes"]["detail"]) == logs.MAX_ATTRIBUTE_CHARS + 1


def test_telemetry_diagnostics_are_never_forwarded_over_the_exporter(monkeypatch):
    """Reporting a broken exporter through that exporter is a loop."""
    from app.observability import logs

    emitted: list[dict] = []
    monkeypatch.setattr(logs, "_setup", lambda: type("L", (), {"emit": lambda self, **r: emitted.append(r)})())

    logs.posthog_log_processor(
        None,
        "warning",
        {"event": "posthog_export_failed", "level": "warning", "telemetry": "local"},
    )

    assert emitted == []


def test_stdlib_warnings_reach_posthog_but_sdk_warnings_do_not(monkeypatch):
    from app.logging import StdlibBridgeHandler

    forwarded: list[dict] = []
    local: list[tuple] = []
    monkeypatch.setattr(
        "app.observability.logs.posthog_log_processor",
        lambda logger, method, event: forwarded.append(event) or event,
    )
    monkeypatch.setattr("app.observability.diagnostics.report_local", lambda code, **kw: local.append((code, kw)))

    handler = StdlibBridgeHandler()

    def record(name: str, level: int, message: str) -> logging.LogRecord:
        return logging.LogRecord(name, level, __file__, 1, message, None, None)

    handler.emit(record("sqlalchemy.pool", logging.WARNING, "connection invalidated"))
    handler.emit(record("asyncpg", logging.INFO, "chatter"))
    handler.emit(record("posthog.client", logging.WARNING, "lane queue is full"))

    assert [event["event"] for event in forwarded] == ["connection invalidated"]
    assert forwarded[0]["logger"] == "sqlalchemy.pool"
    assert local == [("telemetry_sdk_warning", {"logger": "posthog.client", "detail": "lane queue is full"})]


# --- diagnostics of last resort ---------------------------------------------


def test_diagnostics_are_rate_limited_and_count_what_they_suppressed(monkeypatch):
    """A failing exporter fails once per batch; unthrottled that is a disk."""
    lines: list[tuple] = []
    diagnostics.reset()
    monkeypatch.setattr(
        "app.logging.get_logger",
        lambda name: type("L", (), {"warning": lambda self, code, **kw: lines.append((code, kw))})(),
    )

    for _ in range(5):
        diagnostics.report_local("posthog_export_failed", error="ConnectError")

    assert len(lines) == 1
    assert lines[0][1]["telemetry"] == "local"

    monkeypatch.setattr(diagnostics, "DIAGNOSTIC_INTERVAL_SECONDS", 0.0)
    diagnostics.report_local("posthog_export_failed", error="ConnectError")

    assert len(lines) == 2
    assert lines[1][1]["suppressed_since_last"] == 4
    diagnostics.reset()
