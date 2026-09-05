"""Observability must be invisible when off and correct when on.

The first half of this file is the more important half. PostHog is optional,
CI runs with no key, and the failure mode of getting that wrong is not a broken
test — it is a broker that refuses to start, or a chat turn that dies because
an analytics sink was unreachable. So the no-op path is asserted explicitly
rather than assumed.
"""

import time

import pytest

from app.config import get_settings
from app.observability import client as ph_client
from app.observability.llm import build_traced_async_client, current_trace_id, llm_turn, new_trace_id
from app.observability.turns import TurnObservation
from tests.conftest import auth_header, new_user_id


@pytest.fixture
def unconfigured():
    ph_client.get_posthog.cache_clear()
    get_settings.cache_clear()
    yield
    ph_client.get_posthog.cache_clear()
    get_settings.cache_clear()


# --- with no key configured -------------------------------------------------


def test_client_is_none_without_a_key(unconfigured):
    assert not get_settings().posthog_api_key
    assert ph_client.get_posthog() is None


def test_client_is_none_with_a_whitespace_only_key(monkeypatch, unconfigured):
    monkeypatch.setenv("POSTHOG_API_KEY", "   ")
    get_settings.cache_clear()

    assert not get_settings().posthog_configured
    assert ph_client.get_posthog() is None


def test_capture_helpers_are_silent_no_ops(unconfigured):
    # No exception, no network, no output. The names are deliberately
    # self-identifying: `some_event` and a bare `ValueError("boom")` from this
    # file reached the live project once, and an event that escapes a test
    # again should say where it came from.
    ph_client.capture("pytest_no_op_event", distinct_id="pytest-user")
    ph_client.capture_exception(ValueError("pytest no-op exception"), distinct_id="pytest-user")
    ph_client.shutdown()


def test_traced_client_falls_back_to_stock_agno(unconfigured):
    assert build_traced_async_client(api_key="sk-test", base_url="https://example.test/v1") is None


def test_turn_observation_reports_nothing_but_still_finishes(unconfigured):
    observation = TurnObservation(trace_id=new_trace_id(), user_id="user-1", session_id="s-1")
    observation.tool_started("sais_get_transcript")
    observation.tool_failed("sais_get_transcript", "pytest tool failure")
    observation.finish()
    assert observation._finished


async def test_chat_turn_works_end_to_end_without_posthog(client, unconfigured):
    """The real regression guard: a turn must not depend on telemetry."""
    headers = auth_header(new_user_id())
    assert (await client.post("/api/v1/agents/provision", headers=headers)).status_code == 201
    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "merhaba"}], "session_id": "obs-1"},
    )
    assert response.status_code == 200
    assert b"data: [DONE]" in response.content


# --- trace scoping ----------------------------------------------------------


def test_llm_turn_scopes_and_restores_the_trace_id():
    assert current_trace_id.get() is None
    with llm_turn("trace-1", "session-1"):
        assert current_trace_id.get() == "trace-1"
        with llm_turn("trace-2", "session-2"):
            assert current_trace_id.get() == "trace-2"
        assert current_trace_id.get() == "trace-1"
    assert current_trace_id.get() is None


# --- the outcome that must never be swallowed -------------------------------


def test_turn_observation_records_failure_and_is_idempotent(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "app.observability.turns.capture",
        lambda event, **kw: captured.append((event, kw)),
    )
    observation = TurnObservation(trace_id="t-1", user_id="u-1", session_id="s-1", started=time.monotonic())
    observation.run_failed("model refused", "ModelProviderError")
    observation.finish()
    observation.finish()  # second call must not double-report

    events = {event for event, _ in captured}
    assert events == {"$ai_trace", "chat_turn_completed"}

    trace = next(kw for event, kw in captured if event == "$ai_trace")
    assert trace["$ai_is_error"] is True
    assert trace["$ai_error"] == "model refused"
    assert trace["$ai_trace_id"] == "t-1"

    turn = next(kw for event, kw in captured if event == "chat_turn_completed")
    assert turn["outcome"] == "run_error"
    assert turn["error_type"] == "ModelProviderError"


