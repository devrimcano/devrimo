"""Shared pacing helpers for the catalog workers.

The overnight raw-cache warmer this module used to run is gone: shared catalog
facts now come from the reviewed, published catalog, and its own worker owns
the import schedule and the METU request budget. Keeping the old pass around
was worse than dead code — it spent the shared admission budget and then threw
its answers away, because the cache it filled no longer exists.

What remains are the pieces the published worker still borrows: the Istanbul
clock (the off-peak window has to mean the small hours where METU is), the
window guard itself, and the demand hint the scheduler sorts by. The hint's
writer was part of the retired path, so it reads as empty until a published
writer records demand again; the scheduler falls back to an alphabetical
order, which is still bounded and fair.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.digest import stable_digest
from app.core.persistent_cache import read_cached

# METU is in Turkey, so "the small hours" has to mean the small hours there.
# Computing the window in UTC would have run the job from 04:00 to 10:00
# local — straight through the morning peak it exists to avoid.
ISTANBUL = ZoneInfo("Europe/Istanbul")

WANTED_NAMESPACE = "catalog-warm-wanted"


def _wanted_key(semester: str) -> str:
    return stable_digest({"namespace": WANTED_NAMESPACE, "semester": semester})


async def _wanted_courses(semester: str) -> dict[str, int]:
    """The courses students have asked for, most wanted first.

    A missing or malformed row reads as no demand at all: this is a scheduling
    hint, never a source of truth.
    """
    value = await read_cached(_wanted_key(semester))
    if not isinstance(value, dict):
        return {}
    courses = value.get("courses")
    if not isinstance(courses, dict):
        return {}
    return {str(code): int(hits) for code, hits in courses.items() if str(hits).isdigit()}


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
