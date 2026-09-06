"""Course catalog access for the visual schedule builder.

Every read here goes straight to the connected Course Info MCP server through
:mod:`app.campus.course_info` — no Agent run, language model, prompt, memory or
learning pass is involved anywhere in this module, and none of it costs tokens.

``/curriculum`` was the exception until it stopped being one. It ran a bounded
agent and held the same turn lock a chat turn does, which cost a median of 95.8
seconds in production and made opening the planner during a chat turn fail with
a 409. It now reads the student's own curriculum listing directly.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus import curriculum, departments
from app.campus.warmer import record_wanted_courses
from app.campus.eligibility import course_candidates, evaluate, prior_grade
from app.campus.course_info import (
    CatalogSession,
    call_course_info,
    catalog_key,
    catalog_session,
    department_for_course,
    department_options,
    prefetch,
    resolve_department,
    section_numbers,
)
from app.core.digest import owner_digest, stable_digest
from app.core.persistent_cache import read_cached, read_many_cached, write_cached
from app.core.ttl_cache import TTLCache
from app.db.models import StudentAcademicSnapshot, StudentContext, StudentTimetable
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import report_exception
from app.planning.mcp_bridge import sync_student_context_from_sais

router = APIRouter()
logger = get_logger(__name__)

# Filling a student's context spawns their whole campus toolkit as subprocesses,
# so the schedule page mounting must not be able to start one per render. The
# outcome is remembered either way: a student whose SAIS reports no department
# would otherwise pay for four subprocess launches on every page view.
_context_syncs = TTLCache(ttl_seconds=5 * 60, max_entries=1024)

_PLAN_CACHE_SECONDS = 6 * 60 * 60


class AiScheduleCourse(BaseModel):
    code: str = Field(min_length=3, max_length=20)


class AiScheduleRequest(BaseModel):
    # Optional because the broker already knows it. The department is read from
    # ``sais_get_student_info`` during the same sync that fills the transcript
    # snapshot, so requiring the client to send it back was asking the browser
    # to restate a value the server had all along — and made the request fail
    # outright whenever the page's own state happened to be empty.
    # Official SAIS abbreviations such as EE are two characters long.
    department: str | None = Field(default=None, max_length=20)
    semester: str = Field(min_length=4, max_length=20)
    courses: list[AiScheduleCourse] = Field(default_factory=list, max_length=20)




async def _resolve_department(db: AsyncSession, user_id, provided: str | None) -> str:
    """The department to plan against: what the client sent, else what SAIS said.

    The client's value wins when present, because a student who picked their
    department by hand in the planner is correcting exactly this. Otherwise it
    comes from the stored campus context — and if that has never been filled,
    one single-flighted SAIS sync fills it, the same one the schedule page's
    own mount would have triggered.
    """
    supplied = (provided or "").strip()
    if len(supplied) >= 2:
        return supplied

    context = await db.get(StudentContext, user_id)
    if context is None or not (context.department or context.program_code):
        await _context_syncs.run(str(user_id), lambda: _sync_context(user_id))
        await db.rollback()
        context = await db.get(StudentContext, user_id)

    query, code = await _student_department(db, user_id, context)
    resolved = (code or query or "").strip()
    if len(resolved) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "No department is on file for this account and SAIS did not report one. "
            "Choose your department in the planner and try again.",
        )
    return resolved


async def _cached_plan(key_hash: str) -> dict[str, Any] | None:
    payload = await read_cached(key_hash)
    return payload if isinstance(payload, dict) else None




async def _student_department(
    db: AsyncSession, user_id, context: StudentContext | None
) -> tuple[str | None, str | None]:
    """The student's department as ``(query, three-digit code)``.

    A program code that already carries the department — three digits, or the
    seven-digit form whose first three are the department — is authoritative.
    Anything else is a name, and a name is resolved by asking the catalog
    rather than by pattern-matching digits out of it.
    """
    if context is None:
        return None, None
    query = context.department or context.program_code
    digits = re.sub(r"\D", "", context.program_code or "")
    if len(digits) == 3:
        return query, digits
    if len(digits) == 7:
        return query, digits[:3]
    if not query:
        return None, None
    try:
        return query, await resolve_department(db, user_id, query)
    except HTTPException:
        return query, None


@router.get("/student-context")
async def student_context(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    context = await db.get(StudentContext, user.id)
    if context is None or not (context.department or context.program_code):
        # Single-flight: concurrent mounts of the schedule page wait on one
        # sync instead of each spawning the student's campus servers.
        await _context_syncs.run(str(user.id), lambda: _sync_context(user.id))
        await db.rollback()
        context = await db.get(StudentContext, user.id)

    query, code = await _student_department(db, user.id, context)
    return {
        "student": (
            {
                "department": context.department,
                "degree_level": context.degree_level,
                "year_of_study": context.year_of_study,
                "program_code": context.program_code,
                "campus": context.campus,
                "source": context.source,
            }
            if context
            else None
        ),
        "department_query": query,
        "department_code": code,
        # The abbreviation a section's eligibility table keys on. Resolved from
        # whichever identifier we actually hold, because SAIS reports the name
        # and the eligibility table speaks only abbreviations.
        "department_abbreviation": (
            department.abbreviation if (department := departments.resolve(code or query)) else None
        ),
        # The planner prefills from this. Two letters only: that is all a
        # section's surname range compares, so the rest is never stored.
        "surname_prefix": context.surname_prefix if context else None,
    }


async def _sync_context(user_id) -> bool:
    try:
        return await sync_student_context_from_sais(user_id)
    except Exception as exc:
        logger.warning("schedule_context_sync_failed", user_id=str(user_id), error=str(exc))
        report_exception(
            exc,
            distinct_id=str(user_id),
            handler="schedule_context_sync",
            operation="sync_student_context_from_sais",
            dependency="sais",
        )
        return False


class TimetableMeeting(BaseModel):
    day: Literal["Mon", "Tue", "Wed", "Thu", "Fri"]
    start: int = Field(ge=0, le=23)
    duration: int = Field(default=1, ge=1, le=12)
    room: str = Field(default="", max_length=64)


class TimetableCourse(BaseModel):
    code: str = Field(min_length=1, max_length=24)
    name: str = Field(default="", max_length=160)
    section: str = Field(default="", max_length=16)
    credits: float = Field(default=0, ge=0, le=30)
    instructor: str = Field(default="", max_length=160)
    meetings: list[TimetableMeeting] = Field(default_factory=list, max_length=12)


class TimetableBlock(BaseModel):
    name: str = Field(default="", max_length=80)
    meetings: list[TimetableMeeting] = Field(default_factory=list, max_length=12)


class TimetableIn(BaseModel):
    term: str = Field(min_length=1, max_length=32)
    # Bounded so a saved timetable cannot grow into something that is injected
    # into every chat turn. A full week is far below either ceiling.
    courses: list[TimetableCourse] = Field(default_factory=list, max_length=20)
    busy_blocks: list[TimetableBlock] = Field(default_factory=list, max_length=20)


@router.put("/timetable")
async def save_timetable(
    body: TimetableIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Store the week the student is building, so chat can answer about it.

    The planner keeps its full working state in the browser; only this
    projection is sent. Replacing the row outright rather than merging is
    deliberate: the browser holds the truth, and a merge would resurrect a
    course the student had just deleted.
    """
    row = await db.get(StudentTimetable, user.id)
    payload = {"courses": [course.model_dump() for course in body.courses],
               "busy_blocks": [block.model_dump() for block in body.busy_blocks]}
    if row is None:
        row = StudentTimetable(user_id=user.id, term=body.term, payload=payload)
        db.add(row)
    else:
        row.term = body.term
        row.payload = payload
    await db.commit()
    return {"saved": True, "courses": len(body.courses)}


