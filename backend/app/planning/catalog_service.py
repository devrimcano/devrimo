"""Application services shared by the HTTP and workspace catalog adapters.

Course expansion, typed section normalization, student profile reads and
restriction verdicts all describe the same catalog operation. Keeping them at
this layer prevents an adapter from silently changing a list-shaped campus
response into an empty, eligible table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus import departments
from app.campus.course_info import CatalogSession, department_for_course
from app.campus.eligibility import course_candidates, evaluate, prior_grade
from app.db.models import StudentAcademicSnapshot, StudentContext

_ALPHA_PREFIX = re.compile(r"^[A-Z]{2,6}")
# These are the spellings the deterministic eligibility evaluator actually
# understands. Accepting a different label here would silently turn a
# restriction row into an empty, unrestricted table.
_DEPARTMENT_FIELDS = (
    "given_dept",
    "givenDept",
    "given_department",
    "dept",
    # Published catalog rows use a typed name for the same source field. Keep
    # the importer-facing aliases above because old observations still carry
    # them, but do not make a verified table depend on one spelling.
    "department",
    "department_code",
    "program",
)
_UNAVAILABLE_RESTRICTIONS = "Section restrictions could not be verified."


@dataclass(frozen=True, slots=True)
class StudentProfile:
    """All student fields needed to evaluate one or many sections."""

    context: StudentContext | None
    department: departments.Department | None
    cgpa: float | None
    completed: list[dict[str, Any]]


async def load_student_profile(
    db: AsyncSession,
    user_id: UUID,
    *,
    context: StudentContext | None = None,
    snapshot: StudentAcademicSnapshot | None = None,
    department_value: str | None = None,
) -> StudentProfile:
    """Read the profile once for all section checks in a request."""

    if context is None:
        context = await db.get(StudentContext, user_id)
    if snapshot is None:
        snapshot = await db.scalar(
            select(StudentAcademicSnapshot)
            .where(StudentAcademicSnapshot.user_id == user_id)
            .order_by(StudentAcademicSnapshot.fetched_at.desc())
            .limit(1)
        )
    cgpa = None
    if snapshot is not None and snapshot.current_credits:
        cgpa = float(snapshot.current_grade_points) / float(snapshot.current_credits)
    value = department_value or ((context.department or context.program_code) if context else None)
    return StudentProfile(
        context=context,
        department=departments.resolve(value),
        cgpa=cgpa,
        completed=list(snapshot.completed_courses) if snapshot else [],
    )


def published_catalog_reads_enabled() -> bool:
    """Whether shared course facts must come from the reviewed catalog.

    This tiny helper is intentionally local to the consumer adapter so callers
    can keep the rollout gate lazy and tests can patch it without importing the
    settings singleton during module collection.
    """

    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "academic_catalog_reads_enabled", False))
    except Exception:
        return False


async def read_shared_course_info(
    db: AsyncSession,
    user_id: UUID,
    tool_suffix: str,
    values: dict[str, str],
    *,
    session: CatalogSession | None = None,
) -> Any:
    """Read a shared catalog tool through the single Course Info boundary.

    ``call_course_info`` dispatches to ``academic_catalog.service.read_tool``
    once the feature flag is enabled and retains the existing cache-backed
    Course Info behavior during rollout. Keeping this adapter in the planning
    layer gives planner/workspace code one seam to replace in tests and keeps
    personal ``get_student_*`` reads out of the published path.
    """

    from app.campus.course_info import call_course_info

    return await call_course_info(db, user_id, tool_suffix, values, session=session)


async def expand_course_code(
    db: AsyncSession,
    user_id: UUID,
    course_code: str,
    home_department: str = "",
    *,
    session: CatalogSession | None = None,
) -> tuple[str, departments.Department]:
    """Expand a typed course code and return its owning department object."""

    compact = course_code.upper().replace(" ", "").replace("-", "")
    direct = departments.expand_course_code(compact)
    if direct is not None:
        return direct
    resolved_home = departments.resolve(home_department)
    owner_code = await department_for_course(
        db,
        user_id,
        compact,
        resolved_home.code if resolved_home else home_department,
        session=session,
    )
    if not owner_code:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Could not identify which department owns {compact}. Use the course's full seven-digit METU code.",
        )
    # The live catalog can contain a joint or satellite programme absent from
    # the generated Ankara directory. Keep its verified three-digit owner for
    # the upstream lookup instead of silently substituting the student's home
    # department; the display metadata is simply empty in that case.
    owner = departments.by_code(owner_code) or departments.Department(owner_code, "", "", "")
    if not compact.isdigit() and (number := re.search(r"(\d{3,4})$", compact)):
        compact = f"{owner.code}{number.group(1).zfill(4)}"
    elif compact.isdigit() and len(compact) in (3, 4):
        compact = f"{owner.code}{compact.zfill(4)}"
    return compact, owner


def course_grade(
    profile: StudentProfile,
    supplied_course: str,
    owner: departments.Department,
    full_course: str,
) -> str | None:
    """Look up the student's existing grade using one shared course spelling set."""

    return prior_grade(
        profile.completed,
        course_candidates(supplied_course, owner, full_course),
    )