def test_tool_failures_are_counted_and_reported(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "app.observability.turns.capture",
        lambda event, **kw: captured.append((event, kw)),
    )
    observation = TurnObservation(trace_id="t-2", user_id="u-2")
    observation.tool_started("sais_get_transcript")
    observation.tool_failed("sais_get_transcript", "METU said no")
    observation.finish()

    tool_error = next(kw for event, kw in captured if event == "agent_tool_error")
    assert tool_error["tool"] == "sais_get_transcript"
    assert tool_error["$ai_trace_id"] == "t-2"

    turn = next(kw for event, kw in captured if event == "chat_turn_completed")
    assert turn["tool_calls"] == 1
    assert turn["tool_errors"] == 1
    assert turn["tools_used"] == ["sais_get_transcript"]
    # A tool the agent recovered from is preserved as its own failure without
    # marking the turn unsuccessful.
    assert turn["recovered_tool_errors"] == ["sais_get_transcript"]
    assert turn["outcome"] == "completed"
    assert turn["result"] == "success"


# --- secrets must not leave the process -------------------------------------


def test_before_send_redacts_credential_shaped_values():
    event = {
        "event": "test_event",
        "properties": {
            "metu_password": "hunter2",
            "authorization": "Bearer abc.def.ghi",
            "nested": {"access_token": "phx_0123456789abcdef0", "safe": "keep me"},
            "free_text": "token is eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature here",
            "messages": [{"role": "user", "content": "Ekle-birak ne zaman?"}],
        },
    }

    result = ph_client._before_send(event)
    assert result["properties"]["metu_password"] == "[redacted]"
    assert result["properties"]["authorization"] == "[redacted]"
    assert result["properties"]["nested"]["access_token"] == "[redacted]"
    assert result["properties"]["nested"]["safe"] == "keep me"
    assert "eyJhbGciOiJIUzI1NiJ9" not in result["properties"]["free_text"]
    # Conversation content is deliberately preserved: full capture is the
    # documented choice for this project.
    assert result["properties"]["messages"][0]["content"] == "Ekle-birak ne zaman?"


def test_before_send_adds_ai_cost_properties_from_token_prices():
    event = {
        "event": "$ai_generation",
        "properties": {
            "$ai_input_tokens": 512,
            "$ai_output_tokens": 128,
            "$ai_input_token_price": 0.0000005078125,
            "$ai_output_token_price": 0.000001015625,
            "$ai_latency": 37.6,
        },
    }

    result = ph_client._before_send(event)

    assert result["properties"]["$ai_input_cost_usd"] == 0.00026
    assert result["properties"]["$ai_output_cost_usd"] == 0.00013
    assert result["properties"]["$ai_total_cost_usd"] == 0.00039
    assert result["properties"]["$ai_latency"] == 37.6


def test_before_send_does_not_add_ai_costs_without_prices():
    event = {
        "event": "$ai_generation",
        "properties": {"$ai_input_tokens": 512, "$ai_output_tokens": 128},
    }

    result = ph_client._before_send(event)

    assert "$ai_input_cost_usd" not in result["properties"]
    assert "$ai_output_cost_usd" not in result["properties"]
    assert "$ai_total_cost_usd" not in result["properties"]


def test_otlp_logs_scrub_secrets_but_preserve_content(monkeypatch):
    from app.observability import logs

    emitted = []

    class _Logger:
        def emit(self, **record):
            emitted.append(record)

    monkeypatch.setattr(logs, "_setup", lambda: _Logger())
    original = {
        "event": "agent_tool_call_error",
        "level": "warning",
        "authorization": "Bearer abc.def.ghi",
        "detail": "The student's complete tool result",
    }

    returned = logs.posthog_log_processor(None, "warning", original)

    assert returned is original
    assert original["authorization"] == "Bearer abc.def.ghi"
    assert emitted[0]["attributes"]["authorization"] == "[redacted]"
    assert emitted[0]["attributes"]["detail"] == "The student's complete tool result"


