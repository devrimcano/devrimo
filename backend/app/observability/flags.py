"""Feature flags, evaluated per student.

The broker has several settings that today can only change by editing code or
env and redeploying: which campus servers get built, which agent profile runs,
which model answers. Every one of those is something you want to change *during*
an incident — a campus server started returning garbage, a model regressed — so
each gets a flag with the existing ``Settings`` value as its fallback.

Fallback is the whole design. With no PostHog key, no personal API key, or an
unreachable PostHog, every helper here returns the default it was given and the
broker behaves exactly as it does today. A flag lookup must never be able to
take the agent down.

The distinct id comes from the request context bound by ObservabilityMiddleware,
so flags are evaluated per student without threading a user id through call
signatures that have no other reason to know one.
"""

from __future__ import annotations

from typing import Any

from app.logging import get_logger
from app.observability.client import get_posthog

logger = get_logger(__name__)


def _distinct_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from posthog import contexts

        return contexts.get_context_distinct_id()
    except Exception:
        return None


def flag_enabled(key: str, *, default: bool, distinct_id: str | None = None) -> bool:
    """Boolean flag, falling back to ``default`` on any doubt."""
    client = get_posthog()
    resolved = _distinct_id(distinct_id)
    if client is None or resolved is None:
        return default
    try:
        value = client.feature_enabled(key, resolved)
    except Exception as exc:
        logger.warning("feature_flag_lookup_failed", flag=key, error=exc.__class__.__name__)
        return default
    return default if value is None else bool(value)


def flag_variant(key: str, *, default: str | None = None, distinct_id: str | None = None) -> str | None:
    """Multivariate flag value, e.g. which agent profile to run."""
    client = get_posthog()
    resolved = _distinct_id(distinct_id)
    if client is None or resolved is None:
        return default
    try:
        value = client.get_feature_flag(key, resolved)
    except Exception as exc:
        logger.warning("feature_flag_lookup_failed", flag=key, error=exc.__class__.__name__)
        return default
    if value is None or value is False:
        return default
    return str(value) if not isinstance(value, str) else value


def flag_payload(key: str, *, default: Any = None, distinct_id: str | None = None) -> Any:
    """JSON payload attached to a flag, e.g. a model id or a tool allowlist."""
    client = get_posthog()
    resolved = _distinct_id(distinct_id)
    if client is None or resolved is None:
        return default
    try:
        payload = client.get_feature_flag_payload(key, resolved)
    except Exception as exc:
        logger.warning("feature_flag_lookup_failed", flag=key, error=exc.__class__.__name__)
        return default
    return default if payload is None else payload


def int_payload(key: str, *, default: int, distinct_id: str | None = None) -> int:
    """A numeric tuning knob delivered as a flag payload."""
    payload = flag_payload(key, default=None, distinct_id=distinct_id)
    try:
        if payload is None:
            return default
        if isinstance(payload, dict):
            payload = payload.get("value")
        return int(payload)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# --- The flags this service reads. Names are the analytics contract. --------

FLAG_CAMPUS_TOOLS = "campus-tools-enabled"
FLAG_AGENT_MODEL = "agent-model"
FLAG_TOOL_CALL_LIMIT = "scholar-tool-call-limit"
FLAG_HISTORY_RUNS = "scholar-history-runs"


def enabled_campus_tool_ids(default: list[str], *, distinct_id: str | None = None) -> list[str]:
    """Which campus servers may be built for this student.

    The remote kill switch for a misbehaving campus server. Payload shape is
    ``{"tools": ["sais", "course_info"]}``; anything else is ignored in favour
    of the student's own configured set, because a malformed flag must not
    silently take every tool away.
    """
    payload = flag_payload(FLAG_CAMPUS_TOOLS, default=None, distinct_id=distinct_id)
    if not isinstance(payload, dict):
        return default
    allowed = payload.get("tools")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        logger.warning("feature_flag_payload_ignored", flag=FLAG_CAMPUS_TOOLS)
        return default
    return [tool_id for tool_id in default if tool_id in allowed]
