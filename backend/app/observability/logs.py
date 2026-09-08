"""Ship structlog output to PostHog Logs over OTLP.

There is a trap here worth naming. ``app.logging`` uses
``structlog.PrintLoggerFactory``, which writes straight to stdout and never
touches the stdlib ``logging`` module — so the usual advice, "attach an OTel
``LoggingHandler`` to the root logger", captures nothing in this service.

The forwarding therefore happens as a structlog *processor*. It emits each
event to an OTel logger and returns the event dict untouched, so stdout stays
byte-identical and container log collection is unaffected. Every existing
``logger.info/warning/error`` call site is covered without being edited, and
because ``merge_contextvars`` runs first in the chain, each record already
carries the request id, user id and session bound by ObservabilityMiddleware.

Two things this module has to be careful about.

*Structure must survive.* The previous version kept only ``str``/``int``/
``float``/``bool`` attributes, so every list and dict — the tool arguments, the
source ids, the counts by kind — was dropped on the way out. OTLP attributes
really are scalar, so collections are serialised to bounded JSON instead of
discarded.

*It must not feed itself.* A log line emitted while exporting a log line would
export a log line. Records from the telemetry stack itself go to stdout-only
local diagnostics, and a re-entrancy guard covers anything that slips past that
rule.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from app.config import get_settings
from app.observability.client import _scrub
from app.observability.diagnostics import report_local
from app.observability.llm import current_trace_id
from app.observability.runtime import environment, release, service_name

# Structlog level names -> OTel severity. OTel wants both a number and a text.
_SEVERITY = {
    "critical": (21, "FATAL"),
    "exception": (17, "ERROR"),
    "error": (17, "ERROR"),
    "warning": (13, "WARN"),
    "warn": (13, "WARN"),
    "info": (9, "INFO"),
    "debug": (5, "DEBUG"),
    "notset": (0, "UNSPECIFIED"),
}

# A single OTLP attribute is a log line, not a payload dump. Long enough to keep
# a tool-argument object or a list of source ids intact, short enough that one
# pathological record cannot become the batch.
MAX_ATTRIBUTE_CHARS = 4000
MAX_COLLECTION_ITEMS = 50

_logger_provider = None
_otel_logger = None
_failed = False
_reentrant = threading.local()


def _setup() -> Any:
    """Build the OTLP logger provider once. Returns ``None`` when unavailable."""
    global _logger_provider, _otel_logger, _failed
    if _otel_logger is not None or _failed:
        return _otel_logger

    settings = get_settings()
    if not settings.posthog_configured:
        _failed = True
        return None

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource

        provider = LoggerProvider(
            resource=Resource.create(
                {
                    # The worker and the broker are different services. Labelling
                    # both "devrimo-broker" made a worker outage unfindable.
                    SERVICE_NAME: service_name(),
                    SERVICE_VERSION: release() or settings.agent_profile,
                    "deployment.environment": environment(),
                    "devrimo.agent_profile": settings.agent_profile,
                    "devrimo.agent_runtime": settings.agent_runtime,
                }
            )
        )
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=f"{settings.posthog_host.rstrip('/')}/i/v1/logs",
                    headers={"Authorization": f"Bearer {settings.posthog_api_key}"},
                )
            )
        )
        set_logger_provider(provider)
        _logger_provider = provider
        _otel_logger = provider.get_logger("devrimo")
        return _otel_logger
    except Exception as exc:
        # No OTLP exporter installed, or a bad host. Logging to stdout must
        # keep working regardless, so this is latched off rather than retried
        # on every single log line — but it is no longer latched off silently.
        _failed = True
        report_local("otlp_logs_unavailable", error=exc.__class__.__name__)
        return None


def _attribute_value(value: Any) -> Any:
    """One OTLP attribute: a scalar, or a bounded JSON rendering of a collection."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= MAX_ATTRIBUTE_CHARS else value[:MAX_ATTRIBUTE_CHARS] + "…"
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = list(value)[:MAX_COLLECTION_ITEMS]
    elif isinstance(value, dict):
        value = dict(list(value.items())[:MAX_COLLECTION_ITEMS])
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - default=str covers almost everything
        rendered = str(value)
    if len(rendered) > MAX_ATTRIBUTE_CHARS:
        rendered = rendered[:MAX_ATTRIBUTE_CHARS] + "…"
    return rendered


def _attributes(safe_event: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for key, value in safe_event.items():
        if key in ("event", "level", "timestamp"):
            continue
        rendered = _attribute_value(value)
        if rendered is not None:
            attributes[key] = rendered
    return attributes


def posthog_log_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor: forward, then hand the event on unchanged."""
    # Diagnostics about the telemetry pipeline are deliberately stdout-only:
    # exporting the report of a broken exporter through that exporter is how a
    # degraded sink becomes an infinite loop.
    if event_dict.get("telemetry") == "local":
        return event_dict
    if getattr(_reentrant, "active", False):
        return event_dict

    otel_logger = _setup()
    if otel_logger is None:
        return event_dict

    _reentrant.active = True
    try:
        from opentelemetry._logs import SeverityNumber

        # OTLP bypasses the PostHog SDK and therefore its before_send hook.
        # Apply the same secret-only scrubber here while leaving the dictionary
        # returned to structlog untouched, so local stdout remains byte-identical.
        safe_event = _scrub(event_dict)
        if not isinstance(safe_event, dict):  # pragma: no cover - event_dict is typed as a dict
            return event_dict

        level = str(safe_event.get("level", "info")).lower()
        number, text = _SEVERITY.get(level, (9, "INFO"))

        attributes = _attributes(safe_event)
        # Links this log line to the AI trace it happened inside, so a failed
        # turn's logs are reachable from the trace and vice versa.
        trace_id = current_trace_id.get()
        if trace_id:
            attributes["$ai_trace_id"] = trace_id
        if "user_id" in safe_event:
            # PostHog links logs to a person through this property name.
            attributes["distinct_id"] = str(safe_event["user_id"])

        otel_logger.emit(
            body=str(safe_event.get("event", "")),
            severity_number=SeverityNumber(number),
            severity_text=text,
            attributes=attributes,
        )
    except Exception as exc:  # a log sink must never break a log call
        report_local("otlp_log_emit_failed", error=exc.__class__.__name__)
    finally:
        _reentrant.active = False
    return event_dict


def shutdown() -> None:
    """Flush buffered log records. Safe to call when never set up."""
    if _logger_provider is None:
        return
    try:
        _logger_provider.shutdown()
    except Exception as exc:  # pragma: no cover
        report_local("otlp_logs_shutdown_failed", error=exc.__class__.__name__)


def stdlib_level(name: str) -> int:
    return getattr(logging, name.upper(), logging.INFO)
