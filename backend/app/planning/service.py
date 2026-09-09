from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.directory import METU_ID
from app.campus.prerequisites import evaluate_prerequisites
from app.db.models import CourseOffering, CourseRule, PlanningPolicy, StudentAcademicSnapshot, StudentContext
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
PASSING_GRADE_TOKENS = frozenset({"AA", "BA", "BB", "CB", "CC", "DC", "DD", "S", "EX", "P"})


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
    actual_token = str(actual or "").strip().upper().split()[0] if actual else ""
    required_token = str(required or "DD").strip().upper().split()[0] if required else "DD"
    # ``S``/``EX``/``P`` are pass marks without a position on the letter-grade
    # scale. They satisfy a plain pass minimum or an explicitly pass-shaped
    # rule; an unknown minimum must never turn into an admission.
    if required_token in {"S", "EX", "P"}:
        return actual_token in PASSING_GRADE_TOKENS
    return GRADE_POINTS.get(actual_token, -1) >= GRADE_POINTS.get(required_token, 99)


def _prerequisite_met(
    rule: Any,
    completed: dict[str, str],
    cgpa: float | None,
    *,
    program_code: str | None = None,
    curriculum_version: str | None = None,
) -> tuple[bool | None, str | None]:
    if not rule:
        return True, None
    if isinstance(rule, list):
        rule = {"all": rule}
    if not isinstance(rule, dict):
        return False, "invalid prerequisite rule"
    # Published catalog revisions expose typed prerequisite groups. Reuse the
    # same evaluator used by the Course Info curriculum path so AND/OR groups,
    # minimum grades, and programme/curriculum scoping cannot drift between
    # the planner and the student-facing prerequisite check.
    if any(key in rule for key in ("prerequisite_groups", "groups")):
        try:
            from app.campus.prerequisites import _rows as prerequisite_rows

            evaluation = evaluate_prerequisites(
                prerequisite_rows(rule),
                [{"course_code": code, "grade": grade} for code, grade in completed.items()],
                program_code=program_code,
                curriculum_version=curriculum_version,
            )
        except ValueError:
            return None, "prerequisite data could not be verified"
        if evaluation.eligible is None:
            return None, evaluation.reason or "prerequisite data could not be verified"
        if evaluation.eligible:
            return True, None
        missing = ", ".join(evaluation.missing)
        return False, evaluation.reason or (f"requires {missing}" if missing else "prerequisite not satisfied")
    # A rule can be scoped to a programme or curriculum revision. Scope is a
    # decision input, so missing student context is unknown rather than a free
    # pass; a rule for another programme does not apply.
    for names, student_value in (
        (("program_code", "program", "programCode"), program_code),
        (("curriculum_version", "curriculumVersion", "curriculum"), curriculum_version),
    ):
        scoped = next((rule.get(name) for name in names if rule.get(name) not in (None, "")), None)
        if scoped in (None, ""):
            continue
        if student_value is None:
            return None, "program or curriculum version is required to verify prerequisites"
        if str(scoped).strip().casefold() != str(student_value).strip().casefold():
            return True, None

    if "course" in rule or "course_code" in rule or "prerequisite_course_code" in rule:
        code = _code(str(rule.get("course") or rule.get("course_code") or rule.get("prerequisite_course_code")))
        grade = completed.get(code)
        required = str(
            rule.get("min_grade")
            or rule.get("minGrade")
            or rule.get("minimum_grade")
            or rule.get("minimumGrade")
            or rule.get("required_grade")
            or "DD"
        ).strip().upper()
        # Unknown minimum wording is a verification failure, while no
        # transcript row is a deterministic unmet prerequisite.
        if required not in GRADE_POINTS and required not in {"S", "EX", "P"}:
            return None, "invalid or unsupported prerequisite rule"
        return (
            grade is not None and _grade_at_least(grade, required),
            f"requires {code} with {required} or better",
        )
    if "min_cgpa" in rule:
        try:
            required = float(rule["min_cgpa"])
        except (TypeError, ValueError):
            return False, "invalid prerequisite rule"
        if cgpa is None:
            return None, f"requires CGPA {required:.2f}"
        return (cgpa >= required, f"requires CGPA {required:.2f}")
    if "all" in rule:
        if not isinstance(rule["all"], list):
            return False, "invalid prerequisite rule"
        failures = []
        for child in rule["all"]:
            met, reason = _prerequisite_met(
                child,
                completed,
                cgpa,
                program_code=program_code,
                curriculum_version=curriculum_version,
            )
            if met is None:
                return None, reason or "prerequisite data could not be verified"
            if not met:
                failures.append(reason or "unmet prerequisite")
        return (not failures, "; ".join(failures) if failures else None)
    if "any" in rule:
        if not isinstance(rule["any"], list):
            return False, "invalid prerequisite rule"
        reasons = []
        for child in rule["any"]:
            met, reason = _prerequisite_met(
                child,
                completed,
                cgpa,
                program_code=program_code,
                curriculum_version=curriculum_version,
            )
            if met is None:
                return None, reason or "prerequisite data could not be verified"
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


