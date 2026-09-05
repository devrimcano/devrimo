"""Fill the shared catalog cache before the students who need it arrive.

Course offerings are published once per term and then barely move, so the first
student to open a department should not be the one who waits seven seconds for
METU. This walks the department list slowly in the background and fills the
same shared cache the schedule page reads.

The pacing is the point of this module, not an afterthought. These requests go
to METU's own servers carrying a real student's credentials, so a burst that
looks like a crawler risks that student's account, not ours. Everything here is
built to look like somebody browsing slowly and to stop early when unsure:

* one request at a time, never concurrent;
* a long gap between requests, with jitter so it is not a metronome;
* a hard daily ceiling that survives a restart, because a crash loop must not
  become a way around the budget;
* off-peak hours only, when METU is quiet;
* an entry already cached costs nothing and is skipped without a request;
* the first failure ends the pass rather than retrying into a wall.

The whole job is one cheap read per department per term. It deliberately does
not walk individual courses: that would be thousands of page loads, which is a
different thing entirely and not something to point at a university.
"""

import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from app.campus import departments as department_directory
from app.campus import service as campus_service
from app.campus.course_info import CATALOG_NAMESPACE, call_course_info, catalog_session
from app.config import get_settings
from app.core.digest import stable_digest
from app.core.persistent_cache import read_cached, write_cached
from app.db.session import SessionLocal
from app.logging import get_logger
from app.planning.service import current_term

logger = get_logger(__name__)

# METU is in Turkey, so "the small hours" has to mean the small hours there.
# Computing the window in UTC would have run this job from 04:00 to 10:00
# local — straight through the morning peak it exists to avoid.
ISTANBUL = ZoneInfo("Europe/Istanbul")

WARM_TOOL = "list_program_courses"
BUDGET_NAMESPACE = "catalog-warm-budget"


def _budget_key(day: str) -> str:
    return stable_digest({"namespace": BUDGET_NAMESPACE, "day": day})


async def _spent_today(day: str) -> int:
    value = await read_cached(_budget_key(day))
    return int(value) if isinstance(value, (int, float)) else 0


async def _record_spend(day: str, count: int) -> None:
    # Two days of life so the row is still there for a job that starts before
    # midnight and finishes after it, and is reclaimed by the ordinary sweep.
    await write_cached(_budget_key(day), count, namespace=BUDGET_NAMESPACE, ttl_seconds=2 * 24 * 3600)


def _within_hours(now: datetime, window: str) -> bool:
    """Whether ``now`` falls in an "H-H" window of Istanbul hours.

    ``now`` must already be in Istanbul time. The end is exclusive and the
    window may wrap midnight, which is the normal shape of an overnight one.
    """
    try:
        start, _, end = window.partition("-")
        low, high = int(start), int(end)
    except ValueError:
        return True
    hour = now.hour
    return low <= hour < high if low <= high else (hour >= low or hour < high)


def _cache_key(department: str, semester: str) -> str:
    """The key ``call_course_info`` would store this answer under.

    Mirrors that function's own identity construction, so a warmed entry is the
    one the schedule page reads rather than a parallel copy.
    """
    identity = (WARM_TOOL, f"department={department}", f"semester={semester}")
    return stable_digest({"namespace": CATALOG_NAMESPACE, "identity": list(identity)})


async def _warming_user() -> "tuple[object, object] | None":
    """A student whose catalog access can carry the warm-up, or None.

    Rotated by day so the same account does not carry every pass: the load is
    tiny, but concentrating even a tiny load on one student is how one student
    gets a phone call from the registrar.
    """
    async with SessionLocal() as db:
        candidates = await campus_service.users_with_tool(db, "course_info")
    if not candidates:
        return None
    ordered = sorted(str(user_id) for user_id in candidates)
    index = datetime.now(ISTANBUL).toordinal() % len(ordered)
    from uuid import UUID

    return UUID(ordered[index]), len(ordered)


async def warm_once() -> int:
    """One pass. Returns how many departments were fetched from METU."""
    settings = get_settings()
    if not settings.catalog_warm_enabled:
        return 0
    now = datetime.now(ISTANBUL)
    if not _within_hours(now, settings.catalog_warm_hours):
        return 0

    # A local day, so the ceiling lines up with the window that spends it.
    day = now.date().isoformat()
    spent = await _spent_today(day)
    if spent >= settings.catalog_warm_daily_limit:
        return 0

    chosen = await _warming_user()
    if chosen is None:
        return 0
    user_id, pool_size = chosen

    semester = current_term(now)
    # Only what is actually missing, and never the whole list in one pass.
    missing = []
    for entry in department_directory.all_departments():
        if await read_cached(_cache_key(entry.code, semester)) is None:
            missing.append(entry.code)
    if not missing:
        return 0

    allowance = min(
        settings.catalog_warm_batch,
        settings.catalog_warm_daily_limit - spent,
        len(missing),
    )
    logger.info(
        "catalog_warm_started",
        semester=semester,
        missing=len(missing),
        allowance=allowance,
        spent_today=spent,
        account_pool=pool_size,
    )

    fetched = 0
    async with SessionLocal() as db:
        async with catalog_session(db, user_id) as catalog:
            for code in missing[:allowance]:
                try:
                    await call_course_info(
                        db, user_id, WARM_TOOL, {"department": code, "semester": semester}, session=catalog
                    )
                except Exception as exc:
                    # One failure ends the pass. It usually means the session
                    # expired or METU is refusing us, and the correct response
                    # to either is to stop rather than to keep knocking.
                    logger.warning("catalog_warm_stopped", department=code, error=str(exc))
                    break
                fetched += 1
                await _record_spend(day, spent + fetched)
                await asyncio.sleep(
                    settings.catalog_warm_interval_seconds
                    + random.uniform(0, settings.catalog_warm_jitter_seconds)
                )

    logger.info("catalog_warm_finished", fetched=fetched, semester=semester)
    return fetched
