from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.directory import METU_ID
from app.db.models import CourseOffering, CourseRule, PlanningPolicy, StudentAcademicSnapshot
from app.logging import get_logger
from app.planning.solver import SolverGroup, enumerate_solutions

logger = get_logger(__name__)

# Registration data can change during a day, while a transient SAIS outage
# should not erase a usable plan. Both the HTTP and workspace adapters use this
# one freshness window and the same stale-data fallback policy.
PLANNING_SNAPSHOT_MAX_AGE = timedelta(hours=6)

GRADE_POINTS = {
    "AA": 4.0,
    "BA": 3.5,
    "BB": 3.0,
    "CB": 2.5,
    "CC": 2.0,
    "DC": 1.5,
    "DD": 1.0,
    "FD": 0.5,
    "FF": 0.0,
}


def current_term(now: datetime | None = None) -> str:
    """The term a student is registering for, as METU codes it.

    ``YYYY`` + a part number, where the year is the one the academic year
    starts in: 20261 is 2026-2027 Fall, 20253 the summer school running in
    calendar 2026.
    """
    moment = now or datetime.now(UTC)
    if moment.month >= 8:
        return f"{moment.year}1"
    if moment.month <= 5:
        return f"{moment.year - 1}2"
    return f"{moment.year - 1}3"


class SemesterPlanRequest(BaseModel):
    # Defaulted rather than required. A model asked to plan "this semester" has
    # no way to know METU's code for it, so it omitted the field and every call
    # failed validation before reaching any planning logic.
    term: str = Field(default_factory=current_term, min_length=3, max_length=32)
    required_courses: list[str] = Field(default_factory=list)
    preferred_courses: list[str] = Field(default_factory=list)
    excluded_courses: list[str] = Field(default_factory=list)
    retake_courses: list[str] = Field(default_factory=list)
    min_credits: float | None = Field(default=None, ge=0, le=60)
    max_credits: float | None = Field(default=None, ge=0, le=60)
    days_off: list[str] = Field(default_factory=list)
    earliest_start: str | None = None
    latest_end: str | None = None


def _code(value: str) -> str:
    return "".join(value.upper().split())


def _grade_at_least(actual: str, required: str) -> bool:
    return GRADE_POINTS.get(actual.upper(), -1) >= GRADE_POINTS.get(required.upper(), 99)


def _prerequisite_met(rule: Any, completed: dict[str, str], cgpa: float) -> tuple[bool, str | None]:
    if not rule:
        return True, None
    if isinstance(rule, list):
        rule = {"all": rule}
    if not isinstance(rule, dict):
        return False, "invalid prerequisite rule"
    if "course" in rule:
        code = _code(str(rule["course"]))
        grade = completed.get(code)
        required = str(rule.get("min_grade", "DD"))
        return (grade is not None and _grade_at_least(grade, required), f"requires {code} with {required} or better")
    if "min_cgpa" in rule:
        try:
            required = float(rule["min_cgpa"])
        except (TypeError, ValueError):
            return False, "invalid prerequisite rule"
        return (cgpa >= required, f"requires CGPA {required:.2f}")
    if "all" in rule:
        if not isinstance(rule["all"], list):
            return False, "invalid prerequisite rule"
        failures = []
        for child in rule["all"]:
            met, reason = _prerequisite_met(child, completed, cgpa)
            if not met:
                failures.append(reason or "unmet prerequisite")
        return (not failures, "; ".join(failures) if failures else None)
    if "any" in rule:
        if not isinstance(rule["any"], list):
            return False, "invalid prerequisite rule"
        reasons = []
        for child in rule["any"]:
            met, reason = _prerequisite_met(child, completed, cgpa)
            if met:
                return True, None
            reasons.append(reason or "unmet prerequisite")
        return False, "one of: " + ", ".join(reasons)
    return False, "invalid or unsupported prerequisite rule"