@router.get("/timetable")
async def read_timetable(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(StudentTimetable, user.id)
    if row is None:
        return {"term": None, "courses": [], "busy_blocks": [], "updated_at": None}
    return {"term": row.term, **row.payload, "updated_at": row.updated_at}


@router.get("/departments/search")
async def search_departments(
    query: str = Query(min_length=1, max_length=100),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await call_course_info(db, user.id, "search_departments", {"query": query})
    # ``departments`` is the normalized list the schedule page's picker binds to;
    # ``data`` stays for callers that want the untouched catalog payload.
    return {"data": data, "departments": department_options(data)}


@router.get("/courses")
async def courses(
    department: str = Query(min_length=1, max_length=20),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {
        "data": await call_course_info(
            db, user.id, "list_program_courses", {"department": department, "semester": semester}
        )
    }


async def _expand_course(
    db: AsyncSession, user_id, course_code: str, department: str, *, session: CatalogSession | None = None
) -> tuple[str, str]:
    """``("MATH260", "236")`` -> ``("2360260", "236")``: the full code and its owner.

    A METU course code is its owning department's three digits followed by a
    four-digit course number. Short forms a student may type are expanded
    against the department that actually owns the course, not the one they are
    enrolled in — a CENG student opening MATH 260 must not be sent to 5710260.
    """
    compact = course_code.upper().replace(" ", "").replace("-", "")
    owner = await department_for_course(db, user_id, compact, department, session=session)
    if not compact.isdigit() and (number := re.search(r"(\d{3,4})$", compact)):
        compact = f"{owner}{number.group(1).zfill(4)}"
    elif compact.isdigit() and len(compact) in (3, 4):
        compact = f"{owner}{compact.zfill(4)}"
    return compact, owner


def _short_code(full_code: str, abbreviation: str) -> str:
    """``("2400101", "HIST")`` -> ``"HIST101"``.

    The form students actually type and read. METU's own seven-digit code is
    the department's three digits followed by a zero-padded course number, so
    the padding comes off before the abbreviation goes on.
    """
    digits = re.sub(r"[^0-9]", "", full_code or "")
    if len(digits) != 7 or not abbreviation:
        return full_code
    return f"{abbreviation}{digits[3:].lstrip('0') or '0'}"


_SEARCH_FOLD = str.maketrans(
    {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
     "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"}
)


def _search_fold(text: str) -> str:
    """Lowercased with Turkish letters folded, for comparing typed text.

    "Tarih" must reach "TARİHİ" and "muhendislik" must reach "Mühendisliği";
    casefold alone does neither, and a student typing on an English keyboard
    is the normal case rather than the exception.
    """
    return str(text).translate(_SEARCH_FOLD).casefold()


@router.get("/courses/search")
async def search_courses(
    query: str = Query(min_length=2, max_length=60),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Courses matching a code or a title, for the add-course box.

    Adding an elective used to require already knowing the exact code: the box
    put whatever was typed straight into the pool, so "I want a history
    elective" had no path through the screen.

    Two things students actually type, and both are supported:

    * a course code — "PHYS213", "MATH119", "HIST". The letters name the
      department, so this is one cached read of that department's listing.
    * a course title — "termodinamik", "signals", "differential". Titles are
      searched across every department already in the shared cache, in a
      single query, plus the student's own department fetched live if it is
      not cached yet.

    Titles cannot be searched across departments that have never been fetched:
    doing that live would be one campus round trip per department, which is
    the traffic the nightly warm-up exists to avoid making all at once. The
    reply says how many departments were actually searched so the caller can
    be honest about it rather than implying the whole catalog was.
    """
    typed = query.strip()
    letters = re.sub(r"[^A-Za-zÇĞİÖŞÜçğıöşü]", "", typed)
    digits = re.sub(r"[^0-9]", "", typed)
    named = departments.resolve(letters) if len(letters) >= 2 else None

    context = await db.get(StudentContext, user.id)
    home = departments.resolve((context.department or context.program_code) if context else None)

    # A code lookup: the letters name a department, or there are only digits.
    if named is not None or (digits and not letters):
        owner = named or home
        if owner is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Type a course code with its department (PHYS213), or set your department in Settings.",
            )
        async with catalog_session(db, user.id) as catalog:
            payload = await call_course_info(
                db, user.id, "list_program_courses",
                {"department": owner.code, "semester": semester},
                session=catalog,
            )
        courses = _match_courses(payload, owner, digits=digits, title="")
        return {"courses": courses[:40], "searched_departments": 1, "scope": owner.abbreviation or owner.code}

    # A title search, across whatever the cache already holds.
    indexed, covered = await _search_index(semester)
    extra: list[tuple[str, dict]] = []
    if home is not None and home.code not in covered:
        async with catalog_session(db, user.id) as catalog:
            payload = await call_course_info(
                db, user.id, "list_program_courses",
                {"department": home.code, "semester": semester},
                session=catalog,
            )
        extra = _index_rows(payload, home)
    wanted = _search_fold(typed)
    # Every department is scanned rather than stopping at the first sixty hits:
    # breaking early ordered results by department id, so a match in a late
    # department was invisible while an unrelated one filled the box.
    courses = [course for haystack, course in (*indexed, *extra) if wanted in haystack]
    # The student's own department first: an elective search is usually still
    # anchored to what they are studying.
    if home is not None:
        courses.sort(key=lambda item: item["department"] != (home.abbreviation or home.code))
    return {
        "courses": courses[:40],
        "searched_departments": len(covered) + (1 if extra else 0),
        "scope": "catalog",
    }


def _listing_key(department_code: str, semester: str) -> str:
    """The cache key ``call_course_info`` stores a department listing under."""
    return catalog_key("list_program_courses", {"department": department_code, "semester": semester})[1]


async def _cached_listings(semester: str) -> dict[str, Any]:
    """Every department listing already in the shared cache, by department code."""
    wanted = {_listing_key(entry.code, semester): entry.code for entry in departments.all_departments()}
    found = await read_many_cached(list(wanted))
    return {wanted[key]: payload for key, payload in found.items()}


# The whole term's catalog, shaped and folded once. A title search used to pull
# 153 department listings out of Postgres and re-fold every course title in all
# of them, synchronously, on every keystroke of a 300 ms debounce. Ten minutes
# is short next to the thirty-day lifetime of the listings underneath, so the
# staleness this can introduce is a department the warmer added tonight not
# being searchable until the top of the hour.
_SEARCH_INDEX = TTLCache(ttl_seconds=10 * 60, max_entries=4)


def _index_rows(payload: Any, owner: Any) -> list[tuple[str, dict]]:
    """One department's listing as (folded haystack, course) pairs.

    The haystack carries the short code as well as the title, so "PHYS213"
    pasted whole still lands when it arrives through the title path.
    """
    rows: list[tuple[str, dict]] = []
    for row in _catalog_rows(payload):
        full_code = str(row.get("course_code") or "").strip()
        if not full_code:
            continue
        name = " ".join(str(row.get("name") or "").split())
        short = _short_code(full_code, owner.abbreviation)
        rows.append((
            _search_fold(f"{short} {name}"),
            {
                "code": short,
                "full_code": full_code,
                "name": name,
                "credits": _credit_value(row.get("credit")),
                "department": owner.abbreviation or owner.code,
            },
        ))
    return rows


async def _search_index(semester: str) -> tuple[list[tuple[str, dict]], set[str]]:
    """Every cached department's courses for one term, and which departments those were.

    The course dicts are shared with every request that searches this term, so
    callers read them and never mutate them; the endpoint only ever puts them
    in a response.
    """

    async def build() -> tuple[list[tuple[str, dict]], set[str]]:
        listings = await _cached_listings(semester)
        rows: list[tuple[str, dict]] = []
        for code, payload in listings.items():
            owner = departments.by_code(code)
            if owner is None:
                continue
            rows.extend(_index_rows(payload, owner))
        return rows, set(listings)

    # Single-flighted, so a burst of keystrokes past the debounce builds it
    # once and the rest wait on that build rather than starting their own.
    return await _SEARCH_INDEX.run(semester, build)


def _match_courses(payload: Any, owner: Any, *, digits: str, title: str) -> list[dict]:
    """Rows of one department's listing that answer the query."""
    wanted_title = _search_fold(title)
    matches: list[dict] = []
    for haystack, course in _index_rows(payload, owner):
        if digits and not re.sub(r"[^0-9]", "", course["code"]).startswith(digits):
            continue
        if wanted_title and wanted_title not in haystack:
            continue
        matches.append(course)
    return matches


def _catalog_rows(payload: Any) -> list[dict]:
    """The course rows inside whatever wrapper the catalog answered with."""
    if isinstance(payload, dict):
        for key in ("result", "courses", "data", "items"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
        return []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _credit_value(raw: Any) -> float:
    """``"3.00 (3.00,0.00,0.00)"`` -> ``3.0``."""
    match = re.search(r"\d+(?:\.\d+)?", str(raw or ""))
    return float(match.group(0)) if match else 0.0


@router.get("/courses/{course_code}")
async def course_sections(
    course_code: str = Path(min_length=3, max_length=20),
    department: str = Query(min_length=1, max_length=20),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    async with catalog_session(db, user.id) as catalog:
        compact_course, lookup_department = await _expand_course(
            db, user.id, course_code, department, session=catalog
        )
        return {
            "data": await call_course_info(
                db,
                user.id,
                "get_course_info",
                {"department": lookup_department, "semester": semester, "course": compact_course},
                session=catalog,
            )
        }


class BulkConstraintsRequest(BaseModel):
    semester: str = Field(min_length=1, max_length=20)
    # The curriculum is a couple of dozen courses at most. The cap is here so a
    # crafted request cannot turn one HTTP call into hundreds of SAIS fetches.
    courses: list[str] = Field(min_length=1, max_length=40)
    department: str | None = Field(default=None, max_length=20)


class BulkSectionsRequest(BaseModel):
    semester: str = Field(min_length=1, max_length=20)
    courses: list[str] = Field(min_length=1, max_length=40)
    department: str | None = Field(default=None, max_length=20)


@router.post("/sections")
async def bulk_course_sections(
    body: BulkSectionsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Section lists for several courses at once.

    Building a schedule needs the sections of every course in the pool, and the
    browser fetched them one request at a time — fifteen courses meant fifteen
    round trips, each able to spawn its own catalog connection. They share one
    here, exactly as ``/constraints`` already does.

    Two phases. Everything already cached is answered from one database read
    with no campus contact at all, which for a pool the warmer has seen is the
    whole batch. What is left is fetched one at a time over the shared session:
    concurrent calls on it would interleave on the same stateful SAIS page, and
    a stubbed toolkit in a test would not show it.

    A course that cannot be read is reported with its error rather than failing
    the batch, and keyed by the code the client sent so the caller can match the
    answer to what it asked for.
    """
    results: dict[str, Any] = {}
    async with catalog_session(db, user.id) as catalog:
        expanded: dict[str, tuple[str, str]] = {}
        for raw in dict.fromkeys(code.strip() for code in body.courses if code.strip()):
            try:
                expanded[raw] = await _expand_course(
                    db, user.id, raw, body.department or "", session=catalog
                )
            except HTTPException as exc:
                logger.info("bulk_sections_skipped", course=raw, detail=str(exc.detail))
                results[raw] = {"error": str(exc.detail)}

        await prefetch(
            ("get_course_info", {"department": owner, "semester": body.semester, "course": course})
            for course, owner in expanded.values()
        )

        for raw, (compact_course, lookup_department) in expanded.items():
            try:
                results[raw] = {
                    "data": await call_course_info(
                        db,
                        user.id,
                        "get_course_info",
                        {
                            "department": lookup_department,
                            "semester": body.semester,
                            "course": compact_course,
                        },
                        session=catalog,
                    )
                }
            except HTTPException as exc:
                logger.info("bulk_sections_skipped", course=raw, detail=str(exc.detail))
                results[raw] = {"error": str(exc.detail)}
    return {"courses": results}


@router.post("/constraints")
async def bulk_constraints(
    body: BulkConstraintsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Eligibility verdicts for every section of several courses at once.

    A section's restrictions differ from section to section — that is the
    normal case at METU, not the exception — so a student cannot be told
    whether a course is open to them without reading every section's table.
    Doing that from the browser meant one request per course, each opening its
    own catalog connection; here they share one, and the shared cache means a
    course any student has opened this week costs nothing.

    A course that cannot be read is reported with its error rather than failing
    the batch: the rest of the curriculum is still worth answering.
    """
    results: dict[str, Any] = {}
    profile = await _student_profile(db, user)
    async with catalog_session(db, user.id) as catalog:
        # Resolve every code first. This is a local directory lookup for all but
        # an unrecognised prefix, and doing it up front is what lets the course
        # pages below be looked up in the cache together.
        expanded: dict[str, tuple[str, str]] = {}
        for raw in dict.fromkeys(code.strip() for code in body.courses if code.strip()):
            try:
                expanded[raw] = await _expand_course(
                    db, user.id, raw, body.department or "", session=catalog
                )
            except HTTPException as exc:
                logger.info("bulk_constraints_skipped", course=raw, detail=str(exc.detail))
                results[raw] = {"course": raw, "error": str(exc.detail), "sections": {}}

        # One database read for every course page in the batch, instead of one
        # per course each opening its own session.
        await prefetch(
            ("get_course_info", {"department": owner, "semester": body.semester, "course": course})
            for course, owner in expanded.values()
        )

        for raw, (compact_course, lookup_department) in expanded.items():
            try:
                results[raw] = await _constraints_for(
                    db, user, catalog, raw, compact_course, lookup_department, body.semester, profile
                )
            except HTTPException as exc:
                logger.info("bulk_constraints_skipped", course=raw, detail=str(exc.detail))
                results[raw] = {"course": raw, "error": str(exc.detail), "sections": {}}
    return {"courses": results}


@router.get("/courses/{course_code}/constraints")
async def course_section_constraints(
    course_code: str = Path(min_length=3, max_length=20),
    department: str = Query(min_length=1, max_length=20),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Who may register for each section of this course, and whether this student may.

    One SAIS page per section, so this is deliberately a second request rather
    than folded into ``/courses/{code}``: a course with eight sections would
    otherwise make opening it eight times slower for a student who only wanted
    to see the meeting times. Both layers of cache apply, so the cost is paid
    once per course per week across every student.
    """
    # One connection for the department lookup, the course page and every
    # section's table below. Opened only if something misses the cache.
    profile = await _student_profile(db, user)
    async with catalog_session(db, user.id) as catalog:
        compact_course, lookup_department = await _expand_course(
            db, user.id, course_code, department, session=catalog
        )
        return await _constraints_for(
            db, user, catalog, course_code, compact_course, lookup_department, semester, profile
        )


@dataclass(frozen=True)
class _StudentProfile:
    """Everything an eligibility verdict needs about the student, read once.

    These four values are the same for every course and every section in a
    request, but they used to be read per course inside the loop below: a
    forty-course batch issued a hundred and twenty queries — a context row, a
    department resolution that can itself reach the campus, and a transcript
    scan — to answer with the same four values every time.
    """

    context: StudentContext | None
    department: Any
    cgpa: float | None
    completed: list[dict]


async def _student_profile(db: AsyncSession, user: AuthenticatedUser) -> _StudentProfile:
    context = await db.get(StudentContext, user.id)
    query, code = await _student_department(db, user.id, context)
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    cgpa = None
    if snapshot and snapshot.current_credits:
        cgpa = float(snapshot.current_grade_points) / float(snapshot.current_credits)
    return _StudentProfile(
        context=context,
        department=departments.resolve(code or query),
        cgpa=cgpa,
        completed=list(snapshot.completed_courses) if snapshot else [],
    )


async def _constraints_for(
    db: AsyncSession,
    user: AuthenticatedUser,
    catalog: CatalogSession,
    course_code: str,
    compact_course: str,
    lookup_department: str,
    semester: str,
    profile: _StudentProfile,
) -> dict:
    info = await call_course_info(
        db,
        user.id,
        "get_course_info",
        {"department": lookup_department, "semester": semester, "course": compact_course},
        session=catalog,
    )

    # A section reserved for students who still need the course is closed to
    # one who has already passed it, and only the transcript knows which.
    # The course's *owner* department, not the student's: the candidates are
    # transcript spellings of this course ("PHYS213"), so they are built from
    # whoever owns it. Passing the student's department here would look for
    # "EE213", a course that does not exist.
    course_owner = departments.by_code(lookup_department)
    held = prior_grade(
        profile.completed,
        course_candidates(course_code, course_owner, compact_course),
    )

    sections: dict[str, Any] = {}
    for number in section_numbers(info):
        try:
            payload = await call_course_info(
                db,
                user.id,
                "get_section_constraints",
                {
                    "department": lookup_department,
                    "semester": semester,
                    "course": compact_course,
                    "section": number,
                },
                session=catalog,
            )
        except HTTPException as exc:
            # A section whose table cannot be read must not fail the whole
            # course: the student still gets its times, just no verdict.
            logger.warning("section_constraints_failed", course=compact_course, section=number, detail=exc.detail)
            continue
        rows = _constraint_rows(payload)
        verdict = evaluate(
            rows,
            department=profile.department.abbreviation if profile.department else None,
            surname=profile.context.surname_prefix if profile.context else None,
            cgpa=profile.cgpa,
            year=profile.context.year_of_study if profile.context else None,
            prior_grade=held,
        )
        sections[number] = {
            "rows": rows,
            "eligible": verdict.eligible,
            "reason": verdict.reason,
        }

    return {
        "course": compact_course,
        "department": lookup_department,
        "student_department": profile.department.abbreviation if profile.department else None,
        "your_grade_in_this_course": held,
        "sections": sections,
    }


def _constraint_rows(payload: Any) -> list[dict[str, Any]]:
    """The eligibility rows out of whatever shape the tool returned."""
    if isinstance(payload, dict):
        rows = payload.get("constraints")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict) and "given_dept" in row]
    return []


_CURRICULUM_NAMESPACE = "schedule-curriculum"
# Bumped when the shape of a stored answer changes, so a deploy cannot spend six
# hours serving results built by the previous version of this code.
_CURRICULUM_VERSION = 1


def _curriculum_cache_key(
    user_id, department: str, body: AiScheduleRequest, snapshot: StudentAcademicSnapshot | None
) -> tuple[str, str]:
    owner_hash = owner_digest(user_id)
    identity = {
        "version": _CURRICULUM_VERSION,
        "owner": owner_hash,
        "department": department,
        "semester": body.semester.strip(),
        "snapshot": snapshot.fetched_at.isoformat() if snapshot else None,
    }
    return stable_digest(identity), owner_hash


def _curriculum_year(value: Any) -> int:
    """The curriculum year a row is scheduled for; unknown sorts last."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))[:1]
    year = int(digits) if digits else 0
    return year if 1 <= year <= 8 else 9


async def _category_rows(
    db: AsyncSession, user: AuthenticatedUser, catalog: CatalogSession, department: str
) -> tuple[list[dict], list[str]]:
    """The student's curriculum rows, and anything SAIS said instead of rows."""
    overview = await call_course_info(
        db, user.id, "get_student_course_categories", {}, session=catalog
    )
    program_type, categories = curriculum.select_program(overview, department)
    rows: list[dict] = []
    warnings: list[str] = []
    for rank, category in enumerate(categories):
        category_id = str(category.get("id") or "").strip()
        if not category_id:
            continue
        values = {"category": category_id}
        if program_type:
            values["program_type"] = program_type
        result = await call_course_info(
            db, user.id, "get_student_courses_by_category", values, session=catalog
        )
        if not isinstance(result, dict):
            continue
        # SAIS answers an empty category with prose rather than an empty table.
        # Surfacing it is the difference between "you have no electives left"
        # and "we could not read your electives".
        message = result.get("message")
        if isinstance(message, str) and message.strip() and not result.get("courses"):
            warnings.append(f"{category.get('name') or category_id}: {message.strip()}")
        for row in result.get("courses") or []:
            if isinstance(row, dict):
                rows.append({**row, "_rank": rank})
    return rows, warnings


async def _offered_courses(
    db: AsyncSession,
    user: AuthenticatedUser,
    catalog: CatalogSession,
    candidates: dict[str, dict],
    semester: str,
) -> tuple[dict[str, dict], list[str]]:
    """The candidates this term's catalog actually publishes, with its own names.

    One listing per owning department, which is the tool the overnight warmer
    fills for all 153 of them — so a curriculum spanning eight departments
    usually costs nothing at all here.
    """
    by_owner: dict[str, list[str]] = {}
    for code in candidates:
        by_owner.setdefault(code[:3], []).append(code)

    offered: dict[str, dict] = {}
    warnings: list[str] = []
    for owner_code, codes in by_owner.items():
        try:
            payload = await call_course_info(
                db,
                user.id,
                "list_program_courses",
                {"department": owner_code, "semester": semester},
                session=catalog,
            )
        except HTTPException as exc:
            owner = departments.by_code(owner_code)
            label = (owner.abbreviation or owner_code) if owner else owner_code
            # Dropped rather than kept: recommending a course without checking
            # it is offered is the one thing this endpoint must not do.
            warnings.append(
                f"{label}: this term's course list could not be read ({exc.detail}), "
                "so its courses were left out rather than recommended unverified."
            )
            continue
        published: dict[str, dict] = {}
        for row in _catalog_rows(payload):
            full = re.sub(r"[^0-9]", "", str(row.get("course_code") or ""))
            if len(full) == 7:
                published[full] = row
        for code in codes:
            row = published.get(code)
            if row is None:
                continue
            course = candidates[code]
            name = " ".join(str(row.get("name") or "").split())
            offered[code] = {
                **course,
                "name": name or course["name"],
                "credits": _credit_value(row.get("credit")) or course["credits"],
            }
    return offered, warnings


async def _curriculum_courses(
    db: AsyncSession,
    user: AuthenticatedUser,
    catalog: CatalogSession,
    department: str,
    semester: str,
    completed: list[dict],
) -> tuple[list[dict], list[str]]:
    rows, warnings = await _category_rows(db, user, catalog, department)

    candidates: dict[str, dict] = {}
    for row in rows:
        code = curriculum.normalise_code(row.get("course_code"))
        if code is None:
            label = " ".join(str(row.get("course_code") or "").split())
            if label:
                warnings.append(f"{label}: this code names no department we know, so it was left out.")
            continue
        if not curriculum.still_needed(code, completed):
            continue
        candidates.setdefault(
            code,
            {
                "code": code,
                "name": " ".join(str(row.get("course_name") or "").split()),
                "credits": _credit_value(row.get("credit")),
                "sections": [],
                "_rank": row.get("_rank", 0),
                "_year": _curriculum_year(row.get("year_or_ects")),
            },
        )

    offered, offer_warnings = await _offered_courses(db, user, catalog, candidates, semester)
    warnings.extend(offer_warnings)

    kept, variant_warnings = curriculum.resolve_citizenship_variants(
        list(offered.values()), completed
    )
    warnings.extend(variant_warnings)
    kept.sort(key=lambda course: (course["_year"], course["_rank"], course["code"]))
    return (
        [
            {key: value for key, value in course.items() if not key.startswith("_")}
            for course in kept[: curriculum.MAX_COURSES]
        ],
        warnings,
    )


@router.post("/curriculum")
async def curriculum_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The courses this student still has to take, offered this term.

    Read from their own curriculum listing and this term's department catalogs.
    This used to be an agent run: a median of 95.8 seconds in production, of
    which about 88 were model round trips between tool calls that themselves
    took eight. Matching a code list against a department listing is not a
    judgement, so there is nothing here for a model to do.

    ``sections`` is always empty, as it was before: the planner loads a course's
    times when the student opens it, and fetching them for a whole curriculum
    would be dozens of campus pages nobody asked for.
    """
    started_at = time.monotonic()
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    department = await _resolve_department(db, user.id, body.department)
    cache_key, owner_hash = _curriculum_cache_key(user.id, department, body, snapshot)
    cached = await _cached_plan(cache_key)
    if cached is not None:
        return {
            **cached,
            "cache_hit": True,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }

    completed = list(snapshot.completed_courses) if snapshot else []
    try:
        async with catalog_session(db, user.id) as catalog:
            courses, warnings = await _curriculum_courses(
                db, user, catalog, department, body.semester.strip(), completed
            )
    except HTTPException as exc:
        # Deliberately not a 502. Loading the curriculum is one of several ways
        # to fill the pool, and failing the request takes down a screen where
        # the student could still search for courses by hand.
        logger.info("curriculum_plan_unavailable", user_id=str(user.id), detail=str(exc.detail))
        return {
            "courses": [],
            "warnings": [f"Your curriculum could not be read from METU: {exc.detail}"],
            "source": "curriculum",
            "cache_hit": False,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }

    response = {
        "courses": courses,
        "warnings": warnings,
        "source": "curriculum",
        "cache_hit": False,
        "duration_ms": round((time.monotonic() - started_at) * 1000),
    }
    # Only a result worth reusing. An empty list means something upstream gave
    # us nothing, and caching that turns one bad minute into six bad hours.
    if courses:
        # What a student asked for tonight is what the warmer should have ready
        # tomorrow. Course codes only, never who wanted them.
        await record_wanted_courses(body.semester.strip(), [course["code"] for course in courses])
        await write_cached(
            cache_key,
            response,
            namespace=_CURRICULUM_NAMESPACE,
            ttl_seconds=_PLAN_CACHE_SECONDS,
            owner_hash=owner_hash,
        )
    return response


@router.post("/ai-plan")
async def ai_schedule_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The previous name for :func:`curriculum_plan`.

    Kept for one release. A browser holding a cached bundle still calls this
    route by name, and deleting it in the same deploy that adds the new one
    breaks every tab that was already open.
    """
    return await curriculum_plan(body, user, db)