def test_tool_span_state_scrubs_secrets_but_preserves_complete_content():
    from app.agents.scholar.hooks import _span_state

    state = _span_state(
        {
            "password": "hunter2",
            "messages": [{"role": "user", "content": "Show my complete transcript"}],
        }
    )

    assert '"password": "[redacted]"' in state
    assert "hunter2" not in state
    assert "Show my complete transcript" in state


# --- the route wiring -------------------------------------------------------


async def test_chat_turn_emits_one_trace_and_one_turn_event(client, monkeypatch):
    """A turn through the real route must produce exactly one trace."""
    captured = []
    monkeypatch.setattr(
        "app.observability.turns.capture",
        lambda event, **kw: captured.append((event, kw)),
    )

    headers = auth_header(new_user_id())
    assert (await client.post("/api/v1/agents/provision", headers=headers)).status_code == 201
    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "merhaba"}], "session_id": "obs-trace-1"},
    )
    assert response.status_code == 200

    traces = [kw for event, kw in captured if event == "$ai_trace"]
    turns = [kw for event, kw in captured if event == "chat_turn_completed"]
    assert len(traces) == 1, f"expected one trace per turn, got {len(traces)}"
    assert len(turns) == 1

    assert traces[0]["$ai_session_id"] == "obs-trace-1"
    assert traces[0]["$ai_span_name"] == "chat_turn"
    assert "$ai_is_error" not in traces[0]
    assert turns[0]["outcome"] == "completed"
    assert turns[0]["chat_session_id"] == "obs-trace-1"
    # The trace id ties the turn event, the trace, and every generation and
    # span inside it together.
    assert turns[0]["trace_id"] == traces[0]["$ai_trace_id"]


# --- request context binding ------------------------------------------------


async def test_middleware_binds_identity_and_ignores_spoofed_distinct_id(client, monkeypatch):
    """Identity comes from the verified JWT; the header is never trusted.

    Honouring `X-POSTHOG-DISTINCT-ID` would let any caller attribute their
    events, LLM traces and exceptions to any other student.
    """
    seen: dict = {}

    def fake_identify(distinct_id):
        seen["distinct_id"] = distinct_id

    def fake_session(session_id):
        seen["session_id"] = session_id

    monkeypatch.setattr(get_settings(), "posthog_api_key", "phc_test", raising=False)
    ph_client.get_posthog.cache_clear()
    monkeypatch.setattr(ph_client, "get_posthog", lambda: object())
    monkeypatch.setattr("posthog.identify_context", fake_identify)
    monkeypatch.setattr("posthog.set_context_session", fake_session)

    user_id = new_user_id()
    headers = {
        **auth_header(user_id),
        "X-POSTHOG-DISTINCT-ID": "some-other-student",
        "X-POSTHOG-SESSION-ID": "replay-session-42",
    }
    assert (await client.get("/api/v1/agents/me", headers=headers)).status_code in (200, 404)

    assert seen.get("distinct_id") == str(user_id), "distinct id must come from the JWT subject"
    assert seen["distinct_id"] != "some-other-student"
    # The session id is not an identity claim, so it is taken from the header —
    # it is what links a backend trace to the browser's session replay.
    assert seen.get("session_id") == "replay-session-42"


