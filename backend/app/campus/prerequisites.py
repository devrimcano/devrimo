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
from app.campus.eligibility import course_candidates, prior_grade


# Letter grades are ordered by METU's four-point scale.  ``S``/``EX``/``P``
# are passing transcript marks but do not have a meaningful letter-grade
# ordering; they satisfy a plain prerequisite and an explicit minimum only
# when the rule accepts a pass token.  Unknown marks remain unknown.
_GRADE_POINTS = {
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
_PASS_TOKENS = frozenset({"S", "EX", "P"})
_PASS_GRADE_TOKENS = frozenset({"AA", "BA", "BB", "CB", "CC", "DC", "DD"})


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


@dataclass(frozen=True, slots=True)
class PrerequisiteEvaluation:
    """Deterministic prerequisite result with an explicit unknown state."""

    eligible: bool | None
    missing: tuple[str, ...] = ()
    reason: str = ""


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").casefold()
        if status in {"unknown", "unavailable", "unverified", "stale"}:
            raise ValueError("METU prerequisite response could not be verified")
        for key in ("result", "prerequisites", "prerequisite_groups", "courses", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return _flatten_groups(value)
            if isinstance(value, dict):
                # Published readers may keep the response envelope in
                # ``result``. Recurse so metadata does not become an academic
                # rule and a verified empty list stays distinguishable.
                try:
                    return _rows(value)
                except ValueError:
                    raise
        if "groups" in payload or "requirements" in payload:
            return _flatten_groups([payload])
        raise ValueError("METU prerequisite response could not be read")
    if isinstance(payload, list):
        return _flatten_groups(payload)
    raise ValueError("METU prerequisite response could not be read")


def _flatten_groups(values: list[Any]) -> list[dict[str, Any]]:
    """Flatten typed AND/OR groups to the legacy ``set_no`` row contract.

    A set is an AND group (all its requirements are needed); separate sets are
    alternatives (OR). Unsupported boolean operators remain an explicit parse
    error, because guessing their meaning can admit a student incorrectly.
    """

    rows: list[dict[str, Any]] = []
    next_set = 1

    def explicit_set(item: Any) -> str | None:
        if not isinstance(item, dict):
            return None
        value = item.get("set_no") or item.get("setNo") or item.get("group_no") or item.get("groupNo")
        return str(value).strip() or None if value not in (None, "") else None

    def visit(item: Any, set_no: str) -> None:
        nonlocal next_set
        if isinstance(item, list):
            for child in item:
                visit(child, set_no)
            return
        if not isinstance(item, dict):
            raise ValueError("METU prerequisite rows could not be read")
        requirements = item.get("requirements")
        if requirements is None:
            requirements = item.get("items")
        groups = item.get("groups")
        if groups is not None:
            if not isinstance(groups, list):
                raise ValueError("METU prerequisite groups could not be read")
            for group in groups:
                scoped_group = dict(group) if isinstance(group, dict) else group
                if isinstance(scoped_group, dict):
                    for key in ("program_code", "program", "programCode", "curriculum_version", "curriculum", "curriculumVersion"):
                        if key not in scoped_group and key in item:
                            scoped_group[key] = item[key]
                visit(scoped_group, explicit_set(scoped_group) or str(next_set))
                next_set += 1
            return
        if requirements is not None:
            if not isinstance(requirements, list):
                raise ValueError("METU prerequisite requirements could not be read")
            operator = str(item.get("operator") or item.get("logic") or "AND").upper()
            if operator not in {"AND", "OR"}:
                raise ValueError(f"Unsupported prerequisite operator: {operator}")
            group_set = explicit_set(item) or set_no
            if operator == "AND":
                for child in requirements:
                    scoped_child = dict(child) if isinstance(child, dict) else child
                    if isinstance(scoped_child, dict):
                        for key in (
                            "program_code", "program", "programCode",
                            "curriculum_version", "curriculum", "curriculumVersion",
                        ):
                            if key not in scoped_child and key in item:
                                scoped_child[key] = item[key]
                    visit(scoped_child, group_set)
            else:
                for position, child in enumerate(requirements):
                    scoped_child = dict(child) if isinstance(child, dict) else child
                    if isinstance(scoped_child, dict):
                        for key in (
                            "program_code", "program", "programCode",
                            "curriculum_version", "curriculum", "curriculumVersion",
                        ):
                            if key not in scoped_child and key in item:
                                scoped_child[key] = item[key]
                    # Each OR branch is an alternative set. Prefix the group
                    # number so alternatives from distinct groups cannot
                    # accidentally merge into one AND set.
                    visit(scoped_child, f"{group_set}:{position}")
            return
        operator = str(item.get("operator") or item.get("logic") or "").upper()
        if operator and operator not in {"AND", "OR"}:
            raise ValueError(f"Unsupported prerequisite operator: {operator}")
        # A row needs a course code. Ignore wrappers carrying only source
        # metadata; rows without a code are malformed academic rules.
        if not (item.get("prerequisite_course_code") or item.get("course_code") or item.get("course")):
            if item:
                raise ValueError("METU prerequisite course code could not be read")
            return
        row = dict(item)
        row.setdefault("set_no", set_no)
        if row.get("course") and not row.get("prerequisite_course_code"):
            row["prerequisite_course_code"] = row["course"]
        rows.append(row)

    for value in values:
        visit(value, explicit_set(value) or "1")
    return rows


def _full_code(value: Any) -> str | None:
    text = "".join(str(value or "").upper().split())
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 7:
        return digits
    expanded = departments.expand_course_code(text)
    return expanded[0] if expanded else None


def _grade_at_least(actual: Any, required: Any) -> bool | None:
    """Compare a transcript grade with the rule's explicit minimum.

    The old implementation used :func:`has_passed` for every row, so a DD
    transcript satisfied a BB prerequisite.  Keep unknown and non-letter
    tokens explicit: a malformed academic rule must become a verification
    warning rather than silently admitting a student.
    """

    actual_token = str(actual or "").strip().upper().split()[0] if actual else ""
    required_token = str(required or "DD").strip().upper().split()[0] if required else "DD"
    if not actual_token:
        return False
    if required_token in _PASS_TOKENS:
        return actual_token in _PASS_TOKENS or actual_token in _PASS_GRADE_TOKENS
    actual_points = _GRADE_POINTS.get(actual_token)
    required_points = _GRADE_POINTS.get(required_token)
    if actual_points is None or required_points is None:
        # A plain pass/fail prerequisite still uses ``has_passed`` below; an
        # unknown explicit minimum cannot be evaluated safely.
        return None
    return actual_points >= required_points


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


def evaluate_prerequisites(
    rows: list[dict[str, Any]],
    completed: list[dict],
    *,
    program_code: str | None = None,
    curriculum_version: str | None = None,
) -> PrerequisiteEvaluation:
    """Evaluate scoped prerequisite groups without collapsing unknown to false."""

    if not rows:
        return PrerequisiteEvaluation(True)
    sets: dict[str, list[str]] = {}
    minimums: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        code = _full_code(
            row.get("prerequisite_course_code") or row.get("course_code") or row.get("course")
        )
        if code is None:
            return PrerequisiteEvaluation(None, reason="prerequisite course code could not be read")

        applies = True
        for names, student_value in (
            (("program_code", "program", "programCode"), program_code),
            (("curriculum_version", "curriculumVersion", "curriculum"), curriculum_version),
        ):
            rule_value = next((row.get(name) for name in names if row.get(name) not in (None, "")), None)
            if rule_value in (None, ""):
                continue
            if student_value is None:
                return PrerequisiteEvaluation(
                    None,
                    reason="program or curriculum version is required to verify prerequisites",
                )
            if str(rule_value).strip().casefold() != str(student_value).strip().casefold():
                # A rule for another curriculum does not apply to this student.
                applies = False
                break
        # A row scoped to another programme/curriculum does not apply.
        if not applies:
            continue
        set_no = str(row.get("set_no") or row.get("setNo") or "1").strip() or "1"
        if code not in sets.setdefault(set_no, []):
            sets[set_no].append(code)
        minimum = (
            row.get("min_grade")
            or row.get("minGrade")
            or row.get("minimum_grade")
            or row.get("minimumGrade")
            or row.get("required_grade")
            or "DD"
        )
        minimums.setdefault((set_no, code), []).append(str(minimum))

    if not sets:
        return PrerequisiteEvaluation(True)
    failures: list[tuple[str, ...]] = []
    for set_no, required in sets.items():
        missing: list[str] = []
        for code in required:
            grade = prior_grade(completed, course_candidates(code, departments.by_code(code[:3]), code))
            results = [
                _grade_at_least(grade, minimum)
                for minimum in minimums.get((set_no, code), ["DD"])
            ]
            if any(result is True for result in results):
                continue
            if any(result is None for result in results):
                return PrerequisiteEvaluation(
                    None,
                    reason=f"minimum grade for {display_code(code)} could not be verified",
                )
            missing.append(code)
        if not missing:
            return PrerequisiteEvaluation(True)
        failures.append(tuple(missing))
    closest = min(failures, key=lambda missing: (len(missing), missing))
    return PrerequisiteEvaluation(False, closest, "prerequisite not satisfied")


def unmet_prerequisites(
    rows: list[dict[str, Any]],
    completed: list[dict],
    *,
    program_code: str | None = None,
    curriculum_version: str | None = None,
) -> tuple[str, ...]:
    """Return the closest unsatisfied alternative set, or nothing when eligible."""
    if not rows:
        return ()
    result = evaluate_prerequisites(
        rows,
        completed,
        program_code=program_code,
        curriculum_version=curriculum_version,
    )
    if result.eligible is None:
        raise ValueError(result.reason or "prerequisites could not be verified")
    return result.missing


async def filter_courses(
    db: AsyncSession,
    user_id: UUID,
    catalog: CatalogSession,
    semester: str,
    courses: list[dict[str, Any]],
    completed: list[dict],
    *,
    program_code: str | None = None,
    curriculum_version: str | None = None,
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
            evaluation = evaluate_prerequisites(
                _rows(payload),
                completed,
                program_code=program_code,
                curriculum_version=curriculum_version,
            )
        except (HTTPException, ValueError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            warnings.append(f"{display_code(code)}: prerequisites could not be verified ({detail}).")
            continue
        if evaluation.eligible is None:
            warnings.append(
                f"{display_code(code)}: prerequisites could not be verified "
                f"({evaluation.reason or 'academic rule is unknown'})."
            )
        elif evaluation.missing:
            rejected.append(PrerequisiteRejection(code, evaluation.missing))
        else:
            approved.append(course)
    return approved, rejected, warnings