def _minutes(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return fallback


def _meetings(offering: CourseOffering) -> list[tuple[str, int, int]]:
    result = []
    for meeting in offering.schedule or []:
        if not isinstance(meeting, dict) or not meeting.get("day"):
            continue
        result.append(
            (
                str(meeting["day"]).lower(),
                _minutes(str(meeting.get("start", "")), 0),
                _minutes(str(meeting.get("end", "")), 24 * 60),
            )
        )
    return result


def _fits_user_constraints(offering: CourseOffering, request: SemesterPlanRequest) -> tuple[bool, str | None]:
    days_off = {day.lower() for day in request.days_off}
    earliest = _minutes(request.earliest_start, 0)
    latest = _minutes(request.latest_end, 24 * 60)
    for day, start, end in _meetings(offering):
        if day in days_off:
            return False, f"meets on requested day off ({day})"
        if start < earliest:
            return False, f"starts before {request.earliest_start}"
        if end > latest:
            return False, f"ends after {request.latest_end}"
    return True, None


def _best_combination(
    groups: list[tuple[str, list[CourseOffering]]],
    required: set[str],
    preferred: set[str],
    max_credits: float,
) -> list[CourseOffering]:
    solver_groups = [SolverGroup(key=code, options=tuple(sections)) for code, sections in groups]
    solutions = enumerate_solutions(
        solver_groups,
        lambda offering: _meetings(offering),
        max_solutions=200,
        max_nodes=150_000,
        allow_skip=lambda group: group.key not in required,
        allow_option=lambda selected, offering: (
            sum(float(choice.option.credits) for choice in selected) + float(offering.credits) <= max_credits
        ),
    )
    best: list[CourseOffering] = []
    best_score = (-1, -1.0, -1, 0)
    for solution in solutions:
        selected = [choice.option for choice in solution]
        credits = sum(float(item.credits) for item in selected)
        codes = {_code(item.course_code) for item in selected}
        days = {day for item in selected for day, _, _ in _meetings(item)}
        score = (len(codes & required), credits, len(codes & preferred), -len(days))
        if score > best_score:
            best_score = score
            best = selected
    return best


async def prepare_planning_snapshot(
    db: AsyncSession,
    user_id: UUID,
    term: str,
    *,
    max_age: timedelta = PLANNING_SNAPSHOT_MAX_AGE,
) -> tuple[StudentAcademicSnapshot | None, dict[str, Any]]:
    """Return a term snapshot under one explicit freshness/failure policy.

    Missing or stale data triggers one bounded SAIS refresh. A failed refresh
    preserves an existing stale snapshot and reports that fact to callers; a
    missing snapshot remains unavailable so the planner cannot treat an empty
    result as verified academic history. The refresh runs in its own short
    session, so the caller rolls back its read transaction before reloading the
    committed snapshot.
    """

    async def read_snapshot() -> StudentAcademicSnapshot | None:
        # The refresh runs in another session and an academic-data purge may
        # delete this identity while it is in flight. Force a new database row
        # image after rollback instead of trusting SQLAlchemy's identity map.
        return await db.scalar(
            select(StudentAcademicSnapshot)
            .where(StudentAcademicSnapshot.user_id == user_id, StudentAcademicSnapshot.term == term)
            .execution_options(populate_existing=True)
        )

    snapshot = await read_snapshot()
    now = datetime.now(UTC)
    fetched_at = snapshot.fetched_at if snapshot is not None else None
    if fetched_at is not None and fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    is_fresh = fetched_at is not None and now - fetched_at <= max_age
    if is_fresh:
        return snapshot, {
            "status": "fresh",
            "fresh": True,
            "refresh_attempted": False,
            "fetched_at": snapshot.fetched_at.isoformat(),
        }

    # Do not hold an API connection while the independent SAIS session is
    # starting and reading. The planning pool is deliberately small, and a
    # burst of stale planners would otherwise starve the refresh itself.
    await db.rollback()
    refresh_error = False
    try:
        from app.planning.mcp_bridge import sync_planning_snapshot_from_sais

        reached_sais = await sync_planning_snapshot_from_sais(user_id, term)
    except Exception as exc:  # campus failures are a stale-data outcome here
        logger.warning("planning_snapshot_refresh_failed", user_id=str(user_id), term=term, error=str(exc))
        reached_sais = False
        refresh_error = True
    await db.rollback()
    refreshed = await read_snapshot()
    if refreshed is None:
        return None, {
            "status": "unavailable",
            "fresh": False,
            "refresh_attempted": True,
            "refresh_succeeded": bool(reached_sais),
            "refresh_error": refresh_error,
            "fetched_at": None,
        }

    refreshed_at = refreshed.fetched_at
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=UTC)
    changed = fetched_at is None or refreshed_at > fetched_at
    return refreshed, {
        "status": "fresh" if changed and reached_sais else "stale",
        "fresh": bool(changed and reached_sais),
        "refresh_attempted": True,
        "refresh_succeeded": bool(reached_sais),
        "refresh_error": refresh_error,
        "fetched_at": refreshed.fetched_at.isoformat(),
    }