def _meetings(offering: Any) -> list[tuple[str, int, int]]:
    result = []
    for meeting in offering.schedule or []:
        if not isinstance(meeting, dict):
            continue
        # Canonical catalog sections carry minute precise values. Keep the
        # legacy ``HH:MM`` parser for pre-publication CourseOffering rows.
        start_raw = meeting.get("start_minute", meeting.get("start"))
        end_raw = meeting.get("end_minute", meeting.get("end"))
        if end_raw is None and meeting.get("duration_minutes") is not None:
            try:
                end_raw = int(start_raw) + int(meeting["duration_minutes"])
            except (TypeError, ValueError):
                end_raw = None
        if isinstance(start_raw, (int, float)) and isinstance(end_raw, (int, float)):
            start, end = int(start_raw), int(end_raw)
        else:
            start = _minutes(str(start_raw or ""), 0)
            end = _minutes(str(end_raw or ""), 24 * 60)
        if not (0 <= start < end <= 24 * 60):
            continue
        raw_day = meeting.get("day") if meeting.get("day") is not None else meeting.get("weekday")
        if isinstance(raw_day, int) and not isinstance(raw_day, bool):
            day = ("monday", "tuesday", "wednesday", "thursday", "friday")[raw_day] if 0 <= raw_day <= 4 else ""
        else:
            day = str(raw_day or "").lower()
        if not day:
            continue
        result.append((day, start, end))
    return result


def _fits_user_constraints(offering: Any, request: SemesterPlanRequest) -> tuple[bool, str | None]:
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


def _published_mode() -> bool:
    """Read the catalog rollout switch lazily for this planning process."""

    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "academic_catalog_reads_enabled", False))
    except Exception:
        return False


def _capture_catalog_release(db: AsyncSession, metadata: Any) -> str | None:
    """Pin the first published release on this request's DB session."""

    if isinstance(metadata, dict):
        release = (
            metadata.get("catalog_release_id")
            or metadata.get("release_id")
            or metadata.get("revision_id")
            or metadata.get("id")
        )
    else:
        release = (
            getattr(metadata, "catalog_release_id", None)
            or getattr(metadata, "release_id", None)
            or getattr(metadata, "revision_id", None)
            or getattr(metadata, "id", None)
        )
    if release is None:
        return None
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        pinned = info.get("academic_catalog_release_id")
        if pinned is not None and str(pinned) != str(release):
            raise HTTPException(409, "Published catalog release changed during this request.")
        info.setdefault("academic_catalog_release_id", str(release))
    return str(release)


def _plain_catalog_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if isinstance(value, dict):
        return dict(value)
    return value