def constraint_rows(payload: Any) -> list[dict[str, Any]] | None:
    """Extract validated restriction rows, preserving unavailable as ``None``.

    An empty list is a valid, unrestricted section. A missing, malformed or
    unlabelled table is unknown and must not be passed to ``evaluate`` as an
    empty unrestricted table.
    """

    marker = object()
    if isinstance(payload, dict):
        status_value = str(payload.get("status", "")).casefold()
        if status_value in {"unknown", "unavailable", "unverified", "stale", "failed"}:
            return None
        # ``constraints`` is the upstream spelling. Published revisions may
        # expose the semantically identical field as ``restrictions`` or
        # ``eligibility`` while retaining the same row shape.
        raw = marker
        for field in ("constraints", "restrictions", "eligibility"):
            if field in payload:
                raw = payload[field]
                break
        if raw is marker and str(payload.get("status", "")).casefold() in {
            "unknown", "unavailable", "unverified", "stale"
        }:
            return None
        if raw is marker:
            for wrapper in ("result", "data", "value", "items"):
                nested = payload.get(wrapper)
                if isinstance(nested, (dict, list)):
                    return constraint_rows(nested)
    elif isinstance(payload, list):
        raw = payload
    else:
        return None
    if raw is marker or not isinstance(raw, list):
        return None
    if not raw:
        return []
    if any(
        not isinstance(row, dict) or not any(field in row for field in _DEPARTMENT_FIELDS)
        for row in raw
    ):
        return None
    return [row for row in raw if isinstance(row, dict)]


def section_verdict(
    payload: Any,
    *,
    profile: StudentProfile,
    supplied_course: str,
    owner: departments.Department,
    full_course: str,
    held_grade: str | None = None,
) -> tuple[list[dict[str, Any]], bool | None, str]:
    """Return one normalized rows/verdict tuple for either adapter."""

    rows = constraint_rows(payload)
    if rows is None:
        return [], None, _UNAVAILABLE_RESTRICTIONS
    # Empty is an explicitly verified unrestricted table and needs no student
    # profile. For populated tables the evaluator distinguishes an actual
    # exclusion from an unavailable profile field. It also understands the
    # registrar's no-limit encodings (0.00-4.00 and 0-95), so merely carrying
    # those columns does not turn an otherwise verifiable row into unknown.
    held = held_grade if held_grade is not None else course_grade(profile, supplied_course, owner, full_course)
    verdict = evaluate(
        rows,
        department=profile.department.abbreviation if profile.department else None,
        surname=profile.context.surname_prefix if profile.context else None,
        cgpa=profile.cgpa,
        year=profile.context.year_of_study if profile.context else None,
        prior_grade=held,
    )
    return rows, verdict.eligible, verdict.reason


def unavailable_restrictions_reason() -> str:
    """Stable user-facing reason for an unverified restriction response."""

    return _UNAVAILABLE_RESTRICTIONS
