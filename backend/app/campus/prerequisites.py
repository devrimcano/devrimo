"""Isolated prerequisite gate for courses entering the schedule pool.

The service reads official prerequisite rows from the METU catalog and compares
them with the student's stored SAIS transcript. It does not know about HTTP or
frontend state; callers receive approved courses, structured rejections, and
read failures separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus import departments
from app.campus.course_info import CatalogSession, call_course_info
from app.campus.eligibility import course_candidates, has_passed, prior_grade


@dataclass(frozen=True, slots=True)
class PrerequisiteRejection:
    course_code: str
    prerequisite_course_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_code": self.course_code,
            "course_label": display_code(self.course_code),
            "prerequisite_course_codes": list(self.prerequisite_course_codes),
            "prerequisite_course_labels": [display_code(code) for code in self.prerequisite_course_codes],
        }


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("result", "prerequisites", "courses", "data", "items"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
        raise ValueError("METU prerequisite response could not be read")
    if isinstance(payload, list):
        if any(not isinstance(row, dict) for row in payload):
            raise ValueError("METU prerequisite rows could not be read")
        return payload
    raise ValueError("METU prerequisite response could not be read")


def _full_code(value: Any) -> str | None:
    text = "".join(str(value or "").upper().split())
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 7:
        return digits
    expanded = departments.expand_course_code(text)
    return expanded[0] if expanded else None


def display_code(value: str) -> str:
    owner = departments.by_code(value[:3])
    # A few central service-course departments do not publish an abbreviation
    # in the catalog directory even though students consistently see one.
    abbreviation = str(getattr(owner, "abbreviation", "") or "").strip() or {"877": "OHS"}.get(value[:3], "")
    # METU stores the course number as four digits after the three-digit
    # department: EE 213 is 5670213, while HIST 2201 is 2402201. Reading only
    # the final three digits silently turned HIST 2201 into HIST 201.
    number = value[3:].lstrip("0") or "0"
    return f"{abbreviation} {number}" if abbreviation else value


def unmet_prerequisites(rows: list[dict[str, Any]], completed: list[dict]) -> tuple[str, ...]:
    """Return the closest unsatisfied alternative set, or nothing when eligible."""
    if not rows:
        return ()
    sets: dict[str, list[str]] = {}
    for row in rows:
        code = _full_code(row.get("prerequisite_course_code") or row.get("course_code"))
        if code is None:
            raise ValueError("METU prerequisite course code could not be read")
        set_no = str(row.get("set_no") or "1").strip() or "1"
        if code not in sets.setdefault(set_no, []):
            sets[set_no].append(code)

    failures: list[tuple[str, ...]] = []
    for required in sets.values():
        missing = tuple(
            code
            for code in required
            if not has_passed(
                prior_grade(completed, course_candidates(code, departments.by_code(code[:3]), code))
            )
        )
        if not missing:
            return ()
        failures.append(missing)
    return min(failures, key=lambda missing: (len(missing), missing))


async def filter_courses(
    db: AsyncSession,
    user_id: UUID,
    catalog: CatalogSession,
    semester: str,
    courses: list[dict[str, Any]],
    completed: list[dict],
) -> tuple[list[dict[str, Any]], list[PrerequisiteRejection], list[str]]:
    """Pass every candidate through its catalog prerequisites and transcript."""
    approved: list[dict[str, Any]] = []
    rejected: list[PrerequisiteRejection] = []
    warnings: list[str] = []
    for course in courses:
        code = str(course.get("code") or "").strip()
        if len(code) != 7 or not code.isdigit():
            warnings.append(f"{code or 'Unknown course'}: prerequisite check could not identify the course.")
            continue
        try:
            payload = await call_course_info(
                db,
                user_id,
                "get_course_prerequisites",
                {"department": code[:3], "semester": semester, "course": code},
                session=catalog,
            )
            missing = unmet_prerequisites(_rows(payload), completed)
        except (HTTPException, ValueError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            warnings.append(f"{display_code(code)}: prerequisites could not be verified ({detail}).")
            continue
        if missing:
            rejected.append(PrerequisiteRejection(code, missing))
        else:
            approved.append(course)
    return approved, rejected, warnings
