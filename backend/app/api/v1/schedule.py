"""Course catalog access for the visual schedule builder.

Every read here goes straight to the connected Course Info MCP server through
:mod:`app.campus.course_info` — no Agent run, language model, prompt, memory or
learning pass is involved anywhere in this module, and none of it costs tokens.

``/curriculum`` was the exception until it stopped being one. It ran a bounded
agent and held the same turn lock a chat turn does, which cost a median of 95.8
seconds in production and made opening the planner during a chat turn fail with
a 409. It now reads the student's own curriculum listing directly.
"""

import hashlib
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus import curriculum, departments, prerequisites
from app.campus.course_info import (
    CatalogSession,
    call_course_info,
    catalog_answer_expires_at,
    catalog_key,
    catalog_session,
    department_for_course,
    department_options,
    prefetch,
    resolve_department,
    section_numbers,
)
from app.campus.eligibility import course_candidates, evaluate, prior_grade, validated_constraint_rows
from app.campus.warmer import record_wanted_courses
from app.core.digest import owner_digest, stable_digest
from app.core.persistent_cache import read_cached, read_many_cached, write_cached
from app.core.ttl_cache import TTLCache
from app.db.models import StudentAcademicSnapshot, StudentContext
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import report_exception
from app.planning.catalog import normalize_sections
from app.planning.eligibility import academic_evidence_fresh, issue_eligibility_token, planning_context_fingerprint
from app.planning.mcp_bridge import sync_student_context_from_sais
from app.planning.models import PlanChanges, PlanConflictError, PlanSection, PlanValidationError
from app.planning.service import current_term
from app.planning.workspace import projection_from_state
from app.planning.workspace import read_timetable as read_canonical_timetable
from app.planning.workspace import undo_timetable as undo_canonical_timetable
from app.planning.workspace import update_timetable as update_canonical_timetable

router = APIRouter()
logger = get_logger(__name__)
ISTANBUL = ZoneInfo("Europe/Istanbul")
_SAFE_TERM = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

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


class PrerequisiteRejectionOut(BaseModel):
    course_code: str
    course_label: str
    prerequisite_course_codes: list[str]
    prerequisite_course_labels: list[str]


class CurriculumCourseOut(BaseModel):
    code: str
    display_code: str
    name: str
    credits: float
    sections: list[Any] = Field(default_factory=list)


class CurriculumPlanResponse(BaseModel):
    courses: list[CurriculumCourseOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    prerequisite_rejections: list[PrerequisiteRejectionOut] = Field(default_factory=list)
    source: str
    cache_hit: bool
    duration_ms: int


class CurriculumFullCourseOut(BaseModel):
    semester: int
    semester_completed: bool
    course_code: str
    course_name: str
    grade: str | None = None
    status: Literal["completed", "failed", "outstanding"]
    credits: float = 0


class CurriculumFullResponse(BaseModel):
    courses: list[CurriculumFullCourseOut] = Field(default_factory=list)
    source: str
    fetched_at: Any | None = None
    warning: str | None = None


class CourseSectionsResponse(BaseModel):
    """Raw catalog data plus the server-normalized section contract."""

    data: Any
    sections: list[PlanSection] = Field(default_factory=list)


def _signed_course_code(compact_course: str, lookup_department: str) -> str:
    owner = departments.by_code(lookup_department)
    return _short_code(compact_course, owner.abbreviation if owner else "")


def _tag_catalog_sections(rows: list[dict[str, Any]], compact_course: str, lookup_department: str) -> list[dict[str, Any]]:
    signed_code = _signed_course_code(compact_course, lookup_department)
    return [
        {**row, "eligibility_course_code": signed_code, "eligibility_raw_code": compact_course}
        for row in rows
    ]


def _eligibility_is_verified(
    rows: list[dict[str, Any]], profile: "_StudentProfile", prior: str | None
) -> bool:
    """Return whether the inputs needed by these rows came from SAIS.

    ``evaluate`` intentionally treats missing values as unknown so it can be
    useful for explanations. A normal timetable has a stricter contract: an
    unknown dimension must be surfaced as unverified and cannot be saved.
    """
    context = profile.context
    if context is None or context.verified_at is None or not profile.department:
        return False
    if not academic_evidence_fresh(context.verified_at, profile.snapshot_fetched_at):
        return False
    if not rows:
        return True
    if not profile.has_snapshot and any(
        str(row.get("start_grade") or row.get("startGrade") or row.get("end_grade") or row.get("endGrade") or "").strip()
        for row in rows
    ):
        return False
    for row in rows:
        start_char = str(row.get("start_char") or row.get("startChar") or "").strip()
        end_char = str(row.get("end_char") or row.get("endChar") or "").strip()
        if (start_char or end_char) and not context.surname_prefix:
            return False
        if any(row.get(name) not in (None, "") for name in ("min_cgpa", "minCgpa", "max_cgpa", "maxCgpa")) and profile.cgpa is None:
            return False
        if any(row.get(name) not in (None, "") for name in ("min_year", "minYear", "max_year", "maxYear")) and context.year_of_study is None:
            return False
    return True

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


class TimetableUpdateIn(BaseModel):
    """One explicit, revision-checked update to the canonical plan."""

    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    changes: PlanChanges


class TimetableUndoIn(BaseModel):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


def _canonical_response(envelope) -> dict[str, Any]:
    """Expose the resource envelope plus the old chat projection."""

    body = envelope.model_dump(mode="json")
    projection = projection_from_state(envelope.state)
    body.update(projection)
    return body


def _conflict_response(exc: PlanConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "revision_conflict",
            "detail": str(exc),
            "current": _canonical_response(exc.current),
        },
    )


