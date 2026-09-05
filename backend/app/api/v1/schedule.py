"""Course catalog access for the visual schedule builder.

The catalog reads go straight to the connected Course Info MCP server through
:mod:`app.campus.course_info` — no Agent run, language model, prompt, memory or
learning pass is involved, and they cost no tokens. Only ``/ai-plan`` starts an
agent, and it holds the same turn lock a chat turn does.
"""

import asyncio
import json
import re
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import manager
from app.api.v1.schedule_prompt import PLANNER_PROMPT
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus import departments
from app.campus.eligibility import course_candidates, evaluate, prior_grade
from app.campus.course_info import (
    CATALOG_NAMESPACE,
    CatalogSession,
    call_course_info,
    catalog_session,
    department_for_course,
    department_options,
    json_value,
    resolve_department,
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

_AGENT_RUN_TIMEOUT_SECONDS = 180
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


def _plan_cache_key(
    user_id, department: str, body: AiScheduleRequest, snapshot: StudentAcademicSnapshot | None
) -> tuple[str, str]:
    owner_hash = owner_digest(user_id)
    identity = {
        "owner": owner_hash,
        # The *resolved* department, never the request's: two calls that differ
        # only in whether the client bothered to send it are the same plan and
        # must share a cache entry.
        "department": department,
        "semester": body.semester.strip(),
        "courses": [item.code.strip().upper() for item in body.courses],
        "snapshot": snapshot.fetched_at.isoformat() if snapshot else None,
    }
    return stable_digest(identity), owner_hash


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


async def _store_plan(key_hash: str, owner_hash: str, payload: dict[str, Any]) -> None:
    # owner_hash is what lets DELETE /student/academic-data find this row again,
    # so a plan built from one student's transcript stays erasable.
    await write_cached(
        key_hash,
        payload,
        namespace="schedule-plan",
        ttl_seconds=_PLAN_CACHE_SECONDS,
        owner_hash=owner_hash,
    )


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
    listings = await _cached_listings(semester)
    if home is not None and home.code not in listings:
        async with catalog_session(db, user.id) as catalog:
            listings[home.code] = await call_course_info(
                db, user.id, "list_program_courses",
                {"department": home.code, "semester": semester},
                session=catalog,
            )
    courses: list[dict] = []
    # Every cached department is scanned rather than stopping at the first
    # sixty hits: breaking early ordered results by department id, so a match
    # in a late department was invisible while an unrelated one filled the box.
    for code, payload in listings.items():
        owner = departments.by_code(code)
        if owner is None:
            continue
        courses.extend(_match_courses(payload, owner, digits="", title=typed))
    # The student's own department first: an elective search is usually still
    # anchored to what they are studying.
    if home is not None:
        courses.sort(key=lambda item: item["department"] != (home.abbreviation or home.code))
    return {"courses": courses[:40], "searched_departments": len(listings), "scope": "catalog"}


def _listing_key(department_code: str, semester: str) -> str:
    """The cache key ``call_course_info`` stores a department listing under."""
    identity = ("list_program_courses", f"department={department_code}", f"semester={semester}")
    return stable_digest({"namespace": CATALOG_NAMESPACE, "identity": list(identity)})


async def _cached_listings(semester: str) -> dict[str, Any]:
    """Every department listing already in the shared cache, by department code."""
    wanted = {_listing_key(entry.code, semester): entry.code for entry in departments.all_departments()}
    found = await read_many_cached(list(wanted))
    return {wanted[key]: payload for key, payload in found.items()}


def _match_courses(payload: Any, owner: Any, *, digits: str, title: str) -> list[dict]:
    """Rows of one department's listing that answer the query."""
    wanted_title = _search_fold(title)
    matches: list[dict] = []
    for row in _catalog_rows(payload):
        full_code = str(row.get("course_code") or "").strip()
        if not full_code:
            continue
        name = " ".join(str(row.get("name") or "").split())
        short = _short_code(full_code, owner.abbreviation)
        if digits and not re.sub(r"[^0-9]", "", short).startswith(digits):
            continue
        # The code is searched too, so "PHYS213" pasted whole still lands even
        # when it arrives through the title path.
        if wanted_title and wanted_title not in _search_fold(f"{short} {name}"):
            continue
        matches.append({
            "code": short,
            "full_code": full_code,
            "name": name,
            "credits": _credit_value(row.get("credit")),
            "department": owner.abbreviation or owner.code,
        })
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
    compact_course, lookup_department = await _expand_course(db, user.id, course_code, department)
    return {
        "data": await call_course_info(
            db,
            user.id,
            "get_course_info",
            {"department": lookup_department, "semester": semester, "course": compact_course},
        )
    }


def _section_numbers(payload: Any) -> list[str]:
    """Every section number in a ``get_course_info`` payload, in order."""
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        for key, value in item.items():
            if re.sub(r"[^a-z]", "", str(key).lower()) == "section" and isinstance(value, (str, int)):
                number = str(value).strip()
                if number and number not in found:
                    found.append(number)
        for child in item.values():
            visit(child)

    visit(payload)
    return found


class BulkConstraintsRequest(BaseModel):
    semester: str = Field(min_length=1, max_length=20)
    # The curriculum is a couple of dozen courses at most. The cap is here so a
    # crafted request cannot turn one HTTP call into hundreds of SAIS fetches.
    courses: list[str] = Field(min_length=1, max_length=40)
    department: str | None = Field(default=None, max_length=20)


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
    async with catalog_session(db, user.id) as catalog:
        for raw in dict.fromkeys(code.strip() for code in body.courses if code.strip()):
            try:
                compact_course, lookup_department = await _expand_course(
                    db, user.id, raw, body.department or "", session=catalog
                )
                results[raw] = await _constraints_for(
                    db, user, catalog, raw, compact_course, lookup_department, body.semester
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
    async with catalog_session(db, user.id) as catalog:
        compact_course, lookup_department = await _expand_course(
            db, user.id, course_code, department, session=catalog
        )
        return await _constraints_for(
            db, user, catalog, course_code, compact_course, lookup_department, semester
        )


async def _constraints_for(
    db: AsyncSession,
    user: AuthenticatedUser,
    catalog: CatalogSession,
    course_code: str,
    compact_course: str,
    lookup_department: str,
    semester: str,
) -> dict:
    info = await call_course_info(
        db,
        user.id,
        "get_course_info",
        {"department": lookup_department, "semester": semester, "course": compact_course},
        session=catalog,
    )

    context = await db.get(StudentContext, user.id)
    query, code = await _student_department(db, user.id, context)
    student_department = departments.resolve(code or query)
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    cgpa = None
    if snapshot and snapshot.current_credits:
        cgpa = float(snapshot.current_grade_points) / float(snapshot.current_credits)
    # A section reserved for students who still need the course is closed to
    # one who has already passed it, and only the transcript knows which.
    # The course's *owner* department, not the student's: the candidates are
    # transcript spellings of this course ("PHYS213"), so they are built from
    # whoever owns it. Passing the student's department here would look for
    # "EE213", a course that does not exist.
    course_owner = departments.by_code(lookup_department)
    held = prior_grade(
        snapshot.completed_courses if snapshot else [],
        course_candidates(course_code, course_owner, compact_course),
    )

    sections: dict[str, Any] = {}
    for number in _section_numbers(info):
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
            department=student_department.abbreviation if student_department else None,
            surname=context.surname_prefix if context else None,
            cgpa=cgpa,
            year=context.year_of_study if context else None,
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
        "student_department": student_department.abbreviation if student_department else None,
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


def _json_from_agent(value: Any) -> dict[str, Any]:
    content = getattr(value, "content", value)
    if not isinstance(content, str):
        content = json.dumps(json_value(content), ensure_ascii=False)
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Schedule assistant returned an invalid response"
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Schedule assistant returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Schedule assistant response must be an object")
    return parsed


def _completed_codes(snapshot: StudentAcademicSnapshot | None) -> list[str]:
    codes: list[str] = []
    for item in snapshot.completed_courses if snapshot else []:
        if isinstance(item, str) and item.strip():
            codes.append(item.strip())
        elif isinstance(item, dict):
            for key in ("course_code", "courseCode", "code", "course", "ders_kodu"):
                value = item.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    codes.append(str(value).strip())
                    break
    return codes


def _prompt(department: str, semester: str, requested: list[str], completed: list[str]) -> str:
    return PLANNER_PROMPT.format(
        department=department,
        semester=semester,
        requested=json.dumps(requested, ensure_ascii=False),
        completed=json.dumps(completed, ensure_ascii=False),
    )


@router.post("/ai-plan")
async def ai_schedule_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Use one bounded agent run to fill gaps left by direct catalog calls."""
    started_at = time.monotonic()
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    department = await _resolve_department(db, user.id, body.department)
    cache_key, owner_hash = _plan_cache_key(user.id, department, body, snapshot)
    cached = await _cached_plan(cache_key)
    if cached is not None:
        return {
            **cached,
            "cache_hit": True,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }
    agent_record = await manager.get_agent_or_404(db, user.id)
    lock_owner = f"schedule-{uuid4()}"
    if not await manager.acquire_turn_lock(db, agent_record, lock_owner):
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is busy with another message")
    lease = None
    try:
        # The lock's lease is shorter than this run is allowed to take, so it
        # has to be renewed for as long as the run holds it — otherwise it
        # expires mid-run and a chat turn is free to drive the same agent.
        async with manager.turn_lock_held(agent_record.id, lock_owner):
            lease = await manager.lease_for(db, agent_record)
            from app.agents.scholar.context import build_run_dependencies

            dependencies = await build_run_dependencies(db, user.id, lease.resident)
            # Only course identifiers enter the instruction. Display names can
            # be user-authored, so keeping them out prevents prompt-shaped names
            # from changing the planner contract.
            result = await asyncio.wait_for(
                lease.agent.arun(
                    input=_prompt(
                        department,
                        body.semester,
                        [item.code for item in body.courses],
                        _completed_codes(snapshot),
                    ),
                    session_id=f"schedule-{uuid4()}",
                    user_id=str(user.id),
                    dependencies=dependencies,
                    stream=False,
                ),
                timeout=_AGENT_RUN_TIMEOUT_SECONDS,
            )
        payload = _json_from_agent(result)
        # The shape is instructed, not enforced: a model that answers with a
        # null or an object where a list belongs must not become a 502.
        raw_warnings = payload.get("warnings")
        raw_courses = payload.get("courses")
        warnings = [
            warning
            for warning in (raw_warnings if isinstance(raw_warnings, list) else [])
            if isinstance(warning, str)
            and not re.search(r"section|meeting time|şube|ders saat", warning, re.IGNORECASE)
        ]
        response = {
            "courses": [
                course
                for course in (raw_courses if isinstance(raw_courses, list) else [])
                if isinstance(course, dict)
            ],
            "warnings": warnings,
            "source": "ai_verified",
            "cache_hit": False,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }
        # Only a result worth reusing. An empty course list means the run
        # failed — the catalog tools timed out, the model gave up — and caching
        # that turned a transient failure into a permanent one: every later
        # click returned "no verified courses" instantly, from cache, without
        # ever asking again. A six-hour-old outage is not an answer.
        if response["courses"]:
            await _store_plan(cache_key, owner_hash, response)
        return response
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Course recommendation timed out; try again") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("schedule_agent_failed", user_id=str(user.id), error=str(exc))
        report_exception(exc, distinct_id=str(user.id), handler="schedule_recommendation", dependency="agent")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The course recommendation service failed. Department and term were not changed; try this step again.",
        ) from exc
    finally:
        if lease is not None:
            await lease.release()
        # A failed run can leave the request's session mid-transaction, and the
        # lock must come off even then: releasing it on a fresh session keeps a
        # broken transaction from stranding the agent for the whole lease.
        await manager.release_turn_lock_isolated(agent_record.id, lock_owner)