async def plan_semester(db: AsyncSession, user_id: UUID, request: SemesterPlanRequest) -> dict:
    snapshot, snapshot_status = await prepare_planning_snapshot(db, user_id, request.term)
    if snapshot is None:
        return {
            "status": "needs_academic_snapshot",
            "detail": "Refresh the student's SAIS transcript and current registration before planning.",
            "term": request.term,
            "freshness": {"academic_snapshot": snapshot_status},
        }
    offerings = (
        await db.execute(select(CourseOffering).where(CourseOffering.term == request.term))
    ).scalars().all()
    rules = {rule.course_code: rule for rule in (await db.execute(select(CourseRule))).scalars()}
    policy = (
        await db.execute(
            select(PlanningPolicy)
            .where(PlanningPolicy.organization_id == METU_ID, PlanningPolicy.active.is_(True))
            .order_by(PlanningPolicy.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    policy_rules = policy.rules if policy else {}
    max_credits = min(float(request.max_credits or policy_rules.get("max_credits", 20)), 60)
    min_credits = float(request.min_credits or policy_rules.get("min_credits", 0))
    current_credits = float(snapshot.current_credits)
    current_points = float(snapshot.current_grade_points)
    cgpa = current_points / current_credits if current_credits else 0.0
    completed = {
        _code(str(item.get("course_code", ""))): str(item.get("grade", ""))
        for item in snapshot.completed_courses
        if isinstance(item, dict) and item.get("course_code") and item.get("grade")
    }
    required = {_code(item) for item in request.required_courses}
    preferred = {_code(item) for item in request.preferred_courses}
    excluded = {_code(item) for item in request.excluded_courses}
    retakes = {_code(item) for item in request.retake_courses}
    eligible: dict[str, list[CourseOffering]] = defaultdict(list)
    exclusions: list[dict] = []
    for offering in offerings:
        code = _code(offering.course_code)
        reason = None
        if code in excluded:
            reason = "excluded by the student"
        elif code in completed and code not in retakes:
            reason = "already completed"
        else:
            rule = rules.get(code)
            if rule is None:
                reason = "prerequisite data unavailable"
                met, prerequisite_reason = False, reason
            else:
                met, prerequisite_reason = _prerequisite_met(rule.prerequisites, completed, cgpa)
            if not met:
                reason = prerequisite_reason
            else:
                fits, constraint_reason = _fits_user_constraints(offering, request)
                if not fits:
                    reason = constraint_reason
        if reason:
            exclusions.append({"course_code": code, "section": offering.section, "reason": reason})
        else:
            eligible[code].append(offering)
    groups = sorted(
        eligible.items(),
        key=lambda item: (item[0] not in required, item[0] not in preferred, item[0]),
    )
    selected = _best_combination(groups, required, preferred, max_credits)
    selected_codes = {_code(item.course_code) for item in selected}
    missing_required = sorted(required - selected_codes)
    selected_credits = sum(float(item.credits) for item in selected)
    projected = (
        (current_points + selected_credits * 4.0) / (current_credits + selected_credits)
        if current_credits + selected_credits
        else 4.0
    )
    status = "ok"
    if missing_required or selected_credits < min_credits:
        status = "constraints_unsatisfied"
    return {
        "status": status,
        "term": request.term,
        "maximum_semester_gpa": 4.0 if selected else None,
        "projected_cumulative_gpa": round(projected, 3),
        "current_cumulative_gpa": round(cgpa, 3),
        "selected_credits": round(selected_credits, 2),
        "courses": [
            {
                "course_code": _code(item.course_code),
                "section": item.section,
                "title": item.title,
                "credits": float(item.credits),
                "schedule": item.schedule,
                "source_url": item.source_url,
            }
            for item in selected
        ],
        "missing_required_courses": missing_required,
        "excluded_options": exclusions,
        "assumptions": [
            "Every selected course receives AA (4.00).",
            "Offerings, prerequisites, and the academic snapshot are treated as fresh only at their timestamps.",
            "Only one section of each course is selected and overlapping meetings are rejected.",
        ],
        "freshness": {
            "academic_snapshot": snapshot.fetched_at.isoformat(),
            "academic_snapshot_status": snapshot_status,
            "offerings": max((item.fetched_at for item in offerings), default=None).isoformat() if offerings else None,
            "policy_revision": policy.revision if policy else None,
            "calculated_at": datetime.now(UTC).isoformat(),
        },
    }


def _course_key(row: object) -> tuple[str, str]:
    if not isinstance(row, dict):
        return ("", "")
    code = "".join(str(row.get("course_code") or row.get("code") or "").upper().split())
    return (code, str(row.get("section") or ""))


def merge_courses(stored: list[dict], incoming: list[dict]) -> list[dict]:
    """A refreshed course list laid over the stored one, not replacing it.

    A transcript accumulates: a course that was on it last month is still on it
    today, so a read that comes back short is a failed read rather than a
    student who un-took four courses. Rows the new read does carry win — a
    repeated course now has its passing grade — and rows it does not are kept.
    """
    merged = {_course_key(row): row for row in stored if isinstance(row, dict)}
    for row in incoming:
        if isinstance(row, dict):
            merged[_course_key(row)] = row
    return list(merged.values())


async def upsert_academic_snapshot(
    db: AsyncSession,
    user_id: UUID,
    term: str,
    *,
    completed_courses: list[dict] | None,
    enrolled_courses: list[dict] | None,
    current_credits: Decimal | float | None,
    current_grade_points: Decimal | float | None,
    source: str = "sais",
) -> StudentAcademicSnapshot:
    """Store what this read actually learned, and nothing more.

    ``None`` means "not read this time" and leaves the stored value alone.
    An empty list means "read, and there genuinely are none" — the weekly
    schedule of a term the student has not registered for yet.

    The distinction exists because every field here used to be assigned
    unconditionally: one SAIS read that reached the server but returned an
    unparseable transcript replaced sixteen completed courses with none, along
    with the credits and grade points, and nothing said it had happened.
    """
    snapshot = await db.get(StudentAcademicSnapshot, (user_id, term))
    if snapshot is None:
        snapshot = StudentAcademicSnapshot(user_id=user_id, term=term)
        db.add(snapshot)
    if completed_courses is not None:
        snapshot.completed_courses = merge_courses(snapshot.completed_courses or [], completed_courses)
    if enrolled_courses is not None:
        # Replaced, not merged: dropping a course is a real thing that happens,
        # and this list is small enough that a failed read is reported as None.
        snapshot.enrolled_courses = enrolled_courses
    if current_credits is not None:
        snapshot.current_credits = current_credits
    if current_grade_points is not None:
        snapshot.current_grade_points = current_grade_points
    snapshot.source = source
    snapshot.fetched_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot
