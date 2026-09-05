"""Local, rate-limited diagnostics for telemetry that cannot report itself.

An analytics pipeline has one failure mode nothing else has: when it breaks, the
report of the breakage travels down the broken pipe. Export failures, a full
queue, a client that would not initialise and a production deployment with no
key configured are all invisible in PostHog *precisely because* of what went
wrong.

So they are written to stdout instead, where the container log collector picks
them up. Rate limiting is not politeness — a failing exporter fails once per
batch, and an unthrottled line per batch turns a degraded sink into a disk-full
incident.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# One line per code per window. Long enough that a hot failure loop is quiet,
# short enough that a human watching logs sees the problem is ongoing.
DIAGNOSTIC_INTERVAL_SECONDS = 60.0

_lock = threading.Lock()
_last_emitted: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def _should_emit(code: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _lock:
        last = _last_emitted.get(code)
        if last is not None and now - last < DIAGNOSTIC_INTERVAL_SECONDS:
            _suppressed[code] = _suppressed.get(code, 0) + 1
            return False, 0
        _last_emitted[code] = now
        return True, _suppressed.pop(code, 0)


def report_local(code: str, **fields: Any) -> None:
    """Log a telemetry failure to stdout only, at most once per window.

    ``telemetry="local"`` is the marker the structlog processor checks before
    forwarding a record over OTLP: shipping a report of a broken exporter
    through that same exporter is how a stuck pipeline becomes an infinite one.
    """
    emit, suppressed = _should_emit(code)
    if not emit:
        return
    try:
        from app.logging import get_logger

        get_logger("app.observability.diagnostics").warning(
            code,
            telemetry="local",
            **({"suppressed_since_last": suppressed} if suppressed else {}),
            **fields,
        )
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass


def reset() -> None:
    """Clear the rate-limiter. For tests only."""
    with _lock:
        _last_emitted.clear()
        _suppressed.clear()