async def test_structlog_binds_request_context():
    """Every log line inside a request carries the request id and user id.

    `merge_contextvars` has been first in the structlog chain all along with
    nothing ever bound to it; this asserts the middleware finally does.
    """
    import structlog

    from app.observability.middleware import ObservabilityMiddleware

    seen: dict = {}

    async def inner_app(scope, receive, send):
        seen.update(structlog.contextvars.get_contextvars())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    user_id = new_user_id()
    token = auth_header(user_id)["Authorization"]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents/me",
        "headers": [
            (b"authorization", token.encode()),
            (b"x-posthog-session-id", b"replay-session-7"),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await ObservabilityMiddleware(inner_app)(scope, receive, lambda message: _noop())

    assert seen["user_id"] == str(user_id)
    assert seen["session_id"] == "replay-session-7"
    assert seen["path"] == "/api/v1/agents/me"
    assert seen["method"] == "GET"
    assert seen["request_id"]
    # Cleared on the way out, so one request's identity cannot bleed into the
    # next one served by the same worker.
    assert "user_id" not in structlog.contextvars.get_contextvars()


async def _noop():
    return None


# --- usage metrics must survive the credential filter ------------------------


def test_before_send_preserves_numeric_usage_alongside_redaction():
    """The regression the audit found: a third of turns reported `[redacted]`.

    Every one of these property names contains the substring "token", which is
    also what a credential filter has to look for. The value is what separates
    them, and a measurement is never a string.
    """
    event = {
        "event": "chat_turn_completed",
        "properties": {
            "input_tokens": 1024,
            "output_tokens": 256,
            "total_tokens": 1280,
            # Zero is a measurement. Dropping it computes averages over the
            # wrong denominator, which is worse than reporting nothing.
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "time_to_first_token": 1.75,
            "tokens_scholar": 900,
            "tokens_tool_compressor": 380,
            "cost_scholar": 0.0004,
            "$ai_input_tokens": 1024,
            "$ai_input_token_price": 0.0000005,
            # Not measurements, and they must still go.
            "metu_password": "hunter2",
            "access_token": "phx_0123456789abcdef0",
            "metrics": {"total_tokens": 42, "api_key": "sk-0123456789abcdefgh"},
        },
    }

    properties = ph_client._before_send(event)["properties"]

    assert properties["input_tokens"] == 1024
    assert properties["output_tokens"] == 256
    assert properties["total_tokens"] == 1280
    assert properties["cache_read_tokens"] == 0
    assert properties["reasoning_tokens"] == 0
    assert properties["time_to_first_token"] == 1.75
    assert properties["tokens_scholar"] == 900
    assert properties["tokens_tool_compressor"] == 380
    assert properties["cost_scholar"] == 0.0004
    assert properties["$ai_input_tokens"] == 1024
    assert properties["$ai_input_token_price"] == 0.0000005
    assert properties["metu_password"] == "[redacted]"
    assert properties["access_token"] == "[redacted]"
    # Nested payloads get the same treatment in both directions.
    assert properties["metrics"]["total_tokens"] == 42
    assert properties["metrics"]["api_key"] == "[redacted]"


def test_before_send_still_redacts_usage_shaped_keys_holding_non_numbers():
    """The allowlist is by name *and* value, so it cannot be used as a bypass."""
    event = {
        "event": "chat_turn_completed",
        "properties": {
            "input_tokens": "phx_0123456789abcdef0",
            "total_tokens": {"nested": "object"},
            "tokens_scholar": ["list"],
            # A flag is not a measurement, and `bool` is an `int` subclass.
            "output_tokens": True,
            "time_to_first_token": float("inf"),
        },
    }

    properties = ph_client._before_send(event)["properties"]

    assert properties["input_tokens"] == "[redacted]"
    assert properties["total_tokens"] == "[redacted]"
    assert properties["tokens_scholar"] == "[redacted]"
    assert properties["output_tokens"] == "[redacted]"
    assert properties["time_to_first_token"] == "[redacted]"


@pytest.mark.parametrize(
    "event",
    [None, "not an event", {"event": "x"}, {"event": "x", "properties": "not a dict"}, {"properties": None}],
)
def test_before_send_survives_malformed_telemetry(event):
    """A throw in before_send drops the whole queue, so it may never raise."""
    assert ph_client._before_send(event) is event


# --- exceptions carry request context, and only once ------------------------


def test_report_exception_reports_once_per_exception(monkeypatch):
    captured = []
    monkeypatch.setattr(
        ph_client,
        "capture_exception",
        lambda exc, **kw: captured.append((exc, kw)),
    )
    monkeypatch.setattr(ph_client, "exception_already_reported", lambda exc: bool(getattr(exc, "seen", False)))

    error = RuntimeError("upstream refused")
    assert ph_client.report_exception(error, handler="first") is True
    error.seen = True
    assert ph_client.report_exception(error, handler="second") is False
    assert [kw["handler"] for _, kw in captured] == ["first"]


async def test_middleware_reports_route_exceptions_with_request_context(monkeypatch):
    """Starlette runs the app's own 500 handler outside this middleware.

    Reporting from there loses the request id, the user and every tag, which is
    why almost no Python issue carried a request id. The middleware reports
    first, while the context is alive.
    """
    from app.observability import middleware as mw

    reports: list[tuple] = []
    monkeypatch.setattr("app.observability.client.report_exception", lambda exc, **kw: reports.append((exc, kw)))
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: None)

    async def failing_app(scope, receive, send):
        scope["path_params"] = {"session_id": "abc"}
        scope["endpoint"] = lambda: None
        scope["endpoint"].__name__ = "get_session"
        raise RuntimeError("route exploded")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/sessions/abc",
        "headers": [(b"x-request-id", b"req-42")],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(RuntimeError):
        await mw.ObservabilityMiddleware(failing_app)(scope, receive, lambda message: _noop())

    assert len(reports) == 1
    _, properties = reports[0]
    assert properties["request_id"] == "req-42"
    assert properties["route"] == "/api/v1/sessions/{session_id}"
    assert properties["operation"] == "get_session"
    assert properties["handler"] == "observability_middleware"


