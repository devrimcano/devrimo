"""Structured logging, and the bridge that stops stdlib records disappearing.

``structlog`` with a ``PrintLoggerFactory`` is the application's logger, and
every ``app.*`` module uses it. The standard library's ``logging`` module is a
second, parallel channel that this service does not write to but its
dependencies do — SQLAlchemy, asyncpg, the OTLP exporter and the PostHog SDK
itself all report their problems there. Those records used to be formatted to
stdout and go no further, which is why a full PostHog queue or a failing export
was invisible in PostHog: the one place the SDK reports it is the one channel
nothing was forwarding.

The bridge forwards standard-library warnings and errors into the structlog
chain, so they reach PostHog Logs alongside everything else. Records from the
telemetry stack are the exception: they go to rate-limited stdout diagnostics
instead, because forwarding "the exporter failed" through the exporter is a
loop, not a report.
"""

import logging
import sys

import structlog

# Loggers whose output describes the telemetry pipeline itself. Forwarding
# these over OTLP is the recursion this module exists to avoid.
TELEMETRY_LOGGER_PREFIXES = ("posthog", "opentelemetry", "urllib3")

# Below this, stdlib records stay on stdout. Dependencies are chatty at INFO and
# a log pipeline is not a place to find out how chatty.
FORWARD_FROM_LEVEL = logging.WARNING


class StdlibBridgeHandler(logging.Handler):
    """Forward standard-library records to PostHog Logs.

    Deliberately *not* by re-emitting through a structlog logger: stdout
    already receives these records from the handler ``basicConfig`` installed,
    and a second copy would make every dependency warning appear twice. The
    OTLP processor is called directly instead, with the request context merged
    in by hand because ``merge_contextvars`` only runs for structlog call sites.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._forward(record)
        except Exception:  # pragma: no cover - a log bridge must never raise
            pass

    def _forward(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken format string
            message = str(record.msg)

        if record.name.split(".")[0] in TELEMETRY_LOGGER_PREFIXES:
            # "The queue is full" and "error uploading" arrive here. They are
            # the reports that must not travel down the pipe they describe.
            if record.levelno >= FORWARD_FROM_LEVEL:
                from app.observability.diagnostics import report_local

                report_local(
                    f"telemetry_sdk_{record.levelname.lower()}",
                    logger=record.name,
                    detail=message,
                )
            return

        if record.levelno < FORWARD_FROM_LEVEL:
            return

        from app.observability.logs import posthog_log_processor

        level = "error" if record.levelno >= logging.ERROR else "warning"
        event = {
            **structlog.contextvars.get_contextvars(),
            "event": message,
            "level": level,
            "logger": record.name,
            "source": "stdlib_logging",
        }
        if record.exc_info:
            event["exception_type"] = getattr(record.exc_info[0], "__name__", None)
        posthog_log_processor(None, level, event)


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    from app.observability.logs import posthog_log_processor

    root = logging.getLogger()
    if not any(isinstance(handler, StdlibBridgeHandler) for handler in root.handlers):
        root.addHandler(StdlibBridgeHandler(level=FORWARD_FROM_LEVEL))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Forwards to PostHog Logs and returns the event untouched, so the
            # JSON written to stdout below is unchanged. Placed after the
            # level and timestamp processors so it sees both, and before the
            # renderer so it sees a dict rather than a string.
            posthog_log_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
