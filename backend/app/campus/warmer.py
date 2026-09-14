"""Shared pacing helpers for the catalog workers.

The overnight raw-cache warmer this module used to run is gone: shared catalog
facts now come from the reviewed, published catalog, and its own worker owns
the import schedule and the METU request budget. Keeping the old pass around
was worse than dead code — it spent the shared admission budget and then threw
its answers away, because the cache it filled no longer exists.

What remains is the Istanbul clock and the off-peak window guard: the window
has to mean the small hours where METU is, because these requests carry a real
deployment's credentials.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

# METU is in Turkey, so "the small hours" has to mean the small hours there.
# Computing the window in UTC would have run the job from 04:00 to 10:00
# local — straight through the morning peak it exists to avoid.
ISTANBUL = ZoneInfo("Europe/Istanbul")


def _within_hours(now: datetime, window: str) -> bool:
    """Whether ``now`` falls in an "H-H" window of Istanbul hours.

    ``now`` must already be in Istanbul time. The end is exclusive and the
    window may wrap midnight, which is the normal shape of an overnight one.
    """
    from app.config import parse_catalog_warm_hours

    parsed = parse_catalog_warm_hours(window)
    if parsed is None:
        # A malformed safety setting must stop requests, never turn the guard
        # into an always-open window.
        return False
    low, high = parsed
    hour = now.hour
    return low <= hour < high if low <= high else (hour >= low or hour < high)