# --- one outcome event per request ------------------------------------------


async def test_request_outcome_event_carries_the_route_template(monkeypatch):
    from app.observability import middleware as mw

    events: list[tuple] = []
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: events.append((event, kw)))

    async def app(scope, receive, send):
        scope["path_params"] = {"session_id": "abc"}
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict] = []
    scope = {"type": "http", "method": "DELETE", "path": "/api/v1/sessions/abc", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await mw.ObservabilityMiddleware(app)(scope, receive, send)

    outcome = next(kw for event, kw in events if event == "api_request_completed")
    assert outcome["route"] == "/api/v1/sessions/{session_id}"
    assert outcome["method"] == "DELETE"
    assert outcome["status_code"] == 204
    assert outcome["outcome"] == "success"
    assert outcome["request_id"]
    assert outcome["duration_seconds"] >= 0

    # The correlation id is echoed so the browser and the proxy can log the id
    # the broker actually used.
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"].decode() == outcome["request_id"]


async def test_successful_health_probes_are_not_reported(monkeypatch):
    from app.observability import middleware as mw

    events: list[tuple] = []
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: events.append((event, kw)))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw.ObservabilityMiddleware(app)(
        {"type": "http", "method": "GET", "path": "/health", "headers": []},
        receive,
        lambda message: _noop(),
    )

    assert not [event for event, _ in events if event == "api_request_completed"]


async def test_failing_health_probes_are_reported(monkeypatch):
    """It is the *successful* probes that are noise."""
    from app.observability import middleware as mw

    events: list[tuple] = []
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: events.append((event, kw)))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw.ObservabilityMiddleware(app)(
        {"type": "http", "method": "GET", "path": "/health", "headers": []},
        receive,
        lambda message: _noop(),
    )

    outcome = next(kw for event, kw in events if event == "api_request_completed")
    assert outcome["status_code"] == 503
    assert outcome["outcome"] == "unexpected_failure"


# --- every started turn ends on exactly one outcome -------------------------


def test_a_cancelled_turn_reports_cancelled_rather_than_nothing(monkeypatch):
    captured = []
    monkeypatch.setattr("app.observability.turns.capture", lambda event, **kw: captured.append((event, kw)))

    observation = TurnObservation(trace_id="t-3", user_id="u-3", session_id="s-3")
    observation.cancelled("client_disconnected")
    observation.finish()

    turn = next(kw for event, kw in captured if event == "chat_turn_completed")
    assert turn["outcome"] == "cancelled"
    assert turn["result"] == "cancelled"
    trace = next(kw for event, kw in captured if event == "$ai_trace")
    # A student closing the tab is not an error.
    assert "$ai_is_error" not in trace


def test_a_failed_turn_is_not_downgraded_to_cancelled(monkeypatch):
    monkeypatch.setattr("app.observability.turns.capture", lambda event, **kw: None)

    observation = TurnObservation(trace_id="t-4", user_id="u-4")
    observation.run_failed("model refused", "ModelProviderError")
    observation.cancelled("client_disconnected")

    assert observation.outcome == "run_error"