@router.put("/timetable")
async def save_timetable(
    body: TimetableIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Compatibility projection writer for one release of old browsers.

    New clients use PATCH with a typed ``PlanChanges`` request.  This route
    still accepts the old courses/blocks projection and routes it through the
    same revisioned owner service, so it cannot create a second write path.
    """
    current = await read_canonical_timetable(db, user.id, body.term)
    payload = {
        "term": body.term,
        "courses": [course.model_dump() for course in body.courses],
        "busy_blocks": [block.model_dump() for block in body.busy_blocks],
    }
    try:
        envelope = await update_canonical_timetable(
            db,
            user.id,
            body.term,
            PlanChanges(operation="replace_projection", projection=payload),
            current.revision,
            f"legacy-put:{uuid4()}",
        )
    except PlanConflictError as exc:  # pragma: no cover - current was just read
        return _conflict_response(exc)
    return {"saved": True, "courses": len(body.courses), **_canonical_response(envelope)}


@router.get("/timetable/canonical")
async def read_canonical_timetable_route(
    term: str = Query(min_length=3, max_length=32),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read the complete server-owned planner state."""

    return _canonical_response(await read_canonical_timetable(db, user.id, term))


@router.patch("/timetable")
async def update_timetable(
    body: TimetableUpdateIn,
    term: str = Query(min_length=3, max_length=32),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        envelope = await update_canonical_timetable(
            db,
            user.id,
            term,
            body.changes,
            body.expected_revision,
            body.idempotency_key,
        )
    except PlanConflictError as exc:
        return _conflict_response(exc)
    except PlanValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _canonical_response(envelope)


@router.post("/timetable/undo")
async def undo_timetable(
    body: TimetableUndoIn,
    term: str = Query(min_length=3, max_length=32),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        envelope = await undo_canonical_timetable(
            db,
            user.id,
            term,
            body.expected_revision,
            body.idempotency_key,
        )
    except PlanConflictError as exc:
        return _conflict_response(exc)
    except PlanValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _canonical_response(envelope)


@router.get("/timetable")
async def read_timetable(
    term: str | None = Query(default=None, min_length=3, max_length=32),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read the canonical plan with the legacy projection at the top level."""

    requested_term = term or current_term()
    return _canonical_response(await read_canonical_timetable(db, user.id, requested_term))


@router.get("/timetable/export.ics")
async def export_timetable_ics(
    term: str = Query(min_length=3, max_length=32),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    revision: int = Query(ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export one saved timetable revision as a bounded weekly calendar."""
    if not _SAFE_TERM.fullmatch(term):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "term contains unsupported characters")
    if start_date is None or end_date is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "start_date and end_date are required to export a semester calendar",
        )
    if end_date <= start_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end_date must be after start_date")
    if (end_date - start_date).days > 240:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "calendar export cannot span more than 240 days")

    from icalendar import Calendar, Event

    envelope = await read_canonical_timetable(db, user.id, term)
    if envelope.revision != revision:
        raise HTTPException(status.HTTP_409_CONFLICT, "the timetable changed; reload it before exporting")
    if not envelope.state.entries:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "there are no timetable entries to export")
    if envelope.state.generation_error or envelope.state.unscheduled_courses:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the timetable is incomplete; generate a current schedule before exporting",
        )

    calendar = Calendar()
    calendar.add("prodid", "-//Devrimo//Schedule Planner//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-timezone", "Europe/Istanbul")
    mode_label = " (what-if)" if envelope.state.what_if or envelope.state.ignore_constraints else ""
    calendar.add("x-wr-calname", f"Devrimo {term} schedule{mode_label}")
    day_number = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
    now = datetime.now(timezone.utc)
    # RFC 5545 requires RRULE UNTIL to be UTC when DTSTART carries a TZID.
    until = datetime.combine(end_date, dt_time(23, 59, 59), tzinfo=ISTANBUL).astimezone(timezone.utc)
    export_owner = hashlib.sha256(str(user.id).encode("utf-8")).hexdigest()[:16]

    for entry in envelope.state.entries:
        weekday = day_number.get(entry.day)
        if weekday is None:
            continue
        offset = (weekday - start_date.weekday()) % 7
        first_date = start_date + timedelta(days=offset)
        if first_date > end_date:
            continue
        starts_at = datetime.combine(
            first_date,
            dt_time(entry.start_minute // 60, entry.start_minute % 60),
            tzinfo=ISTANBUL,
        )
        ends_at = starts_at + timedelta(minutes=entry.duration_minutes)
        # Generated solver entries receive fresh UUIDs on every run.  The
        # export identity follows the class occurrence instead, so calendar
        # clients update an existing event when the student regenerates the
        # same section rather than creating duplicates.
        stable = hashlib.sha256(
            f"{user.id}:{term}:{entry.code}:{entry.section}:{entry.day}:{entry.start_minute}:{entry.duration_minutes}:{entry.room}:{entry.instructor}".encode("utf-8")
        ).hexdigest()[:24]
        event = Event()
        event.add("uid", f"{stable}.{export_owner}@devrimo")
        event.add("dtstamp", now)
        event.add("dtstart", starts_at)
        event.add("dtend", ends_at)
        event.add("rrule", {"freq": "weekly", "until": until})
        label = f"{entry.code}{f' · Section {entry.section}' if entry.section else ''}"
        if mode_label:
            label = f"[What-if] {label}"
        event.add("summary", label if not entry.name else f"{label} · {entry.name}")
        description = []
        if entry.instructor:
            description.append(f"Instructor: {entry.instructor}")
        description.append(f"Term: {term}")
        if envelope.state.what_if or envelope.state.ignore_constraints:
            description.append("What-if plan; verify eligibility before registration.")
        event.add("description", "\n".join(description))
        if entry.room and entry.room.strip().casefold() not in {"tba", "unknown", "-", "—"}:
            event.add("location", entry.room)
            event.add(
                "url",
                "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": f"METU {entry.room}"}),
            )
        event.add("sequence", envelope.revision)
        calendar.add_component(event)

    return Response(
        content=calendar.to_ical(),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="devrimo-{term}-schedule.ics"',
            "Cache-Control": "no-store",
        },
    )


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