def _number_value(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        import re

        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else default


def _instructor_text(value: Any) -> str:
    """Render the catalog's typed instructor list for legacy plan payloads."""

    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("source_name") or item.get("name") or item.get("instructor")
            text = str(item or "").strip()
            if text and text not in names:
                names.append(text)
        return ", ".join(names)
    return str(value or "").strip()


def _canonical_offerings(value: Any) -> list[Any]:
    """Normalize the exact ``published_plan_inputs`` offering shape for solver."""

    if hasattr(value, "offerings"):
        value = value.offerings
    if isinstance(value, dict):
        value = value.get("offerings", value.get("courses", value.get("items", [])))
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for raw in value:
        row = _plain_catalog_value(raw)
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("data"), dict):
            data = dict(row["data"])
            data.update({name: item for name, item in row.items() if name != "data"})
            row = data
        sections = row.get("sections")
        if isinstance(sections, list) and sections:
            for section in sections:
                if not isinstance(section, dict):
                    continue
                result.append(
                    SimpleNamespace(
                        course_code=str(row.get("course_code") or row.get("code") or "").strip(),
                        section=str(
                            section.get("section")
                            or section.get("section_number")
                            or section.get("section_code")
                            or row.get("section")
                            or row.get("section_code")
                            or "1"
                        ),
                        title=str(section.get("title") or row.get("title") or row.get("name") or ""),
                        credits=_number_value(
                            section.get(
                                "credits",
                                section.get(
                                    "credit",
                                    row.get(
                                        "credits",
                                        row.get("credit", row.get("local_credits")),
                                    ),
                                ),
                            )
                        ),
                        schedule=section.get("schedule", section.get("meetings", [])) or [],
                        campus=section.get("campus", row.get("campus")),
                        department=section.get("department", row.get("department")),
                        instructor=_instructor_text(
                            section.get("instructor")
                            or section.get("instructors")
                            or row.get("instructor")
                            or row.get("instructors")
                        ),
                        source_url=section.get("source_url", row.get("source_url")),
                        fetched_at=section.get("fetched_at", row.get("fetched_at")),
                        eligible=section.get("eligible", row.get("eligible")),
                        eligibility_status=section.get("eligibility_status", row.get("eligibility_status")),
                        data_status=section.get("data_status", row.get("data_status", "unknown")),
                        # The published batch contract represents freshness in
                        # the section's explicit data_status. Preserve an
                        # explicit boolean when present, and derive it only
                        # from the verified ``fresh`` value; missing metadata
                        # remains false/unknown rather than becoming trusted.
                        fresh=section.get(
                            "fresh",
                            row.get(
                                "fresh",
                                str(row.get("data_status") or "").casefold() == "fresh",
                            ),
                        ),
                        meetings_status=section.get("meetings_status", row.get("meetings_status", "unknown")),
                        complete=section.get("complete", row.get("complete", False)),
                        catalog_release_id=section.get("catalog_release_id", row.get("catalog_release_id")),
                        aliases=section.get("aliases", row.get("aliases", [])) or [],
                    )
                )
            continue
        result.append(
            SimpleNamespace(
                course_code=str(row.get("course_code") or row.get("code") or "").strip(),
                section=str(
                    row.get("section") or row.get("section_number") or row.get("section_code") or "1"
                ),
                title=str(row.get("title") or row.get("name") or ""),
                credits=_number_value(row.get("credits", row.get("credit", row.get("local_credits")))),
                schedule=row.get("schedule", row.get("meetings", [])) or [],
                campus=row.get("campus"),
                department=row.get("department"),
                instructor=_instructor_text(row.get("instructor") or row.get("instructors")),
                source_url=row.get("source_url"),
                fetched_at=row.get("fetched_at"),
                eligible=row.get("eligible"),
                eligibility_status=row.get("eligibility_status"),
                data_status=row.get("data_status", "unknown"),
                fresh=row.get(
                    "fresh",
                    str(row.get("data_status") or "").casefold() == "fresh",
                ),
                meetings_status=row.get("meetings_status", "unknown"),
                complete=row.get("complete", False),
                catalog_release_id=row.get("catalog_release_id"),
                aliases=row.get("aliases", []) or [],
            )
        )
    return [row for row in result if row.course_code]


def _canonical_rules(value: Any) -> dict[str, Any]:
    if hasattr(value, "rules"):
        value = value.rules
    if isinstance(value, dict):
        rows = value.get("rules", value.get("items"))
        if rows is None:
            # Already keyed by canonical course code.
            result: dict[str, Any] = {}
            for key, raw in value.items():
                row = _plain_catalog_value(raw)
                if isinstance(row, dict) and isinstance(row.get("data"), dict):
                    # The reader may keep course identity and component
                    # metadata beside the typed rule payload. Flatten that
                    # envelope while retaining status/freshness fields.
                    data = dict(row["data"])
                    data.update({name: item for name, item in row.items() if name != "data"})
                    row = data
                if isinstance(row, dict):
                    row.setdefault("course_code", key)
                result[str(key).replace(" ", "").upper()] = row
            return result
        value = rows
    if not isinstance(value, list):
        return {}
    result: dict[str, Any] = {}
    for raw in value:
        row = _plain_catalog_value(raw)
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("data"), dict):
            data = dict(row["data"])
            data.update({name: item for name, item in row.items() if name != "data"})
            row = data
        code = "".join(str(row.get("course_code") or row.get("code") or "").upper().split())
        if code:
            result[code] = row
    return result