async def test_a_disconnected_stream_still_reports_and_releases_the_lock():
    """The failure the audit named: reporting sat after a `yield`.

    When the student closes the tab that `yield` raises, so the turn was never
    reported and the turn lock was never released.
    """
    import asyncio

    from app.api.v1.chat import _TurnCleanup

    released = asyncio.Event()
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(heartbeat_stop.wait())

    async def finalize():
        released.set()

    observation = TurnObservation(trace_id="t-5", user_id="u-5")
    cleanup = _TurnCleanup(observation, heartbeat, heartbeat_stop, finalize, "u-5")

    async def stream():
        try:
            yield b"chunk"
            yield b"data: [DONE]\n\n"
            await cleanup.run()
        except (asyncio.CancelledError, GeneratorExit):
            observation.cancelled("client_disconnected")
            cleanup.detach()
            raise
        finally:
            cleanup.detach()

    generator = stream()
    assert await generator.__anext__() == b"chunk"
    # The consumer goes away mid-stream, exactly as a closed tab does.
    await generator.aclose()

    assert observation.outcome == "cancelled"
    assert observation._finished
    await asyncio.wait_for(released.wait(), timeout=2)


async def test_a_failing_heartbeat_does_not_prevent_finalization():
    """`await heartbeat` used to be the first statement in the `finally`."""
    import asyncio

    from app.api.v1.chat import _TurnCleanup

    released = asyncio.Event()
    heartbeat_stop = asyncio.Event()

    async def heartbeat_that_dies():
        raise RuntimeError("lease renewal failed")

    async def finalize():
        released.set()

    observation = TurnObservation(trace_id="t-6", user_id="u-6")
    cleanup = _TurnCleanup(
        observation,
        asyncio.create_task(heartbeat_that_dies()),
        heartbeat_stop,
        finalize,
        "u-6",
    )

    await cleanup.run()

    assert observation._finished
    assert released.is_set()


async def test_turn_cleanup_runs_exactly_once():
    import asyncio

    from app.api.v1.chat import _TurnCleanup

    calls = []
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(heartbeat_stop.wait())

    async def finalize():
        calls.append(1)

    cleanup = _TurnCleanup(TurnObservation(trace_id="t-7", user_id="u-7"), heartbeat, heartbeat_stop, finalize, "u-7")
    await cleanup.run()
    cleanup.detach()
    await cleanup.run()

    assert calls == [1]


async def test_the_browsers_request_id_survives_the_whole_backend_request(client, monkeypatch):
    """End to end through the real app: header in, header out, event tagged.

    This is the join key. The browser mints it, the Next.js proxy forwards it,
    and everything the broker records for that request — the outcome event, its
    log line, and any exception — carries the same value.
    """
    from app.observability import middleware as mw

    events: list[tuple] = []
    monkeypatch.setattr("app.observability.client.capture", lambda event, **kw: events.append((event, kw)))

    # The middleware's own logger rather than a reconfigured structlog chain:
    # `configure_logging` caches bound loggers, so reconfiguring globally would
    # miss this call site and leak the new chain into every later test.
    logged: list[tuple] = []
    monkeypatch.setattr(
        mw,
        "logger",
        type("L", (), {"info": lambda self, event, **kw: logged.append((event, kw))})(),
    )

    headers = {**auth_header(new_user_id()), "X-Request-ID": "browser-req-1"}
    response = await client.get("/api/v1/agents/me", headers=headers)

    assert response.headers["x-request-id"] == "browser-req-1"

    outcome = next(kw for event, kw in events if event == "api_request_completed")
    assert outcome["request_id"] == "browser-req-1"
    assert outcome["route"] == "/api/v1/agents/me"
    assert outcome["method"] == "GET"
    assert outcome["status_code"] == response.status_code

    completion = next(fields for event, fields in logged if event == "api_request_completed")
    assert completion["request_id"] == "browser-req-1"
    assert completion["route"] == "/api/v1/agents/me"
    assert completion["outcome"] == outcome["outcome"]
