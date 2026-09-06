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

The first phase is one cheap read per department per term. It deliberately does
not walk every course: that would be thousands of page loads, which is a
different thing entirely and not something to point at a university.

The second phase walks a handful of individual courses, and the distinction
above is exactly why it is shaped the way it is. It does not enumerate the
catalog. It reads the courses students have actually asked for — the curriculum
endpoint records what it returned — takes the few most wanted that are not
already cached, and spends the same daily ceiling at the same pace. It runs only
once every department listing is in hand, so the cheap job that the search box
depends on can never be crowded out by the expensive one.
"""

import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from app.campus import departments as department_directory
from app.campus import service as campus_service
from app.campus.course_info import call_course_info, catalog_key, catalog_session, section_numbers
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
WANTED_NAMESPACE = "catalog-warm-wanted"

# One row per term holding {course_code: times asked for}. A row rather than a
# key per course because nothing here can enumerate a namespace, and a single
# document is both cheaper to read and trivially bounded.
WANTED_LIMIT = 500
# Long enough to survive a quiet week, short enough that last term's demand
# stops steering this term's warming.
WANTED_TTL_SECONDS = 14 * 24 * 3600


def _budget_key(day: str) -> str:
    return stable_digest({"namespace": BUDGET_NAMESPACE, "day": day})


async def _spent_today(day: str) -> int:
    value = await read_cached(_budget_key(day))
    return int(value) if isinstance(value, (int, float)) else 0


async def _record_spend(day: str, count: int) -> None:
    # Two days of life so the row is still there for a job that starts before
    # midnight and finishes after it, and is reclaimed by the ordinary sweep.
    await write_cached(_budget_key(day), count, namespace=BUDGET_NAMESPACE, ttl_seconds=2 * 24 * 3600)


def _wanted_key(semester: str) -> str:
    return stable_digest({"namespace": WANTED_NAMESPACE, "semester": semester})


async def _wanted_courses(semester: str) -> dict[str, int]:
    value = await read_cached(_wanted_key(semester))
    if not isinstance(value, dict):
        return {}
    courses = value.get("courses")
    if not isinstance(courses, dict):
        return {}
    return {str(code): int(hits) for code, hits in courses.items() if str(hits).isdigit()}


async def record_wanted_courses(semester: str, codes: list[str]) -> None:
    """Note that somebody needed these courses, so tonight's pass can prepare them.

    Course codes only. Who asked is not recorded and not inferable from the row:
    it is a running count across everyone, which is exactly what a warming
    decision should be made from.

    Failures are swallowed. This is a hint for a background job, and no student
    should ever see their own request fail because a hint could not be stored.
    """
    wanted = {code for code in codes if code}
    if not wanted:
        return
    try:
        counts = await _wanted_courses(semester)
        for code in wanted:
            counts[code] = counts.get(code, 0) + 1
        if len(counts) > WANTED_LIMIT:
            counts = dict(sorted(counts.items(), key=lambda item: -item[1])[:WANTED_LIMIT])
        await write_cached(
            _wanted_key(semester),
            {"courses": counts},
            namespace=WANTED_NAMESPACE,
            ttl_seconds=WANTED_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a hint is never worth a failed request
        logger.info("catalog_warm_demand_not_recorded", error=str(exc))


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
    """The key ``call_course_info`` stores this answer under.

    Shares that function's own formula rather than restating it, so a warmed
    entry is the one the schedule page reads and not a parallel copy. The test
    that recomputes this digest by hand is deliberately left doing so: it is
    the only thing that would notice the two drifting apart again.
    """
    return catalog_key(WARM_TOOL, {"department": department, "semester": semester})[1]


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
        # Departments first, always. The search box reads those listings, so
        # every one of them matters more than any single course page.
        return await _warm_courses(user_id, semester, day, spent)

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


async def _warm_courses(user_id, semester: str, day: str, spent: int) -> int:
    """Warm the course pages students asked for, once every department is in.

    A course costs one page plus one request per section, so the ceiling is
    counted per *request* rather than per course — three courses is roughly
    twenty. It shares the same daily budget as the department listings and the
    same interval between requests, and the first failure ends the pass.
    """
    settings = get_settings()
    counts = await _wanted_courses(semester)
    if not counts:
        return 0

    uncached: list[str] = []
    for code, _ in sorted(counts.items(), key=lambda item: -item[1]):
        if len(uncached) >= settings.catalog_warm_courses_per_pass:
            break
        if len(code) != 7 or not code.isdigit():
            continue
        key = catalog_key("get_course_info", {"department": code[:3], "semester": semester, "course": code})[1]
        if await read_cached(key) is None:
            uncached.append(code)
    if not uncached:
        return 0

    logger.info("catalog_warm_courses_started", semester=semester, courses=len(uncached), spent_today=spent)
    requests = 0
    async with SessionLocal() as db:
        async with catalog_session(db, user_id) as catalog:
            for code in uncached:
                values = {"department": code[:3], "semester": semester, "course": code}
                try:
                    info = await call_course_info(db, user_id, "get_course_info", values, session=catalog)
                except Exception as exc:
                    logger.warning("catalog_warm_stopped", course=code, error=str(exc))
                    break
                requests += 1
                await _record_spend(day, spent + requests)
                await asyncio.sleep(
                    settings.catalog_warm_interval_seconds
                    + random.uniform(0, settings.catalog_warm_jitter_seconds)
                )

                stop = False
                for number in section_numbers(info):
                    if spent + requests >= settings.catalog_warm_daily_limit:
                        stop = True
                        break
                    try:
                        await call_course_info(
                            db, user_id, "get_section_constraints",
                            {**values, "section": number}, session=catalog,
                        )
                    except Exception as exc:
                        logger.warning("catalog_warm_stopped", course=code, section=number, error=str(exc))
                        stop = True
                        break
                    requests += 1
                    await _record_spend(day, spent + requests)
                    await asyncio.sleep(
                        settings.catalog_warm_interval_seconds
                        + random.uniform(0, settings.catalog_warm_jitter_seconds)
                    )
                if stop:
                    break

    logger.info("catalog_warm_courses_finished", requests=requests, semester=semester)
    return requests
