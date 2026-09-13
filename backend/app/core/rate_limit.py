"""Per-account request windows, built but deliberately not switched on.

The project is pre-launch, so the API still runs without limits and the admin
panel carries the switch that changes that. This module is the mechanism those
limits will use: a sliding one-minute window per account and per scope, counted
in-process, with the same shape as the campus credential throttle — including
why it is per-process: the API runs as a single uvicorn process, and if that
ever changes this becomes a per-worker limit, which is weaker than intended
rather than absent.

Scopes name *why* a route is expensive, not which route it is: ``chat`` counts
LLM runs, ``catalog`` counts the bulk campus/catalog reads. A scope that is not
tracked here is a bug in the caller, and the dependency refuses it at import
time rather than silently letting requests through.
"""

import math
import time
from collections import deque

# The window is a minute because the limits are expressed per minute; short
# enough to shed a burst, long enough that honest use never notices.
WINDOW_SECONDS = 60

# Above this many tracked accounts in one scope, the oldest idle windows are
# dropped. Without a bound, a stream of new user ids would itself be a slow
# memory leak. A dropped window only forgives requests already made.
MAX_TRACKED_USERS = 20_000

_windows: dict[str, dict[str, deque[float]]] = {}


def _prune(scope_windows: dict[str, deque[float]], now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    for key in list(scope_windows):
        stamps = scope_windows[key]
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if not stamps:
            del scope_windows[key]
    if len(scope_windows) > MAX_TRACKED_USERS:
        overflow = len(scope_windows) - MAX_TRACKED_USERS
        for key in sorted(scope_windows, key=lambda item: scope_windows[item][-1])[:overflow]:
            del scope_windows[key]


def retry_after_seconds(scope: str, user_id: str, per_minute: int) -> int | None:
    """Seconds to wait, or ``None`` when this request is allowed.

    Counts the request when it allows it, so a caller cannot ask permission and
    then not use it. ``per_minute <= 0`` means the scope is unlimited.
    """
    if per_minute <= 0:
        return None
    now = time.monotonic()
    scope_windows = _windows.setdefault(scope, {})
    _prune(scope_windows, now)

    stamps = scope_windows.get(user_id)
    if stamps is not None and len(stamps) >= per_minute:
        return max(1, math.ceil(WINDOW_SECONDS - (now - stamps[0])))
    if stamps is None:
        stamps = scope_windows[user_id] = deque()
    stamps.append(now)
    return None


def reset() -> None:
    """Forget every recorded request. For tests; nothing in the app calls it."""
    _windows.clear()