async def _published_plan_inputs(
    db: AsyncSession,
    user_id: UUID,
    term: str,
) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    """Read all plan inputs from one immutable published catalog release.

    ``published_plan_inputs`` is the catalog service's batch boundary. It
    returns offerings and course rules together, avoiding one database query
    per course and ensuring the planner never combines revisions from different
    releases. A missing helper is an unavailable deployment, not permission to
    fall back to legacy ``CourseOffering``/``CourseRule`` rows.
    """

    try:
        from app.academic_catalog.service import published_plan_inputs
    except (ImportError, ModuleNotFoundError) as exc:
        raise HTTPException(503, "The published academic catalog is not available yet.") from exc
    try:
        payload = await published_plan_inputs(db, user_id, term)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("published_plan_inputs_failed", user_id=str(user_id), term=term, error=str(exc))
        raise HTTPException(503, "The published academic catalog could not be read.") from exc

    if isinstance(payload, tuple):
        offerings = payload[0] if len(payload) > 0 else []
        rules = payload[1] if len(payload) > 1 else {}
        metadata = payload[2] if len(payload) > 2 else {}
    elif isinstance(payload, dict):
        offerings = payload.get("offerings", payload.get("courses", []))
        rules = payload.get("rules", {})
        metadata = payload.get("metadata", payload)
    else:
        offerings = getattr(payload, "offerings", [])
        rules = getattr(payload, "rules", {})
        metadata = getattr(payload, "metadata", payload)
    release_id = _capture_catalog_release(db, metadata)
    if release_id and isinstance(metadata, dict):
        metadata = {**metadata, "catalog_release_id": release_id}
    return _canonical_offerings(offerings), _canonical_rules(rules), metadata if isinstance(metadata, dict) else {}


def _fresh_value(value: Any, *, default: bool = False) -> bool:
    """Interpret catalog freshness flags without treating unknown as fresh."""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, dict):
        if "fresh" in value:
            return bool(value["fresh"])
        if "status" in value:
            return str(value["status"]).casefold() in {"fresh", "ok", "current"}
    return str(value).casefold() in {"fresh", "ok", "current"}


def _offering_meeting_status(offering: Any) -> str:
    value = getattr(offering, "meetings_status", None)
    if value is None:
        value = getattr(offering, "meeting_status", None)
    return str(value or "unknown").casefold()


def _offering_decision_state(offering: Any) -> tuple[str, str | None]:
    """Return ``usable``/``untimed``/``unknown``/``blocked`` for a section."""

    if getattr(offering, "complete", False) is not True:
        return "unknown", "course section data is incomplete"
    data_status = str(getattr(offering, "data_status", "unknown") or "unknown").casefold()
    if data_status in {"unknown", "unavailable", "unverified", "stale", "failed"}:
        return "unknown", "course section data could not be verified"
    if not _fresh_value(getattr(offering, "fresh", None), default=False):
        return "unknown", "course section data is stale"
    status = _offering_meeting_status(offering)
    meetings = _meetings(offering)
    if meetings:
        if status in {"unknown", "unavailable", "unverified", "failed", "stale", "invalid", "unpublished"}:
            return "unknown", "meeting data could not be verified"
        return "usable", None
    # A catalog may explicitly verify that a course is untimed, such as a
    # thesis. It can count toward credits but cannot claim to satisfy a
    # timetable slot.
    if status in {"untimed", "explicitly_untimed"} or getattr(offering, "untimed", False) is True:
        return "untimed", "course has no scheduled meeting times"
    return "unknown", "meeting data is unavailable"