def _published_terms(value: Any) -> list[str]:
    """Extract only bounded METU term codes from the catalog response."""

    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in {"semester", "semester_code", "term", "term_code", "code"}:
                    match = re.fullmatch(r"20\d{2}[1-3]", str(child).strip())
                    if match:
                        found.add(match.group(0))
                visit(child)
        elif isinstance(item, list):
            for child in item[:200]:
                visit(child)
        elif isinstance(item, str):
            found.update(re.findall(r"20\d{2}[1-3]", item))

    visit(value)
    return sorted(found, reverse=True)


@router.get("/semesters")
async def published_semesters(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the bounded term list published by Course Info."""

    try:
        payload = await call_course_info(db, user.id, "get_departments_and_semesters", {})
        terms = _published_terms(payload)
    except HTTPException as exc:
        return {"semesters": [current_term()], "source": "fallback", "warning": str(exc.detail)}
    return {"semesters": terms or [current_term()], "source": "catalog", "warning": None}


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


@router.get("/courses/{course_code}", response_model=CourseSectionsResponse)
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
        data = await call_course_info(
            db,
            user.id,
            "get_course_info",
            {"department": lookup_department, "semester": semester, "course": compact_course},
            session=catalog,
        )
        return {"data": data, "sections": _tag_catalog_sections(normalize_sections(data), compact_course, lookup_department)}


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
                data = await call_course_info(
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
                results[raw] = {"data": data, "sections": _tag_catalog_sections(normalize_sections(data), compact_course, lookup_department)}
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
    has_snapshot: bool
    snapshot_fetched_at: datetime | None
    context_fingerprint: str


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
        has_snapshot=snapshot is not None,
        snapshot_fetched_at=snapshot.fetched_at if snapshot else None,
        context_fingerprint=planning_context_fingerprint(
            {
                "department": getattr(context, "department", None),
                "program_code": getattr(context, "program_code", None),
                "degree_level": getattr(context, "degree_level", None),
                "year_of_study": getattr(context, "year_of_study", None),
                "surname_prefix": getattr(context, "surname_prefix", None),
                "campus": getattr(context, "campus", None),
                "verified_at": getattr(context, "verified_at", None),
                "confirmed_at": getattr(context, "confirmed_at", None),
            }
        ),
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
    info_values = {"department": lookup_department, "semester": semester, "course": compact_course}
    info = await call_course_info(
        db,
        user.id,
        "get_course_info",
        info_values,
        session=catalog,
    )
    info_expires_at = await catalog_answer_expires_at(db, user.id, "get_course_info", info_values)
    normalized_sections = {
        str(row.get("section") or ""): row
        for row in normalize_sections(info)
        if isinstance(row, dict)
    }

    prerequisite_ok = True
    prerequisite_reason = ""
    prerequisite_known = True
    prerequisite_expires_at: datetime | None = None
    try:
        prerequisite_values = {
            "department": lookup_department,
            "semester": semester,
            "course": compact_course,
        }
        prerequisite_payload = await call_course_info(
            db,
            user.id,
            "get_course_prerequisites",
            prerequisite_values,
            session=catalog,
        )
        prerequisite_expires_at = await catalog_answer_expires_at(
            db, user.id, "get_course_prerequisites", prerequisite_values
        )
        prerequisite_rows = prerequisites._rows(prerequisite_payload)
        missing = prerequisites.unmet_prerequisites(prerequisite_rows, profile.completed)
        if missing:
            prerequisite_ok = False
            prerequisite_reason = f"Missing prerequisite: {prerequisites.display_code(missing[0])}"
        if prerequisite_rows and not profile.has_snapshot:
            prerequisite_known = False
    except (HTTPException, ValueError):
        prerequisite_known = False
        prerequisite_reason = "Prerequisite data is temporarily unavailable"

    # A section reserved for students who still need the course is closed to
    # one who has already passed it, and only the transcript knows which.
    # The course's *owner* department, not the student's: the candidates are
    # transcript spellings of this course ("PHYS213"), so they are built from
    # whoever owns it. Passing the student's department here would look for
    # "EE213", a course that does not exist.
    course_owner = departments.by_code(lookup_department)
    signed_course_code = _signed_course_code(compact_course, lookup_department)
    held = prior_grade(
        profile.completed,
        course_candidates(course_code, course_owner, compact_course),
    )

    sections: dict[str, Any] = {}
    for number in section_numbers(info):
        try:
            constraint_values = {
                "department": lookup_department,
                "semester": semester,
                "course": compact_course,
                "section": number,
            }
            payload = await call_course_info(
                db,
                user.id,
                "get_section_constraints",
                constraint_values,
                session=catalog,
            )
            constraint_expires_at = await catalog_answer_expires_at(
                db, user.id, "get_section_constraints", constraint_values
            )
        except HTTPException as exc:
            # A section whose table cannot be read must not fail the whole
            # course: the student still gets its times, just no verdict.
            logger.warning("section_constraints_failed", course=compact_course, section=number, detail=exc.detail)
            sections[number] = {
                "rows": [],
                "eligible": None,
                "eligibility_status": "unavailable",
                "reason": "Eligibility data is temporarily unavailable",
            }
            continue
        rows = _constraint_rows(payload)
        if rows is None:
            sections[number] = {
                "rows": [],
                "eligible": None,
                "eligibility_status": "unavailable",
                "reason": "Eligibility data was not returned in a valid form",
            }
            continue
        verdict = evaluate(
            rows,
            department=profile.department.abbreviation if profile.department else None,
            surname=profile.context.surname_prefix if profile.context else None,
            cgpa=profile.cgpa,
            year=profile.context.year_of_study if profile.context else None,
            prior_grade=held,
        )
        section_meetings = normalized_sections.get(number, {}).get("meetings", [])
        verified = _eligibility_is_verified(rows, profile, held) and prerequisite_known and bool(section_meetings)
        eligible = verdict.eligible and prerequisite_ok
        if not prerequisite_ok and prerequisite_known:
            verified = True
        if not prerequisite_known:
            eligible = None
        elif not prerequisite_ok:
            eligible = False
        reason = verdict.reason
        if prerequisite_reason:
            reason = prerequisite_reason if not prerequisite_ok or not prerequisite_known else reason
        token = (
            issue_eligibility_token(
                user.id,
                semester,
                signed_course_code,
                number,
                eligibility_status="verified",
                eligible=eligible is True,
                context_verified_at=profile.context.verified_at if profile.context else None,
                snapshot_fetched_at=profile.snapshot_fetched_at,
                meetings=section_meetings,
                source_expires_at=min(
                    value for value in (info_expires_at, constraint_expires_at, prerequisite_expires_at) if value is not None
                ) if any(value is not None for value in (info_expires_at, constraint_expires_at, prerequisite_expires_at)) else None,
                context_fingerprint=profile.context_fingerprint,
            )
            if verified and eligible is True
            else None
        )
        sections[number] = {
            "rows": rows,
            "eligible": eligible if verified else None,
            "eligibility_status": "verified" if verified else "unverified",
            "reason": reason if verified else "Student context, transcript, or prerequisite data is not verified",
            "eligibility_token": token,
            "eligibility_course_code": signed_course_code,
            "eligibility_raw_code": compact_course,
        }

    return {
        "course": compact_course,
        "department": lookup_department,
        "student_department": profile.department.abbreviation if profile.department else None,
        "your_grade_in_this_course": held,
        "sections": sections,
    }


def _constraint_rows(payload: Any) -> list[dict[str, Any]] | None:
    """The eligibility rows out of whatever shape the tool returned."""
    return validated_constraint_rows(payload)


_CURRICULUM_NAMESPACE = "schedule-curriculum"
# Bumped when the shape of a stored answer changes, so a deploy cannot spend six
# hours serving results built by the previous version of this code.
_CURRICULUM_VERSION = 5


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
    """Ungraded courses in the first unchecked SAIS curriculum semester."""
    board = await call_course_info(
        db, user.id, "get_student_curriculum", {}, session=catalog
    )
    try:
        rows = curriculum.next_semester_courses(board)
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return [{**row, "_rank": index} for index, row in enumerate(rows)], []


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
) -> tuple[list[dict], list[str], list[dict]]:
    rows, warnings = await _category_rows(db, user, catalog, department)

    candidates: dict[str, dict] = {}
    for row in rows:
        code = curriculum.normalise_code(row.get("course_code"))
        if code is None:
            label = " ".join(str(row.get("course_code") or "").split())
            if label:
                warnings.append(f"{label}: this code names no department we know, so it was left out.")
            continue
        candidates.setdefault(
            code,
            {
                "code": code,
                "display_code": prerequisites.display_code(code),
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
    approved, prerequisite_rejections, prerequisite_warnings = await prerequisites.filter_courses(
        db, user.id, catalog, semester, kept, completed
    )
    warnings.extend(prerequisite_warnings)
    return (
        [
            {key: value for key, value in course.items() if not key.startswith("_")}
            for course in approved[: curriculum.MAX_COURSES]
        ],
        warnings,
        [rejection.as_dict() for rejection in prerequisite_rejections],
    )


@router.post("/curriculum", response_model=CurriculumPlanResponse)
async def curriculum_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The courses this student still has to take, offered this term.

    Read from the first unchecked semester in the SAIS Curriculum tab.
    Only courses with a blank grade are considered, then verified as offered.
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
            courses, warnings, prerequisite_rejections = await _curriculum_courses(
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
            "prerequisite_rejections": [],
            "source": "sais_curriculum",
            "cache_hit": False,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }

    response = {
        "courses": courses,
        "warnings": warnings,
        "prerequisite_rejections": prerequisite_rejections,
        "source": "sais_curriculum",
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


@router.post("/ai-plan", response_model=CurriculumPlanResponse)
async def ai_schedule_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The previous name for :func:`curriculum_plan`.

    Kept for one release. A browser holding a cached bundle still calls this
    route by name, and deleting it in the same deploy that adds the new one
    breaks every tab that was already open.
    """
    return await curriculum_plan(body, user, db)


@router.post("/curriculum/full", response_model=CurriculumFullResponse)
async def curriculum_full(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return every curriculum row for the course-management workspace."""
    try:
        async with catalog_session(db, user.id) as catalog:
            board = await call_course_info(db, user.id, "get_student_curriculum", {}, session=catalog)
        rows = curriculum.all_semester_courses(board)
    except (HTTPException, ValueError) as exc:
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        return {"courses": [], "source": "sais_curriculum", "warning": detail}
    return {
        "courses": [
            {
                "semester": row["semester"],
                "semester_completed": row["semester_completed"],
                "course_code": row["course_code"],
                "course_name": row["course_name"],
                "grade": row["grade"],
                "status": row["status"],
                "credits": _credit_value(row.get("credit")),
            }
            for row in rows
        ],
        "source": "sais_curriculum",
        "fetched_at": None,
        "warning": None,
    }
