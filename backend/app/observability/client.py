"""The single PostHog client for the broker, and safe wrappers around it.

Four rules shape this module.

*Never break the broker.* PostHog is optional. With no ``POSTHOG_API_KEY`` the
client is ``None`` and every helper below is a no-op, so a developer without a
PostHog project — and CI, which has none — runs an unmodified service. Nothing
in here is allowed to raise into a caller: an analytics sink that can fail a
chat turn is worse than no analytics sink.

*Never fail silently.* The corollary of the rule above is that a
mis-configuration looks exactly like a working install. A missing key, a client
that would not build, a rejected queue and a failing exporter are therefore all
announced through ``app.observability.diagnostics``, which writes to stdout —
the one channel that still works when the telemetry pipeline is the thing that
is broken.

*Never leak private academic content or a secret.* ``before_send`` is the last
gate every SDK event passes through, and it scrubs credential-shaped values and
academic payloads regardless of which call site produced them. Aggregate usage,
latency and safe type labels remain available; prompts, completions, tool
content, transcripts and exception bodies do not.

*Never redact a measurement.* The rule above used to cost real data: the word
"token" appears in ``input_tokens``, ``total_tokens`` and ``tokens_scholar``
just as it does in ``access_token``, so more than a third of chat turns
reported ``[redacted]`` where their usage should have been. Usage and price
fields are now allowed through by an explicit name allowlist *and* a value
check — a credential is never a finite number, so nothing that is redacted
today becomes readable because of it.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Any

from posthog import Posthog

from app.config import get_settings
from app.logging import get_logger
from app.observability.diagnostics import report_local
from app.observability.runtime import service_properties

logger = get_logger(__name__)

MISSING_KEY_MESSAGE = (
    "POSTHOG_API_KEY variable required by PostHog is missing or un-configured, "
    "this causes events to be silently missed. This error stops appearing once "
    "POSTHOG_API_KEY is configured"
)

# Property names whose values are redacted no matter where they came from.
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api_?key|authorization|cookie|credential|jwt|bearer)",
    re.IGNORECASE,
)
# Value shapes that are credentials wherever they appear: bearer headers, JWTs,
# and PostHog's own personal API keys.
_SECRET_VALUE_PATTERN = re.compile(
    r"((?:Bearer|Basic)\s+[\w\-.=]+|eyJ[\w\-]{8,}\.[\w\-]{8,}\.[\w\-]+|ph[cx]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,})"
)
# Usage and price fields whose names collide with the credential pattern. The
# families, in order: PostHog's own ``$ai_*`` metrics; the flat usage counts
# Agno reports for a turn; the per-model-role totals ``turns.py`` derives from
# ``RunMetrics.details``; and the per-token prices this project supplies because
# PostHog has no price table for the model.
_USAGE_KEY_PATTERN = re.compile(
    r"^\$ai_[a-z0-9_]*token[a-z0-9_]*$"
    r"|^(?:input|output|total|prompt|completion|cache_read|cache_write|cache_creation"
    r"|reasoning|audio|embedding|web_search)_tokens$"
    r"|^tokens_[a-z0-9_]+$"
    r"|^(?:input|output|total)_token_price$"
    r"|^time_to_first_token$"
    r"|^tokens_per_second$",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"
_ACADEMIC_REDACTED = "[academic content redacted]"
_ROUTE_REDACTED = "[route redacted]"
_SAFE_TYPE_KEYS = {"error_type", "exception_type", "$ai_error_type"}
_SAFE_TYPE_VALUE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")

# Academic context is data, not an observability dimension. These keys cover
# the PostHog AI fields, tool states, exception details and the structured
# fields used by the Scholar turn/span events. Numeric values under these keys
# remain available as aggregate measurements; strings/objects are removed.
_ACADEMIC_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:prompt|completion|messages?|content|transcript|course|courses|section|sections|surname|grade|student|academic|context|"
    r"tool[_-]?result|detail|error[_-]?(?:message|detail|body)|errors?|instructions?|answer|response|input|output)(?:$|_)|"
    r"^\$ai_(?:input|output|error|instructions?|input_state|output_state)|^\$exception_list$",
    re.IGNORECASE,
)


def _is_academic_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower()
    # Preserve safe type labels and aggregate dimensions even though their
    # names contain one of the broad words above.
    if normalized in {
        "error_type",
        "exception_type",
        "$ai_error_type",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }:
        return False
    if normalized in {
        "display_name",
        "metu_username",
        "surname_prefix",
        "department",
        "degree_level",
        "program_code",
        "campus",
    }:
        return True
    # Generic exception fields are populated with ``str(exc)`` at several
    # legacy call sites. Keep the explicit type fields for grouping, but never
    # forward the free-form error, reason, or traceback text.
    if normalized in {"error", "exception", "reason", "stack", "stacktrace"}:
        return True
    return bool(_ACADEMIC_KEY_PATTERN.search(normalized))


def _is_concrete_route_key(key: Any) -> bool:
    return isinstance(key, str) and key.strip().lower() in {"path", "url", "request_url", "route_path"}


def _is_safe_type_value(key: Any, value: Any) -> bool:
    if not isinstance(key, str) or key.strip().lower() not in _SAFE_TYPE_KEYS:
        return True
    return value is None or (
        isinstance(value, str) and _SAFE_TYPE_VALUE_PATTERN.fullmatch(value.strip()) is not None
    )

# Explicitly recorded when a provider reports no usage, so "we did not measure
# this" is distinguishable from "this cost nothing".
USAGE_UNAVAILABLE = "unavailable"


def _is_finite_number(value: Any) -> bool:
    """A measurement, not a credential.

    ``bool`` is excluded because it is an ``int`` subclass and a flag is not a
    measurement. Zero passes: a turn that used no cached tokens must report 0,
    not nothing, or the average is computed over the wrong denominator.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # JSON can contain integers too large for a float. Reject the metric
        # without aborting redaction of the rest of the event.
        return False