async def plan_semester(db: AsyncSession, user_id: UUID, request: SemesterPlanRequest) -> dict:
    snapshot, snapshot_status = await prepare_planning_snapshot(db, user_id, request.term)
    if snapshot is None:
        return {
            "status": "needs_academic_snapshot",
            "detail": "Refresh the student's SAIS transcript and current registration before planning.",
            "term": request.term,
            "freshness": {"academic_snapshot": snapshot_status},
        }

    # A published catalog plan must be based on one immutable release.  The
    # legacy rows remain available only while the rollout flag is off; keeping
    # this branch explicit prevents an accidental stale DB fallback from
    # bypassing admin publication.
    catalog_metadata: dict[str, Any] = {}
    if _published_mode():
        if not snapshot_status.get("fresh", False):
            return {
                "status": "needs_verification",
                "detail": "The academic snapshot is stale or could not be refreshed; verify it before planning.",
                "term": request.term,
                "freshness": {"academic_snapshot": snapshot_status},
                "courses": [],
                "course_statuses": [],
                "missing_required_courses": sorted({_code(item) for item in request.required_courses}),
            }
        try:
            offerings, rules, catalog_metadata = await _published_plan_inputs(db, user_id, request.term)
        except HTTPException as exc:
            return {
                "status": "needs_verification",
                "detail": str(exc.detail),
                "term": request.term,
                "freshness": {"academic_snapshot": snapshot_status, "catalog": "unavailable"},
                "courses": [],
                "course_statuses": [],
                "missing_required_courses": sorted({_code(item) for item in request.required_courses}),
            }
        if not (
            catalog_metadata.get("catalog_release_id")
            or catalog_metadata.get("release_id")
            or catalog_metadata.get("id")
        ):
            return {
                "status": "needs_verification",
                "detail": "The published catalog release could not be pinned for this plan.",
                "term": request.term,
                "freshness": {"academic_snapshot": snapshot_status, "catalog": "unavailable"},
                "courses": [],
                "course_statuses": [],
                "missing_required_courses": sorted({_code(item) for item in request.required_courses}),
            }
    else:
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
    max_credits = min(
        float(request.max_credits if request.max_credits is not None else policy_rules.get("max_credits", 20)),
        60,
    )
    min_credits = float(
        request.min_credits if request.min_credits is not None else policy_rules.get("min_credits", 0)
    )
    current_credits = float(snapshot.current_credits)
    current_points = float(snapshot.current_grade_points)
    # A zero-credit snapshot does not prove a 0.00 CGPA. Published planning
    # treats that missing input as unknown; the legacy path keeps its historic
    # numeric fallback while the rollout flag is disabled.
    cgpa = current_points / current_credits if current_credits else (None if _published_mode() else 0.0)
    completed: dict[str, str] = {}
    for item in snapshot.completed_courses:
        if not isinstance(item, dict) or not item.get("course_code") or not item.get("grade"):
            continue
        code = _code(str(item.get("course_code")))
        grade = str(item.get("grade"))
        previous = completed.get(code)
        grade_token = grade.upper().split()[0]
        previous_token = previous.upper().split()[0] if previous else ""
        if (
            previous is None
            or (grade_token in PASSING_GRADE_TOKENS and previous_token not in PASSING_GRADE_TOKENS)
            or GRADE_POINTS.get(grade_token, -1) > GRADE_POINTS.get(previous_token, -1)
        ):
            completed[code] = grade

    context = await db.get(StudentContext, user_id)
    program_code = getattr(context, "program_code", None) if context else None
    curriculum_version = getattr(context, "curriculum_version", None) if context else None
    required = {_code(item) for item in request.required_courses}
    preferred = {_code(item) for item in request.preferred_courses}
    excluded = {_code(item) for item in request.excluded_courses}
    retakes = {_code(item) for item in request.retake_courses}
    eligible: dict[str, list[Any]] = defaultdict(list)
    exclusions: list[dict] = []
    reasons_by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    unknown_codes: set[str] = set()
    for offering in offerings:
        code = _code(offering.course_code)
        reason = None
        decision_state = "usable"
        if code in excluded:
            reason = "excluded by the student"
        elif code in completed and code not in retakes:
            reason = "already completed"
        else:
            rule = rules.get(code)
            if rule is None:
                reason = "prerequisite data unavailable"
                met, prerequisite_reason = (None, reason) if _published_mode() else (False, reason)
            else:
                if isinstance(rule, dict):
                    # The published batch rule carries metadata beside its
                    # actual prerequisite expression. Use typed groups when
                    # present so their AND/OR and scope survive; otherwise
                    # evaluate the compact ``prerequisites`` expression
                    # rather than treating the metadata envelope itself as a
                    # malformed rule.
                    rule_value = (
                        rule
                        if "prerequisite_groups" in rule or "groups" in rule
                        else rule.get("prerequisites", rule)
                    )
                else:
                    rule_value = getattr(rule, "prerequisites", rule)
                rule_status = str(
                    getattr(rule, "data_status", None)
                    or (rule.get("data_status") if isinstance(rule, dict) else None)
                    or "unknown"
                ).casefold()
                rule_fresh = getattr(rule, "fresh", None)
                if isinstance(rule, dict) and rule_fresh is None:
                    rule_fresh = rule.get("fresh", rule.get("freshness"))
                if _published_mode() and (rule_status in {"unknown", "unavailable", "unverified", "stale"} or not _fresh_value(rule_fresh, default=False)):
                    met, prerequisite_reason = None, "prerequisite data could not be verified"
                else:
                    met, prerequisite_reason = _prerequisite_met(
                        rule_value,
                        completed,
                        cgpa,
                        program_code=program_code,
                        curriculum_version=curriculum_version,
                    )
            if met is None:
                decision_state = "unknown"
                unknown_codes.add(code)
                reason = prerequisite_reason or "prerequisite data could not be verified"
            elif not met:
                reason = prerequisite_reason
            else:
                if _published_mode():
                    if getattr(offering, "eligible", None) is False:
                        reason = "section is not eligible for this student"
                    elif getattr(offering, "eligible", None) is not True:
                        decision_state = "unknown"
                        unknown_codes.add(code)
                        reason = "section eligibility could not be verified"
                    else:
                        decision_state, decision_reason = _offering_decision_state(offering)
                        if decision_state == "unknown":
                            unknown_codes.add(code)
                            reason = decision_reason
                        elif decision_state == "untimed":
                            # An explicitly untimed course is still a valid
                            # credit-bearing candidate when its catalog
                            # eligibility and academic rule are verified. It
                            # enters the credit solver without fabricated
                            # meeting times; proposal/timetable adapters keep
                            # it visible as untimed and do not turn it into a
                            # scheduled entry.
                            reason = None
                if reason is None:
                    fits, constraint_reason = _fits_user_constraints(offering, request)
                    if not fits:
                        reason = constraint_reason
        if reason:
            entry = {"course_code": code, "section": offering.section, "reason": reason}
            exclusions.append(entry)
            reasons_by_code[code].append({"reason": reason, "status": decision_state})
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
    requested = required | preferred
    selected_by_code = {_code(item.course_code) for item in selected}
    course_statuses: list[dict[str, Any]] = []
    for code in sorted(requested):
        if code in selected_by_code:
            status_value = "included"
            reason = None
        elif code not in eligible:
            status_value = "needs_verification" if code in unknown_codes else "blocked"
            reason = (
                "course data needs verification"
                if code in unknown_codes
                else (reasons_by_code.get(code) or [{"reason": "course is not available"}])[0]["reason"]
            )
        else:
            status_value = "unschedulable"
            reason = "eligible sections could not satisfy timetable constraints"
        course_statuses.append(
            {"course_code": code, "status": status_value, **({"reason": reason} if reason else {})}
        )

    status = "ok"
    if unknown_codes:
        status = "needs_verification"
    elif missing_required or selected_credits < min_credits:
        status = "constraints_unsatisfied"
    provenance = {
        "catalog_release_id": (
            catalog_metadata.get("catalog_release_id")
            or catalog_metadata.get("release_id")
            or catalog_metadata.get("id")
        ),
        "catalog_course_revision_ids": catalog_metadata.get("course_revision_ids", {}),
        "academic_snapshot_fetched_at": snapshot.fetched_at.isoformat(),
        "academic_snapshot_status": snapshot_status,
    }
    offering_timestamps = [
        item.fetched_at
        for item in offerings
        if isinstance(getattr(item, "fetched_at", None), datetime)
    ]
    return {
        "status": status,
        "term": request.term,
        "maximum_semester_gpa": 4.0 if selected else None,
        "projected_cumulative_gpa": round(projected, 3),
        "current_cumulative_gpa": round(cgpa, 3) if cgpa is not None else None,
        "selected_credits": round(selected_credits, 2),
        "courses": [
            {
                "course_code": _code(item.course_code),
                "section": item.section,
                "title": item.title,
                "credits": float(item.credits),
                "schedule": item.schedule,
                "source_url": item.source_url,
                "timing_status": (
                    "untimed"
                    if _published_mode() and _offering_decision_state(item)[0] == "untimed"
                    else "verified"
                ),
            }
            for item in selected
        ],
        "missing_required_courses": missing_required,
        "course_statuses": course_statuses,
        "excluded_options": exclusions,
        "incomplete": status != "ok",
        "provenance": provenance,
        "assumptions": [
            "Every selected course receives AA (4.00).",
            "Offerings, prerequisites, and the academic snapshot are treated as fresh only at their timestamps.",
            "Only one section of each course is selected and overlapping meetings are rejected.",
        ],
        "freshness": {
            "academic_snapshot": snapshot.fetched_at.isoformat(),
            "academic_snapshot_status": snapshot_status,
            # Published revisions carry component freshness rather than a
            # legacy per-offering fetched_at timestamp. Do not call
            # ``isoformat`` on ``None`` for that contract.
            "offerings": max(offering_timestamps).isoformat() if offering_timestamps else None,
            "catalog": catalog_metadata,
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