def _is_usage_metric(key: Any, value: Any) -> bool:
    """Whether a credential-shaped key is really a usage or price measurement."""
    return isinstance(key, str) and bool(_USAGE_KEY_PATTERN.match(key)) and _is_finite_number(value)


def _non_negative_number(properties: dict[str, Any], key: str) -> float | None:
    """Return a finite, non-negative numeric PostHog property."""
    try:
        value = float(properties[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _add_ai_cost_properties(event: Any) -> None:
    """Add explicit USD costs to generation events using configured prices.

    PostHog's OpenAI wrapper records token counts, but the broker uses a
    custom OpenAI-compatible provider whose model is not in PostHog's price
    table. The wrapper also carries our per-token prices, so this final event
    hook can emit stable cost properties after the response usage is known.
    """
    if not isinstance(event, dict) or event.get("event") != "$ai_generation":
        return
    properties = event.get("properties")
    if not isinstance(properties, dict):
        return

    costs: list[float] = []
    for tokens_key, price_key, cost_key in (
        ("$ai_input_tokens", "$ai_input_token_price", "$ai_input_cost_usd"),
        ("$ai_output_tokens", "$ai_output_token_price", "$ai_output_cost_usd"),
    ):
        tokens = _non_negative_number(properties, tokens_key)
        price = _non_negative_number(properties, price_key)
        if tokens is None or price is None:
            continue
        cost = round(tokens * price, 12)
        properties.setdefault(cost_key, cost)
        costs.append(cost)

    if costs:
        properties.setdefault("$ai_total_cost_usd", round(sum(costs), 12))


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively redact secrets and academic content from outbound telemetry."""
    # Event payloads are JSON-shaped and therefore acyclic by the time the SDK
    # accepts them. A generous ceiling avoids pathological recursion without
    # ever returning an uninspected deep value that could contain a credential.
    if depth > 20:
        return _REDACTED
    if isinstance(value, dict):
        return {
            key: (
                _ACADEMIC_REDACTED
                if isinstance(key, str)
                and key.strip().lower() in _SAFE_TYPE_KEYS
                and not _is_safe_type_value(key, item)
                else (
                _REDACTED
                if isinstance(key, str)
                and _SECRET_KEY_PATTERN.search(key)
                and not _is_usage_metric(key, item)
                else _ROUTE_REDACTED
                if _is_concrete_route_key(key)
                else (
                    item
                    if _is_academic_key(key) and isinstance(item, (int, float, bool))
                    else _ACADEMIC_REDACTED
                    if _is_academic_key(key)
                    else _scrub(item, depth + 1)
                )
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_PATTERN.sub(_REDACTED, value)
    return value


def _before_send(event: Any) -> Any:
    """Last gate before upload. Must never raise — a throw here drops the queue."""
    try:
        # posthog-python's before_send contract is a mutable event dictionary,
        # not an object with a ``properties`` attribute. Keep the small object
        # fallback for duck-typed callers, but exercise the real dictionary
        # shape in tests so SDK events cannot bypass redaction unnoticed.
        properties = event.get("properties") if isinstance(event, dict) else getattr(event, "properties", None)
        if isinstance(properties, dict):
            scrubbed = _scrub(properties)
            if isinstance(event, dict):
                event["properties"] = scrubbed
            else:
                event.properties = scrubbed
            _add_ai_cost_properties(event)
    except Exception:
        # Never upload properties we could not inspect, even if a future
        # scrubber change introduces another failure mode.
        if isinstance(event, dict):
            event["properties"] = {"redaction_failed": True}
        else:
            try:
                event.properties = {"redaction_failed": True}
            except Exception:  # pragma: no cover - immutable, non-SDK event
                return None
    return event


def _on_export_error(error: BaseException, batch: Any) -> None:
    """The SDK's upload thread failed. Report it where it can still be seen."""
    try:
        size = len(batch) if batch is not None else 0
    except TypeError:  # pragma: no cover - defensive
        size = 0
    report_local("posthog_export_failed", error=error.__class__.__name__, batch_size=size)


@lru_cache
def get_posthog() -> Posthog | None:
    """The process-wide PostHog client, or ``None`` when unconfigured."""
    settings = get_settings()

    if not settings.posthog_configured:
        if settings.posthog_debug or settings.environment == "production":
            # A production deployment with no key is a mis-configuration, not a
            # choice, and it is invisible everywhere except right here.
            report_local("posthog_not_configured", detail=MISSING_KEY_MESSAGE)
        return None

    try:
        client = Posthog(
            settings.posthog_api_key.strip(),
            host=settings.posthog_host,
            debug=settings.posthog_debug,
            # Local evaluation keeps feature-flag checks off the network on the
            # chat hot path. Without a personal key the SDK falls back to the
            # decide endpoint, which still works, just slower.
            personal_api_key=settings.posthog_personal_api_key.strip() or None,
            enable_local_evaluation=bool(settings.posthog_personal_api_key.strip()),
            # Every event, from any service, carries who produced it, where it
            # runs and which commit it is running.
            super_properties={
                **service_properties(),
                "agent_profile": settings.agent_profile,
                "agent_runtime": settings.agent_runtime,
                "agent_model": settings.agent_model,
            },
            # Unhandled exceptions anywhere in the process become issues. Rate
            # limited so a hot loop of failures cannot flood the queue.
            enable_exception_autocapture=True,
            enable_exception_autocapture_rate_limiting=True,
            # The SDK's diagnostic logger formats the exception message into
            # local stdout. Keep the PostHog issue, but do not duplicate a
            # provider response or student prompt into application logs.
            log_captured_exceptions=False,
            # A failed upload is otherwise only visible in the SDK's own stdlib
            # logger, which nothing was listening to.
            on_error=_on_export_error,
            # Exception locals can contain the Scholar prompt, transcript or
            # campus tool response. Keep the exception type/stack and aggregate
            # context, but never serialize local variable values.
            capture_exception_code_variables=False,
            code_variables_detect_secrets=True,
            code_variables_mask_url_credentials=True,
            code_variables_mask_patterns=[
                "password",
                "metu_password",
                "secret",
                "token",
                "api_key",
                "access_token",
                "authorization",
            ],
            project_root="/app",
            in_app_modules=["app"],
            # Academic prompts, completions and tool state are private student
            # data. PostHog privacy mode keeps generation metadata and token
            # usage while omitting those payloads; the outbound scrubber below
            # protects ordinary events and OTLP exception/log properties too.
            privacy_mode=True,
            enable_full_ai_capture=False,
            capture_trace_context=True,
            before_send=_before_send,
        )
    except Exception as exc:
        # A telemetry client that cannot be constructed must not stop the
        # service from starting, but it must not look like "no key" either.
        report_local("posthog_client_init_failed", error=exc.__class__.__name__)
        return None

    logger.info(
        "posthog_initialized",
        host=settings.posthog_host,
        **service_properties(),
    )
    return client


def initialize() -> Posthog | None:
    """Build the client at boot so a mis-configuration is reported at boot.

    Discovering an absent key later, as an absence of data, is the failure this
    exists to prevent.
    """
    settings = get_settings()
    production = settings.posthog_configured and settings.environment == "production"
    if production and not service_properties().get("release"):
        report_local(
            "telemetry_release_not_configured",
            detail="RELEASE is unset, so events cannot be attributed to a deploy",
        )
    return get_posthog()


def _merged_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Caller properties, over correlation ids, over nothing."""
    from app.observability.context import correlation_properties

    return {**correlation_properties(), **{k: v for k, v in properties.items() if v is not None}}


def capture(event: str, *, distinct_id: str | None = None, **properties: Any) -> None:
    """Capture an event, inheriting distinct id and session from the request context."""
    client = get_posthog()
    if client is None:
        return
    payload = _merged_properties(properties)
    try:
        if distinct_id is None:
            client.capture(event, properties=payload)
        else:
            client.capture(event, distinct_id=distinct_id, properties=payload)
    except Exception as exc:  # pragma: no cover - telemetry must never break a request
        report_local("posthog_capture_failed", event=event, error=exc.__class__.__name__)


def exception_already_reported(exc: BaseException) -> bool:
    """Whether this exact exception instance has already become an issue.

    The SDK marks captured exceptions on the instance, so this is the same
    answer it would give itself. Consulting it lets an outer handler stay a
    fallback instead of a duplicate: one failure, one issue, with the tags of
    whichever layer was closest to it.
    """
    try:
        from posthog.exception_utils import exception_is_already_captured

        return bool(exception_is_already_captured(exc))
    except Exception:  # pragma: no cover - SDK internals moved
        return hasattr(exc, "__posthog_exception_captured")


def capture_exception(exc: BaseException, *, distinct_id: str | None = None, **properties: Any) -> None:
    """Report a handled exception that would otherwise never reach autocapture."""
    client = get_posthog()
    if client is None:
        return
    payload = _merged_properties(properties)
    try:
        if distinct_id is None:
            client.capture_exception(exc, properties=payload)
        else:
            client.capture_exception(exc, distinct_id=distinct_id, properties=payload)
    except Exception as capture_error:  # pragma: no cover
        report_local("posthog_capture_exception_failed", error=capture_error.__class__.__name__)


def report_exception(exc: BaseException, *, distinct_id: str | None = None, **properties: Any) -> bool:
    """Capture ``exc`` unless something closer to it already did.

    Returns whether this call is the one that reported it, so a caller can log
    the difference between "reported here" and "already an issue".
    """
    if exception_already_reported(exc):
        return False
    capture_exception(exc, distinct_id=distinct_id, **properties)
    return True


def shutdown() -> None:
    """Flush the queue. Called from every service lifecycle; an unflushed queue
    at SIGTERM loses exactly the events explaining why the process is going away."""
    client = get_posthog()
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as exc:  # pragma: no cover
        report_local("posthog_shutdown_failed", error=exc.__class__.__name__)
