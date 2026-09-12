"""Application services for the reviewed academic catalog.

The module owns three boundaries:

* source observations are retained as evidence and may only update drafts;
* administrators publish immutable revisions through an active release pointer;
* student and agent reads resolve one published release and never call METU.

The functions intentionally accept an ``AsyncSession`` rather than hiding one
inside the service.  Import workers, API requests and tests can therefore
choose their transaction boundary and a caller can pin a release for a whole
planning request.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import and_, case, cast, delete, exists, func, literal, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.academic_catalog.models import (
    CatalogAdminOverride,
    CatalogCourse,
    CatalogCourseReplacement,
    CatalogCourseRevision,
    CatalogDraft,
    CatalogImportJob,
    CatalogInstructor,
    CatalogMeeting,
    CatalogPrerequisiteGroup,
    CatalogPrerequisiteRequirement,
    CatalogPublicationOperation,
    CatalogRelease,
    CatalogReleaseItem,
    CatalogRestriction,
    CatalogSection,
    CatalogSectionInstructor,
    CatalogSourceObservation,
    CatalogTerm,
    CatalogTermActiveRelease,
)
from app.academic_catalog.schemas import normalize_course_code, normalize_term
from app.campus import departments as department_directory
from app.campus.prerequisites import display_code


CATALOG_TOOLS = frozenset(
    {
        "get_departments_and_semesters",
        "search_departments",
        "list_program_courses",
        "get_course_info",
        "get_section_constraints",
        "get_course_prerequisites",
        "get_course_replacements",
        "get_thesis_courses",
    }
)

COMPONENTS = (
    "directory",
    "listing",
    "details",
    "sections",
    "constraints",
    "prerequisites",
    "replacements",
)

# Registrar data changes at different rates.  These limits are read-time
# policy, so an immutable release retains its evidence while consumers stop
# calling an old observation fresh after its component window expires.
_COMPONENT_MAX_AGE_SECONDS = {
    "directory": 30 * 24 * 60 * 60,
    "listing": 30 * 24 * 60 * 60,
    "details": 7 * 24 * 60 * 60,
    "sections": 7 * 24 * 60 * 60,
    "constraints": 7 * 24 * 60 * 60,
    "prerequisites": 30 * 24 * 60 * 60,
    "replacements": 30 * 24 * 60 * 60,
}

_SEVEN_DIGIT = re.compile(r"^\d{7}$")
_ERROR_PREFIXES = ("Error from MCP tool ", "MCP tool ", "Error: ")
_DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "pazartesi": 0,
    "tue": 1,
    "tuesday": 1,
    "sali": 1,
    "salı": 1,
    "wed": 2,
    "wednesday": 2,
    "carsamba": 2,
    "çarşamba": 2,
    "thu": 3,
    "thursday": 3,
    "persembe": 3,
    "perşembe": 3,
    "fri": 4,
    "friday": 4,
    "cuma": 4,
    "pzt": 0, "sal": 1, "çar": 2, "car": 2, "per": 3, "cum": 4,
    "sat": 5, "saturday": 5, "cumartesi": 5, "cmt": 5,
    "sun": 6, "sunday": 6, "pazar": 6, "paz": 6,
}


class CatalogUnavailable(HTTPException):
    """Raised when no published release can satisfy a catalog read."""

    def __init__(self, term: str | None = None):
        detail = {
            "code": "catalog_unavailable",
            "message": "No published academic catalog is available for this term.",
            "term": term,
        }
        super().__init__(status.HTTP_503_SERVICE_UNAVAILABLE, detail)


class CatalogConflict(HTTPException):
    """Raised when an optimistic release or draft check fails."""

    def __init__(self, message: str, **extra: Any):
        detail: dict[str, Any] = {"code": "catalog_conflict", "message": message}
        detail.update(extra)
        super().__init__(status.HTTP_409_CONFLICT, detail)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def _parse_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if value in (None, ""):
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    """Convert source/Pydantic values into JSONB-safe plain data."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold().translate(str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c"})))


def _field(record: dict[str, Any], *names: str) -> Any:
    wanted = {_key(name) for name in names}
    for name, value in record.items():
        if _key(name) in wanted:
            return value
    return None


def _decode_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _decode_payload(json.loads(stripped))
            except (TypeError, ValueError, json.JSONDecodeError):
                return value
        return value
    if isinstance(value, list):
        return [_decode_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_payload(item) for key, item in value.items()}
    return value


def _scope(values: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    term = _field(values, "term", "semester", "semester_code", "academic_term")
    department = _field(values, "department", "department_code", "dept", "program")
    course = _field(values, "course", "course_code", "course_id")
    section = _field(values, "section", "section_code", "section_number")
    normalized_course: str | None = None
    if course not in (None, ""):
        normalized_course = normalize_course_code(str(course))
    return (
        normalize_term(str(term)) if term not in (None, "") else None,
        str(department).strip() if department not in (None, "") else None,
        normalized_course,
        str(section).strip() if section not in (None, "") else None,
    )


def _resolve_catalog_scope(department: str | None, requested_course: str | None) -> tuple[str | None, str | None]:
    """Map a student's lettered code onto the catalog's numeric key.

    "EE 201" is 5670201 in the catalog, and the agent hands the lettered form
    straight through. Without this the published read compares "EE201" against
    the numeric key, finds nothing, and answers "not available in the published
    release" for a course the admin panel lists. The admin listing already
    resolves codes this way; this brings the student-facing read in line.
    """
    if requested_course is not None:
        expanded = department_directory.expand_course_code(requested_course)
        if expanded is not None:
            requested_course, expanded_department = expanded
            department = department or expanded_department.code
    if department:
        resolved_department = department_directory.resolve(department)
        if resolved_department is not None:
            department = resolved_department.code
    return department, requested_course


def _rows(value: Any, names: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Find labelled rows without treating arbitrary dictionaries as tables."""

    value = _decode_payload(value)
    wanted = {_key(name) for name in names}
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for name, child in value.items():
        if _key(name) in wanted and isinstance(child, list):
            return [item for item in child if isinstance(item, dict)]
    for name in ("result", "value", "data", "items", "courses", "sections", "constraints", "prerequisites", "replacements"):
        child = _field(value, name)
        if isinstance(child, list):
            return [item for item in child if isinstance(item, dict)]
    if any(_field(value, name) is not None for name in ("course_code", "code", "section", "title")):
        return [value]
    return []


def _course_code(record: dict[str, Any], fallback: str | None = None) -> str | None:
    candidate = _field(record, "course_code", "prerequisite_course_code", "replaced_course_code", "related_course_code", "course", "code", "id")
    if candidate in (None, ""):
        candidate = fallback
    if candidate in (None, ""):
        return None
    value = normalize_course_code(str(candidate))
    return value if _SEVEN_DIGIT.fullmatch(value) else None


def _title(record: dict[str, Any]) -> str | None:
    value = _field(record, "title", "course_title", "name", "course_name")
    return str(value).strip() if value not in (None, "") else None


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool | None:
    """Parse source booleans while preserving unknown values as ``None``."""

    if isinstance(value, bool):
        return value
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    folded = _key(value)
    if folded in {"true", "yes", "y", "1", "available", "mevcut", "var"}:
        return True
    if folded in {"false", "no", "n", "0", "unavailable", "yok", "none"}:
        return False
    return None


def _minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        if 0 <= number <= 24:
            return number * 60
        return number if 0 <= number <= 1440 else None
    match = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})", str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _weekday(value: Any) -> int | None:
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    if isinstance(value, str) and value.strip() in {str(day) for day in range(7)}:
        return int(value.strip())
    folded = _key(value)
    if folded in {_key(alias) for alias in _DAY_ALIASES}:
        return next(day for alias, day in _DAY_ALIASES.items() if _key(alias) == folded)
    for alias, day in _DAY_ALIASES.items():
        if _key(alias) == folded or _key(alias) in folded:
            return day
    return None


def _meeting(record: dict[str, Any]) -> dict[str, Any]:
    day_value = _field(record, "weekday", "day", "week_day", "gun")
    start_value = _field(record, "start_minute", "start", "start_time", "begin", "baslangic")
    end_value = _field(record, "end_minute", "end", "end_time", "finish", "bitis")
    schedule = _field(record, "time", "hours", "schedule", "meeting", "saat")
    if (start_value is None or end_value is None) and schedule not in (None, ""):
        match = re.search(r"(\d{1,2}[:.]\d{2})\s*(?:-|–|—|to)\s*(\d{1,2}[:.]\d{2})", str(schedule))
        if match:
            start_value, end_value = match.groups()
    weekday = _weekday(day_value)
    start = _minutes(start_value)
    end = _minutes(end_value)
    raw_label = str(_field(record, "raw_label", "label", "schedule", "time") or "").strip() or None
    room = _field(record, "room", "classroom", "location", "derslik")
    status_value = str(_field(record, "status", "meeting_status") or "").strip().lower()
    if status_value not in {"scheduled", "untimed", "unpublished", "invalid", "unknown"}:
        status_value = "scheduled" if weekday is not None and start is not None and end is not None and end > start else "invalid"
    if status_value == "scheduled" and (weekday is None or start is None or end is None or end <= start):
        status_value = "invalid"
    return {
        "weekday": weekday,
        "start_minute": start,
        "end_minute": end,
        "room": str(room).strip() if room not in (None, "") else None,
        "status": status_value,
        "raw_label": raw_label,
    }


def _meeting_rows(record: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    marker = object()
    raw = marker
    for name in ("meetings", "meeting", "schedule", "hours", "times"):
        value = _field(record, name)
        if value is not None:
            raw = value
            break
    if raw is marker:
        return [], False
    raw = _decode_payload(raw)
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], False
    return [_meeting(item) for item in raw if isinstance(item, dict)], True


def _explicit_untimed(record: dict[str, Any]) -> bool:
    """Return whether a source row explicitly asserts that it has no time.

    An empty meetings array is not enough to make that assertion: many source
    responses omit schedule data when the request failed or when a section was
    not included in the response.  Only the dedicated status fields can make
    an empty schedule usable as an explicitly untimed section.
    """

    marker = _field(
        record,
        "meetings_status",
        "meeting_status",
        "timing_status",
        "schedule_status",
    )
    return _key(marker) in {"untimed", "explicitlyuntimed"}


def _section_meetings_status(
    record: dict[str, Any], meetings: list[dict[str, Any]], meeting_field_present: bool
) -> str:
    """Normalize section timing evidence without inferring it from absence.

    ``untimed`` is an explicit positive assertion.  If it appears alongside
    any meeting row, the source is contradictory and therefore invalid; this
    keeps malformed times from becoming planner-usable through a status flag.
    """

    if _explicit_untimed(record):
        return "invalid" if meetings else "untimed"
    if meeting_field_present and meetings:
        return "verified" if all(row["status"] == "scheduled" for row in meetings) else "invalid"
    return "unknown"


def _instructor_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = _field(record, "instructors", "instructor", "lecturer", "teacher")
    if value in (None, ""):
        return []
    if isinstance(value, str):
        names = [item.strip() for item in re.split(r"[,;/|]", value) if item.strip()]
        return [{"source_name": name} for name in names]
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                rows.append({"source_name": item.strip()})
            elif isinstance(item, dict):
                name = _field(item, "source_name", "name", "instructor", "lecturer", "teacher")
                if name not in (None, ""):
                    rows.append(
                        {
                            "source_name": str(name).strip(),
                            "position": _field(item, "position", "title", "academic_title"),
                            "researcher_id": _int(_field(item, "researcher_id", "researcher")),
                        }
                    )
        return rows
    return []


def _restriction_rows(value: Any) -> tuple[list[dict[str, Any]], bool]:
    value = _decode_payload(value)
    marker = object()
    raw: Any = marker
    if isinstance(value, dict):
        for name in ("constraints", "restrictions", "eligibility", "rows", "items"):
            candidate = _field(value, name)
            if candidate is not None:
                raw = candidate
                break
    elif isinstance(value, list):
        raw = value
    if raw is marker:
        return [], False
    if not isinstance(raw, list):
        return [], False
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], False
        if not any(_field(item, key) is not None for key in (
            "given_dept", "given_department", "department", "kind", "type", "min_cgpa", "start_char",
        )):
            return [], False
        for keys in (("min_cgpa", "gpa_min", "minimum_gpa", "min_gpa"),
                     ("max_cgpa", "gpa_max", "maximum_gpa", "max_gpa"),
                     ("min_year", "year_min", "minimum_year"), ("max_year", "year_max", "maximum_year")):
            value = _field(item, *keys)
            if value not in (None, "") and _number(value) is None:
                return [], False
        row = {
            "restriction_group": str(_field(item, "restriction_group", "group", "group_no", "set_no") or "1"),
            "row_index": _int(_field(item, "row_index", "index", "row_no")) or index,
            "kind": str(_field(item, "kind", "type", "constraint_type") or "eligibility"),
            "value_text": _field(item, "value_text", "value", "description"),
            "value_numeric": _number(_field(item, "value_numeric", "value_number")),
            "operator": _field(item, "operator", "op"),
            "minimum_grade": _field(item, "minimum_grade", "prior_grade", "grade_min"),
            "given_department": _field(item, "given_dept", "given_department", "department", "department_code", "dept"),
            "start_char": _field(item, "start_char", "surname_start", "surname_from", "start_surname"),
            "end_char": _field(item, "end_char", "surname_end", "surname_to", "end_surname"),
            "min_cgpa": _number(_field(item, "min_cgpa", "gpa_min", "minimum_gpa", "min_gpa")),
            "max_cgpa": _number(_field(item, "max_cgpa", "gpa_max", "maximum_gpa", "max_gpa")),
            "min_year": _int(_field(item, "min_year", "year_min", "minimum_year")),
            "max_year": _int(_field(item, "max_year", "year_max", "maximum_year")),
            "start_grade": _field(item, "start_grade", "grade_from"),
            "end_grade": _field(item, "end_grade", "grade_to"),
            "prior_course_code": _course_code(item, None),
            "program_code": _field(item, "program_code", "program"),
            "curriculum_version": _field(item, "curriculum_version", "dept_version", "curriculum", "version"),
            "raw_text": _field(item, "raw_text", "text", "description", "constraint"),
            "verified": _field(item, "verified", "is_verified") is not False,
            "status": str(_field(item, "status", "state") or ("unknown" if _field(item, "verified", "is_verified") is False else "verified")),
        }
        row["value_text"] = str(row["value_text"]).strip() if row["value_text"] not in (None, "") else None
        rows.append(row)
    return rows, True


def _prerequisite_groups(value: Any) -> tuple[list[dict[str, Any]], bool]:
    value = _decode_payload(value)
    raw: Any = None
    if isinstance(value, dict):
        # `result` belongs here for the same reason it is in _rows: the source
        # does not answer in one shape. Read live, get_course_prerequisites
        # hands back a bare list; the same call through the cache hands back
        # {"result": [...]}. Without the key this read the second shape as no
        # table at all and recorded missing_prerequisites_table.
        #
        # Measured: MATH 219 published with nine sections and an empty
        # prerequisite_groups, while the source lists MATH 120 at DD for it -
        # and 513 courses were held out of publication by the same issue. The
        # planner's prerequisite check was running against nothing.
        raw = _field(
            value, "groups", "prerequisite_groups", "prerequisites", "requirements", "rows",
            "result", "value", "data", "items",
        )
    elif isinstance(value, list):
        raw = value
    if raw is None:
        return [], False
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], False
    groups: dict[tuple[int, str | None, str | None], dict[str, Any]] = {}
    malformed = False

    def has_course_identity(item: dict[str, Any]) -> bool:
        return any(
            _field(item, name) not in (None, "")
            for name in (
                "course_code",
                "prerequisite_course_code",
                "replaced_course_code",
                "related_course_code",
                "course",
                "code",
                "id",
            )
        )

    # `position` is deliberately not read as an ordinal here. METU sends it on
    # every prerequisite row as a course *status* - "Offered Course / Açık
    # Ders", "Closed Course / Kapalı Ders" - and the name collides with this
    # parser's own idea of a position, which is an index. Treating the label as
    # an ordinal made _int() return None and marked the row malformed, so every
    # course that actually had a prerequisite was recorded as unreadable while
    # every course without one parsed cleanly.
    #
    # Measured: 513 courses held out of publication, and MATH 219 published
    # with nine sections and no prerequisites while the source lists MATH 120
    # at DD for it. `order` and `index` are this parser's own spellings and
    # still mean what they say; where neither is given the row keeps its place
    # in the list, which is the order the source sent them in.
    def has_position(item: dict[str, Any]) -> bool:
        return _field(item, "order", "index") not in (None, "")

    def valid_position(item: dict[str, Any]) -> bool:
        value = _field(item, "order", "index")
        return value in (None, "") or _int(value) is not None

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            malformed = True
            continue
        group_value = _field(item, "group_no", "set_no", "set", "group")
        group_no = _int(group_value) if group_value not in (None, "") else 1
        if group_no is None or group_no < 1:
            malformed = True
            group_no = 1
        program = _field(item, "program_code", "program")
        curriculum = _field(item, "curriculum_version", "dept_version", "curriculum", "version")
        key = (group_no, str(program) if program not in (None, "") else None, str(curriculum) if curriculum not in (None, "") else None)
        logic_value = _field(item, "logic", "operator")
        logic = str(logic_value or "AND").upper()
        if logic not in {"AND", "OR"}:
            malformed = True
            logic = "AND"
        applicability = _field(item, "applicability", "applies_to")
        if applicability not in (None, "") and not isinstance(applicability, dict):
            malformed = True
            applicability = {}
        group = groups.setdefault(
            key,
            {
                "group_no": group_no,
                "logic": logic,
                "program_code": key[1],
                "curriculum_version": key[2],
                "applicability": applicability or {},
                "verified": bool(_field(item, "verified", "is_verified") if _field(item, "verified", "is_verified") is not None else True),
                "raw_text": _field(item, "raw_text", "text", "description"),
                "requirements": [],
            },
        )
        code = _course_code(item, None)
        if has_course_identity(item) and code is None:
            malformed = True
        if not valid_position(item):
            malformed = True
        if code:
            group["requirements"].append(
                {
                    "course_code": code,
                    "minimum_grade": _field(item, "minimum_grade", "grade_min", "min_grade", "required_grade"),
                    "requirement_type": str(_field(item, "requirement_type", "type") or "course"),
                    "position": _int(_field(item, "order", "index")) or index,
                    "raw_text": _field(item, "raw_text", "text", "description"),
                }
            )
        nested = _field(item, "requirements", "courses", "items")
        if nested is not None and not isinstance(nested, list):
            malformed = True
        elif isinstance(nested, list):
            if not nested and code is None:
                malformed = True
            for position, requirement in enumerate(nested):
                if not isinstance(requirement, dict):
                    malformed = True
                    continue
                if not has_course_identity(requirement):
                    malformed = True
                nested_code = _course_code(requirement, None)
                if nested_code is None:
                    malformed = True
                if not valid_position(requirement):
                    malformed = True
                if nested_code:
                    group["requirements"].append(
                        {
                            "course_code": nested_code,
                            "minimum_grade": _field(requirement, "minimum_grade", "grade_min", "min_grade", "required_grade"),
                            "requirement_type": str(_field(requirement, "requirement_type", "type") or "course"),
                            "position": _int(_field(requirement, "order", "index")) or position,
                            "raw_text": _field(requirement, "raw_text", "text", "description"),
                        }
                    )
    parsed = list(groups.values())
    if raw and (malformed or not parsed or any(not group["requirements"] for group in parsed)):
        return parsed, False
    return parsed, True


def _replacement_rows(value: Any) -> tuple[list[dict[str, Any]], bool]:
    value = _decode_payload(value)
    # Same envelope, same reason as _prerequisite_groups above: a cached read
    # of this tool answers {"result": [...]} where a live one answers a list.
    raw: Any = (
        _field(value, "replacements", "exclusions", "relationships", "result", "value", "data", "items")
        if isinstance(value, dict)
        else value
    )
    if raw is None:
        return [], False
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], False
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            code = _course_code({"course_code": item})
            if code:
                rows.append({"relationship_type": "replacement", "related_course_code": code, "verified": True})
            continue
        if not isinstance(item, dict):
            continue
        code = _course_code(item, None)
        if not code:
            continue
        rows.append(
            {
                "relationship_type": str(_field(item, "relationship_type", "type", "kind") or "replacement"),
                "related_course_code": code,
                "program_code": _field(item, "program_code", "program"),
                "curriculum_version": _field(item, "curriculum_version", "dept_version", "curriculum", "version"),
                "verified": bool(_field(item, "verified", "is_verified") if _field(item, "verified", "is_verified") is not None else True),
                "raw_text": _field(item, "raw_text", "text", "description"),
            }
        )
    return rows, not raw or len(rows) == len(raw)


def _section_rows(value: Any) -> tuple[list[dict[str, Any]], bool]:
    payload = _decode_payload(value)
    rows = _rows(payload, ("sections", "offerings", "sections_list"))
    if not rows and isinstance(payload, dict) and _field(payload, "section", "section_code", "section_number") is not None:
        rows = [payload]
    found: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        code = _field(item, "section_code", "section_number", "section", "section_no", "sec")
        section_code = str(code).strip() if code not in (None, "") else str(index + 1)
        meetings, meeting_field_present = _meeting_rows(item)
        instructors = _instructor_rows(item)
        restrictions, restriction_field_present = _restriction_rows(item)
        found.append(
            {
                "section_code": section_code,
                "status": str(_field(item, "status", "state") or "listed"),
                "notes": _field(item, "notes", "critical_info", "critical", "constraint", "comment"),
                "syllabus_url": _field(item, "syllabus_url", "syllabus", "url"),
                "syllabus_available": _boolish(
                    _field(item, "syllabus_available", "syllabusAvailable", "has_syllabus", "syllabus_exists")
                ),
                "meetings_status": _section_meetings_status(item, meetings, meeting_field_present),
                "meetings": meetings,
                "instructors": instructors,
                "restrictions": restrictions,
                "restrictions_present": restriction_field_present,
            }
        )
    field_present = isinstance(payload, dict) and any(isinstance(_field(payload, key), list) for key in ("sections", "offerings", "sections_list"))
    return found, bool(rows) or field_present


def _course_row_data(record: dict[str, Any], fallback_code: str | None, *, include_sections: bool = True) -> tuple[str | None, dict[str, Any]]:
    code = _course_code(record, fallback_code)
    data: dict[str, Any] = {}
    title = _title(record)
    if title is not None:
        data["title"] = title
    department = _field(record, "department", "department_code", "dept", "program")
    if department not in (None, ""):
        department_text = str(department).strip()
        if re.fullmatch(r"\d{3}", department_text):
            data["department"] = department_text
        else:
            data["department_name"] = department_text
            if code:
                data["department"] = code[:3]
    credits = _number(_field(record, "local_credits", "credits", "credit", "local_credit", "hours"))
    if credits is None and (credit_info := _field(record, "credit_info")):
        match = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:\(|$)", str(credit_info))
        credits = _number(match.group(1)) if match else None
    if credits is not None:
        data["local_credits"] = credits
    ects = _number(_field(record, "ects", "ects_credit", "european_credits", "akts"))
    if ects is not None:
        data["ects"] = ects
    level = _field(record, "level", "course_level")
    if level not in (None, ""):
        data["level"] = str(level).strip()
    availability = _field(record, "availability", "offered", "type", "semester_availability")
    if availability not in (None, ""):
        data["availability"] = str(availability).strip()
    campus = _field(record, "campus", "location", "campus_name")
    if campus not in (None, ""):
        data["campus"] = str(campus).strip()
    if include_sections:
        sections, present = _section_rows(record)
        if present:
            data["sections"] = sections
    return code, data


def _tool_component(tool: str) -> str:
    return {
        "get_departments_and_semesters": "directory",
        "search_departments": "directory",
        "list_program_courses": "listing",
        "get_course_info": "details",
        "get_section_constraints": "constraints",
        "get_course_prerequisites": "prerequisites",
        "get_course_replacements": "replacements",
        "get_thesis_courses": "listing",
    }.get(tool, "details")


# One canonical field -> component map.  The draft editor and the override
# removal path must agree: an edit that marks a component nothing reads leaves
# the real one looking fresh.  ``is_thesis`` belongs to the listing.
FIELD_COMPONENT_MAP = {
    "title": "details",
    "department": "listing",
    "local_credits": "details",
    "ects": "details",
    "level": "details",
    "availability": "details",
    "campus": "details",
    "is_thesis": "listing",
    "sections": "sections",
    "prerequisite_groups": "prerequisites",
    "replacements": "replacements",
    "completeness": "details",
}


def override_components(overrides: dict[str, Any]) -> dict[str, list[str]]:
    """Group the retained overrides by the component whose freshness they gate.

    A component is only as verified as its least verified override, so the
    grouping has to be explicit: ``details`` alone covers six editable fields.
    ``sections`` additionally gates ``constraints``, because a hand-edited
    section roster decides which restriction tables still describe the course.
    """

    grouped: dict[str, list[str]] = {}
    for field in overrides:
        grouped.setdefault(FIELD_COMPONENT_MAP.get(field, field), []).append(field)
        if field == "sections":
            grouped.setdefault("constraints", []).append(field)
    return grouped


def _override_evidence(entry: Any) -> str | None:
    """Return the verification evidence recorded against one override."""

    if not isinstance(entry, dict):
        return None
    evidence = str(entry.get("verification_evidence") or "").strip()
    return evidence or None


def apply_override_component_status(
    component_status: dict[str, Any],
    overrides: dict[str, Any],
    components: Iterable[str],
    *,
    observed_at: str | None,
) -> None:
    """Re-derive the admin freshness of every component the overrides gate.

    Verification is recorded per field rather than per component.  Marking the
    component itself verified would let evidence for ``title`` certify an
    untouched ``ects`` correction that happens to share ``details``, so a
    component counts as verified only while every override under it carries
    evidence, and it falls back to pending as soon as a new edit lands.
    """

    grouped = override_components(overrides)
    for component in components:
        fields = grouped.get(component, [])
        entries = [overrides[field] for field in fields]
        verified = [entry for entry in entries if _override_evidence(entry)]
        fully_verified = bool(entries) and len(verified) == len(entries)
        meta = dict(component_status.get(component) or {})
        meta.update(
            {
                "component": component,
                "source_status": "admin_verified" if fully_verified else "admin_pending",
                "verified": fully_verified,
                "fresh": fully_verified,
                "observed_at": observed_at,
                "source_fetched_at": None,
            }
        )
        if fully_verified:
            latest = max(verified, key=lambda entry: str(entry.get("verified_at") or ""))
            meta["verification_evidence"] = _override_evidence(latest)
            meta["verified_at"] = latest.get("verified_at")
        else:
            meta.pop("verification_evidence", None)
            meta.pop("verified_at", None)
        component_status[component] = meta


_MANUAL_VERIFICATION_ISSUE = {
    "code": "manual_verification_required",
    "message": "Administrator edit requires explicit verification before publication is considered fresh",
    "severity": "warning",
}


def _sync_manual_verification_issue(draft: CatalogDraft) -> None:
    """Keep one pending-verification marker in step with the retained overrides.

    The marker used to be appended on every unverified edit and never removed:
    after the administrator supplied verification evidence, or removed the
    correction again, the draft still carried a warning that no longer
    described it.
    """

    issues = [
        item
        for item in (draft.issues or [])
        if not (isinstance(item, dict) and item.get("code") == _MANUAL_VERIFICATION_ISSUE["code"])
    ]
    # Read the evidence off each override rather than off its component.  Two
    # corrections can share one component, and only the unverified one should
    # keep the warning alive.
    pending = any(
        _override_evidence(entry) is None for entry in (draft.field_overrides or {}).values()
    )
    if pending:
        issues.append(dict(_MANUAL_VERIFICATION_ISSUE))
    draft.issues = issues[-200:]


def _error_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith(_ERROR_PREFIXES):
        return value
    return None


def _parse_observation(tool: str, values: dict[str, Any], payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Normalize one source response into draft fields and explicit issues."""

    payload = _decode_payload(payload)
    component = _tool_component(tool)
    issues: list[dict[str, Any]] = []
    error = _error_text(payload)
    if error:
        return {}, [{"component": component, "code": "source_failed", "message": error, "severity": "error"}], "failed"
    if payload is None:
        return {}, [{"component": component, "code": "source_failed", "message": "Source returned no payload", "severity": "error"}], "failed"
    if isinstance(payload, str):
        return {}, [{"component": component, "code": "malformed_payload", "message": "Source payload was not JSON", "severity": "error"}], "malformed"

    term, department, requested_code, requested_section = _scope(values)
    if isinstance(payload, dict):
        identities = (
            ("course", requested_code, _field(payload, "course_code", "course")),
            ("term", term, _field(payload, "semester", "semester_code", "term")),
            ("section", requested_section, _field(payload, "section_code", "section")),
        )
        for identity, expected, actual in identities:
            if expected is not None and actual not in (None, "") and str(actual).strip() != str(expected):
                return {}, [{"component": component, "code": "source_identity_mismatch",
                             "message": f"Source {identity} does not match the requested {identity}",
                             "severity": "error"}], "malformed"
    if tool in {"get_departments_and_semesters", "search_departments"}:
        departments = _rows(payload, ("departments", "department", "results"))
        semesters = _rows(payload, ("semesters", "terms", "academic_terms"))
        result: dict[str, Any] = {
            "departments": [
                {
                    "code": str(_field(row, "code", "department_code", "id") or "").strip(),
                    "name": str(_field(row, "name", "department_name", "title") or "").strip(),
                }
                for row in departments
                if _field(row, "code", "department_code", "id") not in (None, "")
            ],
            "semesters": [
                {
                    "code": str(_field(row, "code", "term", "semester", "id") or "").strip(),
                    "name": str(_field(row, "name", "label", "title") or "").strip(),
                }
                for row in semesters
                if _field(row, "code", "term", "semester", "id") not in (None, "")
            ],
        }
        if not result["departments"] and not result["semesters"] and isinstance(payload, dict):
            issues.append({"component": component, "code": "unlabelled_payload", "message": "No labelled directory rows", "severity": "error"})
            return result, issues, "malformed"
        return result, issues, "empty" if not result["departments"] and not result["semesters"] else "success"

    if tool in {"list_program_courses", "get_thesis_courses"}:
        rows = _rows(payload, ("courses", "course_list", "results", "items"))
        if not rows and isinstance(payload, dict):
            rows = [payload] if _course_code(payload, requested_code) else []
        courses: list[dict[str, Any]] = []
        for row in rows:
            code, data = _course_row_data(row, requested_code, include_sections=False)
            if code:
                if tool == "get_thesis_courses":
                    data["thesis"] = True
                courses.append({"course_code": code, "data": data})
        result = {"courses": courses}
        if not courses and rows:
            issues.append({"component": component, "code": "course_identity_unresolved", "message": "Course rows did not contain full seven-digit codes", "severity": "error"})
            return result, issues, "malformed"
        return result, issues, "empty" if not courses else "success"

    if tool == "get_course_info":
        code, data = _course_row_data(payload if isinstance(payload, dict) else {}, requested_code, include_sections=True)
        if code is None:
            code = requested_code
        sections = data.get("sections", [])
        if requested_code is None and code is None:
            issues.append({"component": component, "code": "course_identity_unresolved", "message": "Course detail did not identify a full seven-digit code", "severity": "error"})
            return data, issues, "malformed"
        if not data:
            issues.append({"component": component, "code": "empty_detail", "message": "Course detail contained no typed fields", "severity": "error"})
            return {"course_code": code, "sections": sections}, issues, "empty"
        data["course_code"] = code
        return data, issues, "success"

    if tool == "get_section_constraints":
        rows, present = _restriction_rows(payload)
        if not present:
            issues.append({"component": component, "code": "missing_constraints_table", "message": "Restriction table was unavailable or malformed", "severity": "error"})
            return {}, issues, "malformed"
        return {"restrictions": rows, "section_code": _scope(values)[3]}, issues, "empty" if not rows else "success"

    if tool == "get_course_prerequisites":
        groups, present = _prerequisite_groups(payload)
        if not present:
            issues.append({"component": component, "code": "missing_prerequisites_table", "message": "Prerequisite groups were unavailable or malformed", "severity": "error"})
            return {}, issues, "malformed"
        return {"prerequisite_groups": groups}, issues, "empty" if not groups else "success"

    if tool == "get_course_replacements":
        rows, present = _replacement_rows(payload)
        if not present:
            issues.append({"component": component, "code": "missing_replacements_table", "message": "Replacement table was unavailable or malformed", "severity": "error"})
            return {}, issues, "malformed"
        return {"replacements": rows}, issues, "empty" if not rows else "success"

    issues.append({"component": component, "code": "unsupported_tool", "message": f"Unsupported source tool: {tool}", "severity": "error"})
    return {}, issues, "failed"


def _component_meta(
    component: str,
    *,
    status_value: str,
    observed_at: datetime,
    source_fetched_at: datetime | None,
    verified: bool,
) -> dict[str, Any]:
    fetched = _utc(source_fetched_at)
    observed = _utc(observed_at)
    return {
        "component": component,
        "source_status": status_value,
        "verified": verified,
        # An imported cache snapshot has an observed timestamp but no actual
        # source fetch timestamp, so it remains browsable yet stale.
        "fresh": fetched is not None,
        "observed_at": _iso(observed),
        "source_fetched_at": _iso(fetched),
    }


def _merge_dicts(existing: dict[str, Any], incoming: dict[str, Any], overrides: dict[str, Any], *, replace_sections: bool = False) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key.startswith("_"):
            continue
        if key not in overrides:
            if key == "sections" and isinstance(value, list) and isinstance(merged.get(key), list):
                # Detail and restriction tools arrive independently.  Merge
                # by section identity so a later details response cannot erase
                # a restriction response that was already reviewed.
                old = {
                    str(item.get("section_code") or item.get("section") or ""): dict(item)
                    for item in merged[key]
                    if isinstance(item, dict)
                }
                combined: list[dict[str, Any]] = []
                seen: set[str] = set()
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("section_code") or item.get("section") or len(combined) + 1)
                    previous = old.get(code, {})
                    merged_item = dict(previous)
                    for child_key, child_value in item.items():
                        if child_key == "restrictions" and item.get("restrictions_present") is False:
                            continue
                        merged_item[child_key] = _jsonable(child_value)
                    combined.append(merged_item)
                    seen.add(code)
                if not replace_sections:
                    combined.extend(item for code, item in old.items() if code not in seen)
                merged[key] = combined
            else:
                merged[key] = _jsonable(value)
    return merged


async def _ensure_term(db: AsyncSession, organization_id: UUID, term_code: str, *, label: str | None = None) -> CatalogTerm:
    term_code = normalize_term(term_code)
    row = await db.scalar(
        select(CatalogTerm).where(CatalogTerm.organization_id == organization_id, CatalogTerm.term_code == term_code)
    )
    if row is None:
        row = CatalogTerm(organization_id=organization_id, term_code=term_code, label=label)
        db.add(row)
        await db.flush()
    elif label and not row.label:
        row.label = label
    return row


async def _ensure_course(
    db: AsyncSession,
    organization_id: UUID,
    course_code: str,
    *,
    department: str | None = None,
) -> CatalogCourse:
    normalized = normalize_course_code(course_code)
    if not _SEVEN_DIGIT.fullmatch(normalized):
        raise ValueError("Catalog course identities require a full seven-digit METU code")
    row = await db.scalar(
        select(CatalogCourse).where(
            CatalogCourse.organization_id == organization_id,
            CatalogCourse.course_code == normalized,
        )
    )
    department_value = str(department or normalized[:3]).strip()
    if row is None:
        row = CatalogCourse(organization_id=organization_id, course_code=normalized, department=department_value)
        db.add(row)
        await db.flush()
    elif department and row.department != department_value:
        # Source identity is stable.  A changed owner is retained as a source
        # conflict for an administrator instead of moving the course silently.
        aliases = list(row.aliases or [])
        marker = {"source_department": department_value}
        if marker not in aliases:
            aliases.append(marker)
            row.aliases = aliases
    return row


async def _active_revision_for_course(
    db: AsyncSession,
    organization_id: UUID,
    term_id: UUID,
    course_id: UUID,
) -> CatalogCourseRevision | None:
    """Return the revision selected by the term's current release pointer."""

    pointer = await db.scalar(
        select(CatalogTermActiveRelease).where(
            CatalogTermActiveRelease.organization_id == organization_id,
            CatalogTermActiveRelease.term_id == term_id,
        )
    )
    if pointer is None:
        return None
    return await db.scalar(
        select(CatalogCourseRevision)
        .join(CatalogReleaseItem, CatalogReleaseItem.course_revision_id == CatalogCourseRevision.id)
        .where(
            CatalogReleaseItem.release_id == pointer.release_id,
            CatalogReleaseItem.course_id == course_id,
            CatalogCourseRevision.organization_id == organization_id,
            CatalogCourseRevision.term_id == term_id,
            CatalogCourseRevision.state == "published",
        )
    )


async def _overrides_for_revision(
    db: AsyncSession,
    organization_id: UUID,
    term_id: UUID,
    course_id: UUID,
    revision_id: UUID,
) -> dict[str, Any]:
    """Load the override snapshot that belongs to one published revision.

    A global active-override query is incorrect after rollback: it can apply
    a correction from a newer release to the older revision being restored.
    Published drafts already carry the complete inherited snapshot, so use it
    as the canonical source and only fall back to individual override rows
    for legacy revisions that have no draft record.
    """

    published_draft = await db.scalar(
        select(CatalogDraft)
        .where(
            CatalogDraft.organization_id == organization_id,
            CatalogDraft.term_id == term_id,
            CatalogDraft.course_id == course_id,
            CatalogDraft.published_revision_id == revision_id,
        )
        .order_by(CatalogDraft.updated_at.desc())
        .limit(1)
    )
    if published_draft is not None:
        return _jsonable(dict(published_draft.field_overrides or {}))
    rows = (
        await db.scalars(
            select(CatalogAdminOverride)
            .join(CatalogDraft, CatalogDraft.id == CatalogAdminOverride.draft_id)
            .where(
                CatalogAdminOverride.organization_id == organization_id,
                CatalogAdminOverride.term_id == term_id,
                CatalogAdminOverride.course_id == course_id,
                CatalogAdminOverride.active.is_(True),
                CatalogDraft.published_revision_id == revision_id,
            )
        )
    ).all()
    return {
        row.field_name: {
            "value": _jsonable(row.value),
            "reason": row.reason,
            "updated_by": str(row.created_by) if row.created_by else None,
            "updated_at": _iso(row.created_at),
        }
        for row in rows
    }


async def _draft_for_course(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    course: CatalogCourse,
    *,
    base_revision_id: UUID | None = None,
    created_by: UUID | None = None,
    initial_data: dict[str, Any] | None = None,
) -> CatalogDraft:
    # Course identity is the serialization point for source observations and
    # the first draft creation.  Without this lock two workers can both miss
    # the partial unique index and then race while incrementing JSON draft
    # revisions (or lose one source component entirely).
    locked_course = await db.scalar(
        select(CatalogCourse)
        .where(CatalogCourse.id == course.id, CatalogCourse.organization_id == organization_id)
        .with_for_update()
    )
    if locked_course is not None:
        course = locked_course
    draft = await db.scalar(
        select(CatalogDraft).where(
            CatalogDraft.organization_id == organization_id,
            CatalogDraft.term_id == term.id,
            CatalogDraft.course_id == course.id,
            CatalogDraft.state == "draft",
        ).with_for_update()
    )
    if draft is not None:
        return draft
    base: dict[str, Any] = {}
    inherited_overrides: dict[str, Any] = {}
    if base_revision_id is not None:
        revision = await db.scalar(
            select(CatalogCourseRevision).where(
                CatalogCourseRevision.id == base_revision_id,
                CatalogCourseRevision.organization_id == organization_id,
                CatalogCourseRevision.course_id == course.id,
                CatalogCourseRevision.term_id == term.id,
            )
        )
        if revision is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Base course revision not found")
        base = await _revision_data(db, revision)
        inherited_overrides = await _overrides_for_revision(
            db, organization_id, term.id, course.id, revision.id
        )
    elif initial_data is not None:
        base = _jsonable(initial_data)
        active_revision = await _active_revision_for_course(db, organization_id, term.id, course.id)
        if active_revision is not None:
            base_revision_id = active_revision.id
    else:
        # A new source observation after publication starts from the last
        # published value.  Rehydrate the active overrides too, otherwise a
        # refresh would silently discard a correction made in the Courses tab.
        # Rebase a refresh on the revision in the active release.  The highest
        # historical revision may belong to a release that was rolled back;
        # using it here would silently resurrect content an administrator had
        # deliberately removed from the live pointer.
        revision = await _active_revision_for_course(db, organization_id, term.id, course.id)
        if revision is None:
            revision = await db.scalar(
                select(CatalogCourseRevision)
                .where(
                    CatalogCourseRevision.organization_id == organization_id,
                    CatalogCourseRevision.course_id == course.id,
                    CatalogCourseRevision.term_id == term.id,
                    CatalogCourseRevision.state == "published",
                )
                .order_by(CatalogCourseRevision.revision.desc())
                .limit(1)
            )
        if revision is not None:
            base = await _revision_data(db, revision)
            # Keep the optimistic base pointer alongside the inherited JSON.
            # Publication checks it against the active release before creating
            # a new immutable revision.
            base_revision_id = revision.id
            inherited_overrides = await _overrides_for_revision(
                db, organization_id, term.id, course.id, revision.id
            )
    draft = CatalogDraft(
        organization_id=organization_id,
        term_id=term.id,
        course_id=course.id,
        base_revision_id=base_revision_id,
        revision=1,
        state="draft",
        data=base,
        issues=[],
        field_overrides=inherited_overrides,
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(draft)
    await db.flush()
    return draft


async def _merge_observation_into_draft(
    draft: CatalogDraft,
    candidate: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    component: str,
    status_value: str,
    observed_at: datetime,
    source_fetched_at: datetime | None,
) -> None:
    current = dict(draft.data or {})
    overrides = dict(draft.field_overrides or {})
    incoming = dict(candidate.get("data") or candidate)
    successful = status_value in {"success", "empty"}
    if component == "constraints" and "sections" not in incoming:
        section_code = incoming.pop("section_code", None)
        restrictions = incoming.pop("restrictions", None)
        if section_code is not None and restrictions is not None:
            incoming["sections"] = [
                {
                    "section_code": str(section_code),
                    "restrictions": _jsonable(restrictions),
                    "restrictions_status": "verified" if successful else "unknown",
                    "restrictions_observed_at": _iso(observed_at),
                    "restrictions_source_fetched_at": _iso(source_fetched_at),
                }
            ]
    elif component == "constraints" and isinstance(incoming.get("sections"), list):
        # Keep evidence attached to each section. A successful response for
        # section 1 must never certify an unobserved section 2.
        for section in incoming["sections"]:
            if not isinstance(section, dict):
                continue
            section.setdefault(
                "restrictions_status",
                "verified" if successful else "unknown",
            )
            section.setdefault("restrictions_observed_at", _iso(observed_at))
            section.setdefault("restrictions_source_fetched_at", _iso(source_fetched_at))
    elif component == "details" and isinstance(incoming.get("sections"), list):
        # Course Info includes the section roster and meeting rows.  Record
        # that section data was actually observed, while leaving restrictions
        # unknown unless this same response explicitly contained a
        # restriction table for that section.
        for section in incoming["sections"]:
            if not isinstance(section, dict):
                continue
            if section.get("restrictions_present"):
                section.setdefault("restrictions_status", "verified" if successful else "unknown")
                section.setdefault("restrictions_observed_at", _iso(observed_at))
                section.setdefault("restrictions_source_fetched_at", _iso(source_fetched_at))
    current = _merge_dicts(current, incoming, overrides, replace_sections=component == "details" and successful)
    conflict_issues = list(issues)
    for field, source_value in incoming.items():
        override = overrides.get(field)
        if not override or override.get("value") == _jsonable(source_value):
            continue
        conflict_issues.append(
            {
                "component": component,
                "code": "source_conflict",
                "field": field,
                "source_value": _jsonable(source_value),
                "override_value": _jsonable(override.get("value")),
                "message": f"Source value for {field} differs from the active administrator override",
                "severity": "warning",
            }
        )
    component_status = dict(current.get("component_status") or {})
    # A failed/empty response records its status and issues while leaving the
    # last known fields intact.  A successful empty restrictions table is a
    # legitimate verified empty table and is therefore written explicitly.
    component_status[component] = _component_meta(
        component,
        status_value=status_value,
        observed_at=observed_at,
        source_fetched_at=source_fetched_at,
        verified=successful and not any(key in overrides for key in incoming),
    )
    if component == "details" and "sections" in incoming:
        component_status["sections"] = _component_meta(
            "sections",
            status_value=status_value,
            observed_at=observed_at,
            source_fetched_at=source_fetched_at,
            verified=successful and "sections" not in overrides,
        )
        if any(isinstance(item, dict) and item.get("restrictions_present") for item in incoming["sections"]):
            component_status["constraints"] = _component_meta(
                "constraints",
                status_value=status_value,
                observed_at=observed_at,
                source_fetched_at=source_fetched_at,
                verified=successful and "sections" not in overrides,
            )
    current["component_status"] = component_status
    current["_source_observation_ids"] = list(current.get("_source_observation_ids") or [])
    draft.data = _jsonable(current)
    existing_issues = [item for item in (draft.issues or []) if isinstance(item, dict)]
    # A component that has just been read cleanly is not failing any more, so
    # its earlier errors go with the reading that replaced them. Without this
    # the list only ever grew, and a single bad read blocked a course from
    # publication for good: publish refuses any draft carrying a blocking
    # issue, so re-reading the page successfully changed nothing.
    #
    # Measured: 513 courses stuck behind a `missing_prerequisites_table` from a
    # parser that misread METU's `position` field. Re-reading them restored the
    # prerequisite groups - MATH 219 went from none to four - and every one of
    # them stayed blocked, because the stale error was still attached.
    #
    # Scoped to this component and to errors: another component's failure is
    # not this reading's to clear, and warnings (a source value differing from
    # an administrator's override) are review notes that outlive one read.
    if not any(issue.get("severity", "error") == "error" for issue in conflict_issues):
        existing_issues = [
            item
            for item in existing_issues
            if item.get("component") != component or item.get("severity", "error") != "error"
        ]
    # Keep source failures for review.  Identical repeated observations are
    # deduplicated to keep a repeatedly retried job from growing JSONB forever.
    for issue in conflict_issues:
        if issue not in existing_issues:
            existing_issues.append(issue)
    draft.issues = existing_issues[-200:]
    draft.revision = int(draft.revision or 1) + 1


async def ingest_observation(
    db: AsyncSession,
    organization_id: UUID,
    tool: str,
    values: dict[str, Any],
    payload: Any,
    observed_at: datetime,
    source_fetched_at: datetime | None = None,
    job_id: UUID | None = None,
) -> dict[str, Any]:
    """Persist one source observation and update only draft candidates.

    ``source_fetched_at`` is deliberately explicit.  Backfilled cache data
    passes ``None`` and therefore cannot renew freshness or make a planner
    believe that a network read happened now.
    """

    if tool not in CATALOG_TOOLS:
        raise ValueError(f"Unsupported catalog source tool: {tool}")
    observed_at = _utc(observed_at) or datetime.now(UTC)
    source_fetched_at = _utc(source_fetched_at)
    values = _jsonable(values or {})
    payload = _jsonable(payload)
    term_code, department, requested_course, section_code = _scope(values)
    candidate, issues, status_value = _parse_observation(tool, values, payload)
    component = _tool_component(tool)
    observation = CatalogSourceObservation(
        organization_id=organization_id,
        job_id=job_id,
        tool=tool,
        arguments=values,
        term=term_code,
        department=department,
        course_code=requested_course,
        section_code=section_code,
        payload=payload,
        payload_hash=_digest(payload),
        observed_at=observed_at,
        source_fetched_at=source_fetched_at,
        status=status_value,
        issues=issues,
        candidate_data=candidate,
    )
    db.add(observation)
    await db.flush()

    candidates: list[tuple[str, dict[str, Any]]] = []
    raw_courses = candidate.get("courses") if isinstance(candidate, dict) else None
    if isinstance(raw_courses, list):
        for item in raw_courses:
            if isinstance(item, dict):
                code = _course_code(item, requested_course)
                if code:
                    data = dict(item.get("data") or {})
                    candidates.append((code, data))
    if not candidates and requested_course is not None:
        data = dict(candidate)
        # The list of course candidates is an index for this observation, not a
        # field to copy into one course's detail row.
        data.pop("courses", None)
        candidates.append((requested_course, data))

    draft_ids: list[str] = []
    for code, data in candidates:
        if isinstance(data.get("sections"), list):
            from app.academic_catalog.instructors import enrich_instructors

            data["sections"] = await enrich_instructors(db, organization_id, data["sections"])
        course = await _ensure_course(db, organization_id, code, department=department or str(code[:3]))
        if term_code is None:
            continue
        term = await _ensure_term(db, organization_id, term_code)
        draft = await _draft_for_course(db, organization_id, term, course)
        await _merge_observation_into_draft(
            draft,
            {"data": data},
            issues,
            component=component,
            status_value=status_value,
            observed_at=observed_at,
            source_fetched_at=source_fetched_at,
        )
        source_ids = list(draft.data.get("_source_observation_ids") or [])
        if str(observation.id) not in source_ids:
            source_ids.append(str(observation.id))
        data_copy = dict(draft.data)
        data_copy["_source_observation_ids"] = source_ids[-100:]
        draft.data = data_copy
        draft_ids.append(str(draft.id))

    # A detail/constraint observation can arrive before its listing.  The
    # requested code path above creates the course and term regardless, so a
    # later listing can merge into the same draft without inventing a section.
    await db.flush()
    return {
        "observation_id": str(observation.id),
        "draft_ids": draft_ids,
        "draft_id": draft_ids[0] if len(draft_ids) == 1 else None,
        "candidate_data": candidate,
        "issues": issues,
        "status": status_value,
        "course_codes": [code for code, _ in candidates],
    }


async def _revision_data(db: AsyncSession, revision: CatalogCourseRevision) -> dict[str, Any]:
    course = await db.get(CatalogCourse, revision.course_id)
    data: dict[str, Any] = {
        "course_code": course.course_code if course else None,
        "department": course.department if course else None,
        "title": revision.title,
        "local_credits": float(revision.local_credits) if revision.local_credits is not None else None,
        "ects": float(revision.ects) if revision.ects is not None else None,
        "level": revision.level,
        "availability": revision.availability,
        "campus": revision.campus,
        "is_thesis": bool(revision.is_thesis),
        "completeness": _jsonable(revision.completeness or {}),
        "component_status": _jsonable(revision.component_status or {}),
        "sections": [],
        "prerequisite_groups": [],
        "replacements": [],
    }
    # Load the complete immutable revision graph in bounded queries.  The
    # previous implementation queried meetings, instructors and restrictions
    # once per section and requirements once per prerequisite group, turning a
    # course detail page into an N+1 query fan-out.
    children = await _bulk_revision_components(db, revision.organization_id, [revision])
    sections = children["sections"].get(revision.id, [])
    for section in sections:
        meeting_rows = children["meetings"].get(section.id, [])
        instructors = children["instructors"].get(section.id, [])
        restrictions = children["restrictions"].get(section.id, [])
        section_payload = {
                "section_code": section.section_code,
                "section": section.section_code,
                "status": section.status,
                "notes": section.notes,
                "syllabus_url": section.syllabus_url,
                "syllabus_available": section.syllabus_available,
                "meetings_status": section.meetings_status,
                "restrictions_status": section.restrictions_status,
                "restrictions_observed_at": _iso(section.restrictions_observed_at),
                "restrictions_source_fetched_at": _iso(section.restrictions_source_fetched_at),
                "meetings": [
                    {
                        "weekday": meeting.weekday,
                        "start_minute": meeting.start_minute,
                        "end_minute": meeting.end_minute,
                        "room": meeting.room,
                        "status": meeting.status,
                        "raw_label": meeting.raw_label,
                    }
                    for meeting in meeting_rows
                ],
                "instructors": [
                    {
                        "source_name": link.source_name,
                        "position": instructor.position,
                        # Read the immutable section snapshot rather than the
                        # mutable organization-level instructor directory.
                        "researcher_id": link.researcher_id,
                        "match_method": link.match_method,
                        "resolution_status": link.resolution_status,
                    }
                    for link, instructor in instructors
                ],
                "restrictions": [_restriction_dict(row) for row in restrictions],
            }
        # Keep the canonical field while exposing the aliases consumed by the
        # existing Course Info and planner adapters.
        section_payload["schedule"] = list(section_payload["meetings"])
        section_payload["meeting_status"] = section.meetings_status
        data["sections"].append(section_payload)
    groups = (
        await db.scalars(
            select(CatalogPrerequisiteGroup)
            .where(
                CatalogPrerequisiteGroup.organization_id == revision.organization_id,
                CatalogPrerequisiteGroup.course_revision_id == revision.id,
            )
            .order_by(CatalogPrerequisiteGroup.group_no, CatalogPrerequisiteGroup.id)
        )
    ).all()
    for group in groups:
        requirements = children["requirements"].get(group.id, [])
        data["prerequisite_groups"].append(
            {
                "group_no": group.group_no,
                "logic": group.logic,
                "program_code": group.program_code,
                "curriculum_version": group.curriculum_version,
                "applicability": _jsonable(group.applicability or {}),
                "verified": group.verified,
                "raw_text": group.raw_text,
                "requirements": [
                    {
                        "course_code": item.course_code,
                        "minimum_grade": item.minimum_grade,
                        "requirement_type": item.requirement_type,
                        "position": item.position,
                        "raw_text": item.raw_text,
                    }
                    for item in requirements
                ],
            }
        )
    replacements = children["replacements"].get(revision.id, [])
    data["replacements"] = [
        {
            "relationship_type": row.relationship_type,
            "related_course_code": row.related_course_code,
            "program_code": row.program_code,
            "curriculum_version": row.curriculum_version,
            "verified": row.verified,
            "raw_text": row.raw_text,
        }
        for row in replacements
    ]
    return _jsonable(data)


def _restriction_dict(row: CatalogRestriction) -> dict[str, Any]:
    return {
        "restriction_group": row.restriction_group,
        "row_index": row.row_index,
        "kind": row.kind,
        "value_text": row.value_text,
        "value_numeric": float(row.value_numeric) if row.value_numeric is not None else None,
        "operator": row.operator,
        "minimum_grade": row.minimum_grade,
        "given_department": row.given_department,
        "start_char": row.start_char,
        "end_char": row.end_char,
        "min_cgpa": float(row.min_cgpa) if row.min_cgpa is not None else None,
        "max_cgpa": float(row.max_cgpa) if row.max_cgpa is not None else None,
        "min_year": row.min_year,
        "max_year": row.max_year,
        "start_grade": row.start_grade,
        "end_grade": row.end_grade,
        "prior_course_code": row.prior_course_code,
        "program_code": row.program_code,
        "curriculum_version": row.curriculum_version,
        "raw_text": row.raw_text,
        "verified": row.verified,
        "status": row.status,
    }


def _component_status(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("component_status")
    if not isinstance(value, dict):
        value = {}
    result = {str(key): _jsonable(item) for key, item in value.items() if isinstance(item, dict)}
    # A manually supplied field in a draft is explicit data, but remains
    # unknown until an administrator verifies it through the UI patch path.
    for component, fields in {
        "details": ("title", "local_credits", "ects", "level", "availability", "campus"),
        "sections": ("sections",),
        "constraints": ("sections",),
        "prerequisites": ("prerequisite_groups",),
        "replacements": ("replacements",),
        "listing": ("course_code",),
    }.items():
        if component not in result and any(field in data and data.get(field) not in (None, [], {}) for field in fields):
            result[component] = {
                "component": component,
                "source_status": "admin",
                "verified": False,
                "fresh": False,
                "observed_at": None,
                "source_fetched_at": None,
            }
    return result


async def _latest_source_observation(
    db: AsyncSession, organization_id: UUID, data: dict[str, Any]
) -> CatalogSourceObservation | None:
    observations = [
        row
        for row in await _source_observations(db, organization_id, data)
        if row.status in {"success", "empty"}
    ]
    return observations[0] if observations else None


async def _source_observations(
    db: AsyncSession, organization_id: UUID, data: dict[str, Any]
) -> list[CatalogSourceObservation]:
    ids = data.get("_source_observation_ids")
    if not isinstance(ids, list):
        return []
    valid: list[UUID] = []
    for value in reversed(ids):
        try:
            valid.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not valid:
        return []
    return list((await db.scalars(
        select(CatalogSourceObservation)
        .where(
            CatalogSourceObservation.organization_id == organization_id,
            CatalogSourceObservation.id.in_(valid),
        )
        .order_by(CatalogSourceObservation.created_at.desc(), CatalogSourceObservation.id.desc())
    )).all())


def _valid_researcher_id(value: Any) -> int | None:
    parsed = _int(value)
    return parsed if parsed is not None and parsed > 0 else None


async def _instructor_for(
    db: AsyncSession,
    organization_id: UUID,
    source_name: str,
    position: str | None,
    researcher_id: int | None,
    resolution_status: str | None = None,
) -> CatalogInstructor:
    if researcher_id is not None:
        from app.admin.directory import METU_ID
        from app.researchers.models import Researcher

        if organization_id != METU_ID or await db.get(Researcher, researcher_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Researcher link is unavailable for this organization")
    normalized = _key(source_name)
    stmt = select(CatalogInstructor).where(
        CatalogInstructor.organization_id == organization_id,
        CatalogInstructor.normalized_name == normalized,
    )
    if position is None:
        stmt = stmt.where(CatalogInstructor.position.is_(None))
    else:
        stmt = stmt.where(CatalogInstructor.position == position)
    row = await db.scalar(stmt)
    if row is None:
        row = CatalogInstructor(
            organization_id=organization_id,
            source_name=source_name,
            normalized_name=normalized,
            position=position,
            researcher_id=researcher_id,
            resolution_status=resolution_status or ("unassigned" if _key(source_name) == "staff" else "unresolved"),
        )
        db.add(row)
        await db.flush()
    elif researcher_id is not None and row.researcher_id is None:
        row.researcher_id = researcher_id
        row.resolution_status = resolution_status or "matched"
    return row


def _course_data_value(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    return None if value in (None, "") else value


async def _materialize_revision(
    db: AsyncSession,
    draft: CatalogDraft,
    *,
    created_by: UUID | None,
) -> CatalogCourseRevision:
    """Turn a validated draft payload into an immutable published revision."""

    data = _jsonable(dict(draft.data or {}))
    course = await db.get(CatalogCourse, draft.course_id)
    if course is None or course.organization_id != draft.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft course not found")
    observations = await _source_observations(db, draft.organization_id, data)
    source_observations = [
        row for row in observations if row.status in {"success", "empty"}
    ]
    observation = source_observations[0] if source_observations else None

    def source_for(
        tools: tuple[str, ...],
        *,
        section_code: str | None = None,
    ) -> CatalogSourceObservation | None:
        candidates = [row for row in source_observations if row.tool in tools]
        if section_code is not None:
            exact = [
                row for row in candidates
                if str(row.section_code or "").strip() == section_code
            ]
            if exact:
                return exact[0]
            # Course-info responses are course-scoped and may contain the
            # complete section roster. They are safe fallbacks for the
            # section identity, while a differently scoped constraints call
            # must never be attached to an unrelated section. Listing rows
            # carry course metadata only, so they are not section evidence.
            if not set(tools).issubset({"get_course_info"}):
                return None
            candidates = [row for row in candidates if row.section_code in (None, "")]
        return candidates[0] if candidates else None
    latest = await db.scalar(
        select(func.coalesce(func.max(CatalogCourseRevision.revision), 0)).where(
            CatalogCourseRevision.organization_id == draft.organization_id,
            CatalogCourseRevision.course_id == draft.course_id,
            CatalogCourseRevision.term_id == draft.term_id,
        )
    )
    component_status = _component_status(data)
    completeness = dict(data.get("completeness") or {})
    def verified_component(name: str) -> bool:
        value = component_status.get(name)
        return bool(isinstance(value, dict) and value.get("verified") is True)

    # Completeness describes observed, typed source components.  Course code
    # identity alone must not make an unlisted detail-only draft look complete
    # (and an empty restrictions/prerequisites table is complete only when its
    # corresponding source call explicitly verified it).
    completeness.setdefault("listing", verified_component("listing"))
    completeness.setdefault("details", bool(data.get("title")) and verified_component("details"))
    completeness.setdefault("sections", "sections" in data and verified_component("sections"))
    completeness.setdefault(
        "constraints",
        "sections" in data
        and verified_component("constraints")
        and all("restrictions" in section for section in data.get("sections", []) if isinstance(section, dict)),
    )
    completeness.setdefault("prerequisites", "prerequisite_groups" in data and verified_component("prerequisites"))
    completeness.setdefault("replacements", "replacements" in data and verified_component("replacements"))
    revision = CatalogCourseRevision(
        organization_id=draft.organization_id,
        course_id=draft.course_id,
        term_id=draft.term_id,
        revision=int(latest or 0) + 1,
        # A database trigger allows the one-way draft -> published transition
        # after all child rows have been attached.
        state="draft",
        title=_course_data_value(data, "title"),
        local_credits=_number(data.get("local_credits")),
        ects=_number(data.get("ects")),
        level=_course_data_value(data, "level"),
        availability=_course_data_value(data, "availability"),
        campus=_course_data_value(data, "campus"),
        is_thesis=bool(data.get("is_thesis") or data.get("thesis")),
        completeness=_jsonable(completeness),
        component_status=_jsonable(component_status),
        issues=_jsonable(draft.issues or []),
        source_observation_id=observation.id if observation else None,
        observed_at=observation.observed_at if observation else None,
        source_fetched_at=observation.source_fetched_at if observation else None,
        created_by=created_by,
    )
    db.add(revision)
    await db.flush()

    sections = data.get("sections")
    if not isinstance(sections, list):
        sections = []
    for index, raw_section in enumerate(sections):
        if not isinstance(raw_section, dict):
            continue
        section_code = str(raw_section.get("section_code") or raw_section.get("section") or index + 1).strip()
        section_observation = source_for(("get_course_info",), section_code=section_code)
        constraints_observation = source_for(("get_section_constraints",), section_code=section_code)
        parsed_meetings, meeting_field_present = _meeting_rows(raw_section)
        section = CatalogSection(
            organization_id=draft.organization_id,
            course_revision_id=revision.id,
            section_code=section_code,
            status=str(raw_section.get("status") or "listed"),
            notes=raw_section.get("notes"),
            syllabus_url=raw_section.get("syllabus_url"),
            syllabus_available=_boolish(raw_section.get("syllabus_available")),
            meetings_status=_section_meetings_status(raw_section, parsed_meetings, meeting_field_present),
            restrictions_status=str(raw_section.get("restrictions_status") or "unknown"),
            restrictions_observed_at=_parse_datetime_value(raw_section.get("restrictions_observed_at")),
            restrictions_source_fetched_at=_parse_datetime_value(
                raw_section.get("restrictions_source_fetched_at")
            ),
            # Section fields come from the course-info response.  A
            # section-scoped constraints observation is reserved for the
            # restriction rows below and must not be attributed to meetings.
            source_observation_id=section_observation.id if section_observation is not None else None,
        )
        db.add(section)
        await db.flush()
        if meeting_field_present:
            for raw_meeting in parsed_meetings:
                meeting_status = str(raw_meeting.get("status") or "unknown")
                weekday = _int(raw_meeting.get("weekday"))
                start = _int(raw_meeting.get("start_minute"))
                end = _int(raw_meeting.get("end_minute"))
                if meeting_status == "scheduled" and (weekday is None or start is None or end is None or end <= start):
                    meeting_status = "invalid"
                db.add(
                    CatalogMeeting(
                        organization_id=draft.organization_id,
                        section_id=section.id,
                        weekday=weekday,
                        start_minute=start,
                        end_minute=end,
                        room=raw_meeting.get("room"),
                        status=meeting_status,
                        raw_label=raw_meeting.get("raw_label"),
                    )
                )
        instructors = raw_section.get("instructors")
        if isinstance(instructors, list):
            for raw_instructor in instructors:
                if isinstance(raw_instructor, str):
                    raw_instructor = {"source_name": raw_instructor}
                if not isinstance(raw_instructor, dict):
                    continue
                source_name = str(raw_instructor.get("source_name") or raw_instructor.get("name") or "").strip()
                if not source_name:
                    continue
                instructor = await _instructor_for(
                    db,
                    draft.organization_id,
                    source_name,
                    str(raw_instructor.get("position")).strip() if raw_instructor.get("position") else None,
                    _valid_researcher_id(raw_instructor.get("researcher_id")),
                    str(raw_instructor.get("resolution_status") or "").strip() or None,
                )
                db.add(
                    CatalogSectionInstructor(
                        organization_id=draft.organization_id,
                        section_id=section.id,
                        instructor_id=instructor.id,
                        researcher_id=instructor.researcher_id,
                        source_name=source_name,
                        match_method=str(raw_instructor.get("match_method") or "unmatched"),
                        resolution_status=str(raw_instructor.get("resolution_status") or instructor.resolution_status),
                    )
                )
        restrictions = raw_section.get("restrictions")
        if isinstance(restrictions, list):
            for row_index, raw_restriction in enumerate(restrictions):
                if not isinstance(raw_restriction, dict):
                    continue
                db.add(
                    CatalogRestriction(
                        organization_id=draft.organization_id,
                        course_revision_id=revision.id,
                        section_id=section.id,
                        restriction_group=raw_restriction.get("restriction_group") or raw_restriction.get("group"),
                        row_index=_int(raw_restriction.get("row_index")) or row_index,
                        kind=str(raw_restriction.get("kind") or "eligibility"),
                        value_text=raw_restriction.get("value_text"),
                        value_numeric=_number(raw_restriction.get("value_numeric")),
                        operator=raw_restriction.get("operator"),
                        minimum_grade=raw_restriction.get("minimum_grade"),
                        given_department=raw_restriction.get("given_department") or raw_restriction.get("given_dept"),
                        start_char=raw_restriction.get("start_char"),
                        end_char=raw_restriction.get("end_char"),
                        min_cgpa=_number(raw_restriction.get("min_cgpa")),
                        max_cgpa=_number(raw_restriction.get("max_cgpa")),
                        min_year=_int(raw_restriction.get("min_year")),
                        max_year=_int(raw_restriction.get("max_year")),
                        start_grade=raw_restriction.get("start_grade"),
                        end_grade=raw_restriction.get("end_grade"),
                        prior_course_code=raw_restriction.get("prior_course_code"),
                        program_code=raw_restriction.get("program_code"),
                        curriculum_version=raw_restriction.get("curriculum_version"),
                        raw_text=raw_restriction.get("raw_text"),
                        verified=bool(raw_restriction.get("verified", False)),
                        status=str(raw_restriction.get("status") or "unknown"),
                        source_observation_id=constraints_observation.id if constraints_observation else None,
                    )
                )
    groups = data.get("prerequisite_groups")
    if isinstance(groups, list):
        prerequisite_observation = source_for(("get_course_prerequisites",))
        for raw_group in groups:
            if not isinstance(raw_group, dict):
                continue
            group = CatalogPrerequisiteGroup(
                organization_id=draft.organization_id,
                course_revision_id=revision.id,
                group_no=_int(raw_group.get("group_no")) or 1,
                logic=str(raw_group.get("logic") or "AND").upper(),
                program_code=raw_group.get("program_code"),
                curriculum_version=raw_group.get("curriculum_version"),
                applicability=raw_group.get("applicability") or {},
                verified=bool(raw_group.get("verified", False)),
                raw_text=raw_group.get("raw_text"),
                source_observation_id=prerequisite_observation.id if prerequisite_observation else None,
            )
            db.add(group)
            await db.flush()
            requirements = raw_group.get("requirements")
            if isinstance(requirements, list):
                for position, raw_requirement in enumerate(requirements):
                    if not isinstance(raw_requirement, dict):
                        continue
                    code = _course_code(raw_requirement, None)
                    if code is None:
                        continue
                    db.add(
                        CatalogPrerequisiteRequirement(
                            organization_id=draft.organization_id,
                            group_id=group.id,
                            course_code=code,
                            minimum_grade=raw_requirement.get("minimum_grade"),
                            requirement_type=str(raw_requirement.get("requirement_type") or "course"),
                            position=_int(raw_requirement.get("position")) or position,
                            raw_text=raw_requirement.get("raw_text"),
                        )
                    )
    replacements = data.get("replacements")
    if isinstance(replacements, list):
        replacement_observation = source_for(("get_course_replacements",))
        for raw_replacement in replacements:
            if not isinstance(raw_replacement, dict):
                continue
            code = _course_code(raw_replacement, None) or _course_code({"course_code": raw_replacement.get("related_course_code")}, None)
            if code is None:
                continue
            db.add(
                CatalogCourseReplacement(
                    organization_id=draft.organization_id,
                    course_revision_id=revision.id,
                    relationship_type=str(raw_replacement.get("relationship_type") or "replacement"),
                    related_course_code=code,
                    program_code=raw_replacement.get("program_code"),
                    curriculum_version=raw_replacement.get("curriculum_version"),
                    verified=bool(raw_replacement.get("verified", False)),
                    raw_text=raw_replacement.get("raw_text"),
                    source_observation_id=replacement_observation.id if replacement_observation else None,
                )
            )
    await db.flush()
    revision.state = "published"
    await db.flush()
    return revision


def import_dedup_key(
    organization_id: UUID,
    term: str,
    department: str | None = None,
    course_codes: Iterable[str] | None = None,
) -> str:
    codes = sorted({normalize_course_code(code) for code in (course_codes or [])})
    return _digest(
        {
            "organization_id": str(organization_id),
            "term": normalize_term(term),
            "department": str(department or "").strip().upper() or None,
            "course_codes": codes,
        }
    )


# Only pages whose data stands alone are reusable.  Course detail steps are
# deliberately absent: their responses are what discovers the section steps, so
# skipping a detail would hide a section whose own observation is missing or
# failed until the detail observation ages out.
_LEAF_REUSE_TOOLS = (
    "get_section_constraints",
    "get_course_prerequisites",
    "get_course_replacements",
)


def leaf_reuse_ttl_seconds(tool: str) -> int:
    """How long a successful observation may satisfy one reusable leaf step.

    The window is clamped below the component's read-time max age: reusing a
    page past that age would keep a component permanently stale, because the
    step that would refresh it would keep being skipped.
    """
    from app.config import get_settings

    settings = get_settings()
    if tool in {"get_course_prerequisites", "get_course_replacements"}:
        ttl = int(settings.catalog_reuse_rules_seconds)
    else:
        ttl = int(settings.catalog_reuse_info_seconds)
    max_age = _COMPONENT_MAX_AGE_SECONDS.get(_tool_component(tool))
    if max_age is not None:
        ttl = min(ttl, max(0, max_age - 60))
    return max(0, ttl)


def leaf_step_key(step: dict[str, Any]) -> tuple[str, str, str | None]:
    """The observation identity that can satisfy a leaf step."""
    values = step.get("values") or {}
    section = None
    if step.get("tool") == "get_section_constraints":
        section = str(values.get("section") or "").strip()
    return (str(step.get("tool") or ""), str(values.get("course") or "").strip(), section)


def drop_reused_steps(
    steps: Iterable[dict[str, Any]], reused: set[tuple[str, str, str | None]]
) -> list[dict[str, Any]]:
    """Remove leaf steps whose source data is still within its reuse window."""
    return [step for step in steps if leaf_step_key(step) not in reused]


async def drop_fresh_leaf_steps(
    db: AsyncSession,
    organization_id: UUID,
    term: str | None,
    steps: Iterable[dict[str, Any]],
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Drop leaf steps whose latest successful observation is still fresh.

    Listing and course detail steps always run: they are how the plan grows and
    how its section steps are discovered.  A reused observation must carry
    network-fetched evidence - backfilled cache rows have no
    ``source_fetched_at`` and never satisfy a step.  A forced refresh returns
    every step.
    """
    kept = list(steps)
    if force or not kept:
        return kept
    term_code = normalize_term(term) if term else None
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for step in kept:
        tool = str(step.get("tool") or "")
        if tool in _LEAF_REUSE_TOOLS:
            by_tool.setdefault(tool, []).append(step)
    if not by_tool:
        return kept
    now = datetime.now(UTC)
    registration = _registration_window(now)
    window_start = _registration_boundary(registration.get("start")) if registration.get("active") else None
    reused: set[tuple[str, str, str | None]] = set()
    for tool, tool_steps in by_tool.items():
        ttl = leaf_reuse_ttl_seconds(tool)
        if ttl <= 0:
            continue
        codes = sorted({key[1] for key in map(leaf_step_key, tool_steps) if key[1]})
        if not codes:
            continue
        filters = [
            CatalogSourceObservation.organization_id == organization_id,
            CatalogSourceObservation.tool == tool,
            CatalogSourceObservation.status.in_(("success", "empty")),
            CatalogSourceObservation.source_fetched_at.is_not(None),
            CatalogSourceObservation.source_fetched_at >= now - timedelta(seconds=ttl),
            # A future fetch is stale to readers too (clock skew, bad evidence).
            CatalogSourceObservation.source_fetched_at <= now,
            CatalogSourceObservation.course_code.in_(codes),
        ]
        if window_start is not None:
            # An active add-drop window invalidates anything fetched before it,
            # however recent: readers mark those components stale, so the import
            # must be able to refresh them without a forced job.
            filters.append(CatalogSourceObservation.source_fetched_at >= window_start)
        if term_code:
            filters.append(CatalogSourceObservation.term == term_code)
        if tool == "get_section_constraints":
            result = await db.execute(
                select(CatalogSourceObservation.course_code, CatalogSourceObservation.section_code)
                .where(*filters, CatalogSourceObservation.section_code.is_not(None))
                .group_by(CatalogSourceObservation.course_code, CatalogSourceObservation.section_code)
            )
            reused |= {(tool, str(code or "").strip(), str(section or "").strip()) for code, section in result}
        else:
            result = await db.execute(
                select(CatalogSourceObservation.course_code)
                .where(*filters)
                .group_by(CatalogSourceObservation.course_code)
            )
            reused |= {(tool, str(row[0] or "").strip(), None) for row in result}
    return drop_reused_steps(kept, reused)


async def enqueue_import(
    db: AsyncSession,
    organization_id: UUID,
    term: str,
    department: str | None = None,
    course_codes: list[str] | None = None,
    reason: str | None = None,
    requested_by: UUID | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 50,
) -> CatalogImportJob:
    """Create or reuse one active import request for a scope."""

    term = normalize_term(term)
    department = str(department).strip() if department else None
    codes = [normalize_course_code(code) for code in (course_codes or [])]
    if any(not _SEVEN_DIGIT.fullmatch(code) for code in codes):
        raise ValueError("Import course codes require full seven-digit METU codes")
    force_refresh = bool((payload or {}).get("force_refresh"))
    dedup_key = import_dedup_key(organization_id, term, department, codes)
    if force_refresh:
        # A forced refresh must not adopt a scheduled job whose plan may already
        # have skipped its fresh pages, so it is its own scope.
        dedup_key = _digest({"scope": dedup_key, "force_refresh": True})
    if not department and not codes:
        # Directory-only maintenance and a full university import have
        # different work to do and must never reuse each other's job.
        dedup_key = _digest({
            "scope": dedup_key,
            "discovery_only": bool((payload or {}).get("discovery_only")),
        })
    def reuse(job: CatalogImportJob) -> CatalogImportJob:
        # An explicit admin request may reuse an overnight refresh. Promote
        # that job without duplicating its source work or resetting progress.
        if requested_by is not None and not (payload or {}).get("scheduled"):
            job.requested_by = requested_by
            job.payload = {**(job.payload or {}), "scheduled": False}
        return job
    existing = await db.scalar(
        select(CatalogImportJob).where(
            CatalogImportJob.organization_id == organization_id,
            CatalogImportJob.dedup_key == dedup_key,
            CatalogImportJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return reuse(existing)
    # A failed import that got somewhere is resumed, not started again.
    #
    # Only queued and running jobs were ever reused, so a job that failed took
    # its progress with it: the checkpoint was still there, recording exactly
    # which step it reached, and nothing could reach it. Measured on this
    # deployment - a whole-term import stopped at 4817 of 10048 steps after
    # about twenty-four hours, and the only way forward was a fresh job that
    # would re-walk all 10048, costing another day and ten thousand requests
    # METU has already answered.
    #
    # Guarded on real progress: a job that failed at the very first step has
    # nothing to resume and every reason to be built again from scratch, which
    # also keeps a permanently broken scope from being retried for ever off its
    # own checkpoint.
    resumable = await db.scalar(
        select(CatalogImportJob).where(
            CatalogImportJob.organization_id == organization_id,
            CatalogImportJob.dedup_key == dedup_key,
            CatalogImportJob.status == "failed",
            CatalogImportJob.checkpoint_offset > 0,
        ).order_by(CatalogImportJob.updated_at.desc())
    )
    if resumable is not None:
        resumable.status = "queued"
        # The attempt counter belongs to the run that failed, not to the work.
        resumable.attempts = 0
        resumable.lease_until = None
        resumable.error_code = None
        resumable.error_detail = None
        return reuse(resumable)
    def new_job() -> CatalogImportJob:
        return CatalogImportJob(
            organization_id=organization_id,
            term=term,
            department=department,
            course_codes=codes or None,
            payload=_jsonable(payload or {}),
            status="queued",
            checkpoint={},
            attempts=0,
            lease_until=None,
            dedup_key=dedup_key,
            reason=reason,
            requested_by=requested_by,
            priority=priority,
        )

    def is_active_dedup_conflict(exc: IntegrityError) -> bool:
        # Restrict the retry to the partial unique index.  Check/check-FK
        # violations must still surface to the caller instead of being
        # mistaken for a concurrent duplicate request.
        original = getattr(exc, "orig", None)
        diagnostic = getattr(original, "diag", None)
        if getattr(diagnostic, "constraint_name", None) == "uq_catalog_import_jobs_active_dedup":
            return True
        # asyncpg exposes the SQLSTATE and constraint in its translated error
        # text rather than a psycopg-style ``diag`` object.
        return (
            getattr(original, "sqlstate", None) == "23505"
            and "uq_catalog_import_jobs_active_dedup" in str(original)
        )

    # The active-dedup index is the concurrency boundary.  Keep each insert
    # inside a savepoint so a duplicate request does not erase unrelated work
    # already staged in the caller's transaction.  If the winner completes
    # between the failed insert and the fallback SELECT, one retry can create
    # the now-valid next job; a second conflict is resolved by the SELECT.
    job = new_job()
    for attempt in range(2):
        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
            return job
        except IntegrityError as exc:
            if not is_active_dedup_conflict(exc):
                raise
            existing = await db.scalar(
                select(CatalogImportJob).where(
                    CatalogImportJob.organization_id == organization_id,
                    CatalogImportJob.dedup_key == dedup_key,
                    CatalogImportJob.status.in_(("queued", "running")),
                )
            )
            if existing is not None:
                return reuse(existing)
            if attempt == 0:
                job = new_job()
                continue
            raise
    raise RuntimeError("catalog import enqueue retry exhausted")


async def get_draft(
    db: AsyncSession,
    organization_id: UUID,
    draft_id: UUID,
    *,
    for_update: bool = False,
) -> CatalogDraft | None:
    stmt = select(CatalogDraft).where(
        CatalogDraft.id == draft_id,
        CatalogDraft.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


async def create_draft(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    course_code: str,
    *,
    base_revision_id: UUID | None = None,
    data: dict[str, Any] | None = None,
    reason: str | None = None,
    created_by: UUID | None = None,
) -> CatalogDraft:
    """Create a mutable candidate.

    An existing draft is returned only for an idempotent empty create.  A
    non-empty create has no expected revision and therefore cannot safely
    merge into a concurrently edited draft; callers must use PATCH instead.
    """

    term = await _ensure_term(db, organization_id, term_code)
    course = await _ensure_course(db, organization_id, course_code)
    # Match the source path's course lock before deciding whether a create is
    # idempotent.  Otherwise a concurrent source observation could create a
    # draft between this check and ``_draft_for_course``.
    course = (
        await db.scalar(
            select(CatalogCourse)
            .where(CatalogCourse.id == course.id, CatalogCourse.organization_id == organization_id)
            .with_for_update()
        )
    ) or course
    existing = await _active_draft_for(db, organization_id, term.id, course.id)
    if existing is not None and data:
        raise CatalogConflict(
            "An active draft already exists; use PATCH with its expected_revision",
            draft_id=str(existing.id),
            current_revision=int(existing.revision),
        )
    draft = await _draft_for_course(
        db,
        organization_id,
        term,
        course,
        base_revision_id=base_revision_id,
        created_by=created_by,
        initial_data=data if data else None,
    )
    if data and existing is None:
        current = dict(draft.data or {})
        current = _merge_dicts(current, _jsonable(data), dict(draft.field_overrides or {}))
        draft.data = current
    if reason:
        draft.reason = reason
    if created_by is not None:
        draft.updated_by = created_by
    await db.flush()
    return draft


def _validate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"course_code", "term", "organization_id", "id", "state", "revision"}
    if forbidden.intersection(patch):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Draft identity and revision fields cannot be changed")
    normalized = _jsonable(patch)
    if not isinstance(normalized, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Draft patch must be an object")
    if "local_credits" in normalized and normalized["local_credits"] is not None and _number(normalized["local_credits"]) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "local_credits must be numeric")
    if "ects" in normalized and normalized["ects"] is not None and _number(normalized["ects"]) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ects must be numeric")
    sections = normalized.get("sections")
    if sections is not None:
        if not isinstance(sections, list):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "sections must be a list")
        for section in sections:
            if not isinstance(section, dict) or not str(section.get("section_code") or section.get("section") or "").strip():
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Every section needs a section_code")
            for meeting in section.get("meetings") or []:
                if not isinstance(meeting, dict):
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Meeting rows must be objects")
                meeting_status = str(meeting.get("status") or "unknown")
                if meeting_status == "scheduled":
                    start, end, weekday = _int(meeting.get("start_minute")), _int(meeting.get("end_minute")), _int(meeting.get("weekday"))
                    if weekday is None or start is None or end is None or end <= start:
                        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Scheduled meetings need valid weekday and time range")
    return normalized


async def patch_draft(
    db: AsyncSession,
    organization_id: UUID,
    draft_id: UUID,
    *,
    expected_revision: int,
    patch: dict[str, Any],
    reason: str,
    updated_by: UUID | None = None,
    verify: bool = False,
    verification_evidence: str | None = None,
) -> CatalogDraft:
    if verify and not str(verification_evidence or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Explicit verification requires source or review evidence",
        )
    draft = await get_draft(db, organization_id, draft_id, for_update=True)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    if draft.state != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an active draft can be edited")
    if int(draft.revision) != expected_revision:
        raise CatalogConflict(
            "Draft changed since it was read",
            draft_id=str(draft.id),
            expected_revision=expected_revision,
            current_revision=int(draft.revision),
        )
    patch = _validate_patch(patch)
    current = dict(draft.data or {})
    overrides = dict(draft.field_overrides or {})
    current = _merge_dicts(current, patch, overrides={}, replace_sections=True)
    current["component_status"] = dict(current.get("component_status") or {})
    now = datetime.now(UTC)
    if "sections" in patch and isinstance(current.get("sections"), list):
        incoming_by_code = {
            str(item.get("section_code") or item.get("section") or ""): item
            for item in patch["sections"]
            if isinstance(item, dict)
        }
        for section in current["sections"]:
            if not isinstance(section, dict):
                continue
            section_code = str(section.get("section_code") or section.get("section") or "")
            if section_code not in incoming_by_code:
                continue
            section["restrictions_status"] = "verified" if verify else "unknown"
            section["restrictions_observed_at"] = _iso(now) if verify else None
            section["restrictions_source_fetched_at"] = None
    touched_components: set[str] = set()
    for key in patch:
        # Every admin edit is retained as a per-field source override.  An
        # ordinary edit remains pending verification: a reason documents why
        # it was changed, but it cannot manufacture source freshness.  The
        # evidence is stamped on the override itself, so a later
        # verify-overrides call can supply it without re-sending the value.
        entry: dict[str, Any] = {
            "value": _jsonable(patch[key]),
            "reason": reason,
            "updated_by": str(updated_by) if updated_by else None,
            "updated_at": _iso(now),
        }
        if verify:
            entry["verification_evidence"] = str(verification_evidence).strip()
            entry["verified_at"] = _iso(now)
            entry["verified_by"] = str(updated_by) if updated_by else None
        overrides[key] = entry
        touched_components.add(FIELD_COMPONENT_MAP.get(key, key))
        if key == "sections":
            touched_components.add("constraints")
        old_override = await db.scalar(
            select(CatalogAdminOverride).where(
                CatalogAdminOverride.organization_id == organization_id,
                CatalogAdminOverride.draft_id == draft.id,
                CatalogAdminOverride.field_name == key,
                CatalogAdminOverride.active.is_(True),
            )
        )
        if old_override is not None:
            old_override.active = False
            old_override.removed_at = now
        db.add(
            CatalogAdminOverride(
                organization_id=organization_id,
                term_id=draft.term_id,
                course_id=draft.course_id,
                draft_id=draft.id,
                field_name=key,
                value=_jsonable(patch[key]),
                reason=reason,
                active=True,
                created_by=updated_by,
            )
        )
    # Derive component freshness once the whole patch is recorded: an edit to
    # one ``details`` field must not inherit the evidence of another, and a new
    # unverified correction has to pull its component back to pending.
    apply_override_component_status(
        current["component_status"], overrides, touched_components, observed_at=_iso(now)
    )
    draft.data = current
    draft.field_overrides = overrides
    draft.reason = reason
    draft.updated_by = updated_by
    draft.revision = int(draft.revision) + 1
    _sync_manual_verification_issue(draft)
    await db.flush()
    return draft


async def _term_for_read(db: AsyncSession, organization_id: UUID, term_code: str) -> CatalogTerm | None:
    return await db.scalar(
        select(CatalogTerm).where(CatalogTerm.organization_id == organization_id, CatalogTerm.term_code == normalize_term(term_code))
    )


async def _release_for_term(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    *,
    release_id: UUID | None = None,
    required: bool = True,
) -> CatalogRelease | None:
    selected = release_id
    explicit_release = release_id is not None
    if selected is None:
        pinned_by_term = db.info.get("academic_catalog_release_ids")
        if isinstance(pinned_by_term, dict):
            value = pinned_by_term.get(str(term.id)) or pinned_by_term.get(term.term_code)
            if value:
                try:
                    selected = UUID(str(value))
                except (TypeError, ValueError):
                    selected = None
        if selected is None:
            pinned = db.info.get("academic_catalog_release_id")
            if pinned:
                try:
                    candidate = UUID(str(pinned))
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None:
                    selected = candidate
    if selected is not None:
        release = await db.scalar(
            select(CatalogRelease).where(
                CatalogRelease.id == selected,
                CatalogRelease.organization_id == organization_id,
                CatalogRelease.term_id == term.id,
            )
        )
        if release is None and not explicit_release:
            selected = None
            pointer = await db.scalar(
                select(CatalogTermActiveRelease).where(
                    CatalogTermActiveRelease.organization_id == organization_id,
                    CatalogTermActiveRelease.term_id == term.id,
                )
            )
            release = await db.scalar(
                select(CatalogRelease).where(
                    CatalogRelease.id == pointer.release_id,
                    CatalogRelease.organization_id == organization_id,
                    CatalogRelease.term_id == term.id,
                )
            ) if pointer else None
    else:
        pointer = await db.scalar(
            select(CatalogTermActiveRelease).where(
                CatalogTermActiveRelease.organization_id == organization_id,
                CatalogTermActiveRelease.term_id == term.id,
            )
        )
        release = await db.scalar(
            select(CatalogRelease).where(
                CatalogRelease.id == pointer.release_id,
                CatalogRelease.organization_id == organization_id,
                CatalogRelease.term_id == term.id,
            )
        ) if pointer else None
    if release is not None:
        db.info["academic_catalog_release_id"] = str(release.id)
        mapping = db.info.setdefault("academic_catalog_release_ids", {})
        if isinstance(mapping, dict):
            mapping[str(term.id)] = str(release.id)
    if release is None and required:
        raise CatalogUnavailable(term.term_code)
    return release


async def _release_revisions(
    db: AsyncSession,
    organization_id: UUID,
    release: CatalogRelease,
    *,
    course_code: str | None = None,
    course_codes: list[str] | None = None,
) -> list[tuple[CatalogCourse, CatalogCourseRevision]]:
    stmt = (
        select(CatalogCourse, CatalogCourseRevision)
        .join(CatalogReleaseItem, CatalogReleaseItem.course_id == CatalogCourse.id)
        .join(CatalogCourseRevision, CatalogCourseRevision.id == CatalogReleaseItem.course_revision_id)
        .where(
            CatalogReleaseItem.release_id == release.id,
            CatalogReleaseItem.organization_id == organization_id,
            CatalogCourse.organization_id == organization_id,
            CatalogCourseRevision.organization_id == organization_id,
        )
        .order_by(CatalogCourse.course_code)
    )
    if course_code is not None:
        stmt = stmt.where(CatalogCourse.course_code == normalize_course_code(course_code))
    elif course_codes is not None:
        normalized = list(dict.fromkeys(normalize_course_code(code) for code in course_codes if code))
        if not normalized:
            return []
        stmt = stmt.where(CatalogCourse.course_code.in_(normalized))
    return list((await db.execute(stmt)).all())


def _component_is_verified(revision: CatalogCourseRevision, component: str) -> bool:
    value = (revision.component_status or {}).get(component)
    return bool(isinstance(value, dict) and value.get("verified") is True)


def _registration_boundary(value: Any) -> datetime | None:
    """Parse the optional registration freshness boundary from settings."""

    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = datetime.combine(date.fromisoformat(text_value), datetime.min.time())
        except (TypeError, ValueError):
            return None
    return _utc(parsed)


def _registration_window(now: datetime | None = None) -> dict[str, Any]:
    """Return configured registration bounds and whether the window is open."""

    try:
        from app.config import get_settings

        settings = get_settings()
        raw_start = getattr(settings, "academic_catalog_registration_start", "")
        raw_end = getattr(settings, "academic_catalog_registration_end", "")
    except Exception:
        raw_start = raw_end = ""
    start = _registration_boundary(raw_start)
    end = _registration_boundary(raw_end)
    current = _utc(now or datetime.now(UTC)) or datetime.now(UTC)
    active = bool(start and current >= start and (end is None or current <= end))
    return {
        "start": str(raw_start).strip() or None,
        "end": str(raw_end).strip() or None,
        "active": active,
        "valid": not raw_start or start is not None,
        "end_valid": not raw_end or end is not None,
    }


def _effective_component_status(
    status_value: dict[str, Any],
    *,
    component: str | None = None,
    registration: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate freshness at read time instead of trusting a stale JSON flag."""

    status = dict(status_value) if isinstance(status_value, dict) else {}
    fetched = _registration_boundary(status.get("source_fetched_at"))
    verified = bool(status.get("verified") is True)
    source_status = str(status.get("source_status") or "unknown")
    verification_evidence = bool(status.get("verification_evidence"))
    # A manual review has no source fetch timestamp, but it still needs a
    # bounded evidence age. ``verified_at`` is written by PATCH; the observed
    # timestamp is accepted for older rows created before that key existed.
    verification_at = _registration_boundary(status.get("verified_at") or status.get("observed_at"))
    freshness_at = fetched
    freshness_kind = "source_fetch"
    if freshness_at is None and source_status == "admin_verified" and verification_evidence:
        freshness_at = verification_at
        freshness_kind = "verification"
    # Backfills deliberately have no source_fetched_at. Explicit human
    # verification carries evidence and is the only non-source exception.
    fresh = bool(status.get("fresh")) and verified and (
        fetched is not None
        or (source_status == "admin_verified" and verification_evidence and verification_at is not None)
    )
    stale_reason = None
    current = _utc(now or datetime.now(UTC)) or datetime.now(UTC)
    if freshness_at is not None and freshness_at > current:
        fresh = False
        stale_reason = "source_fetched_at_in_future" if freshness_kind == "source_fetch" else "verification_at_in_future"
    max_age = _COMPONENT_MAX_AGE_SECONDS.get(component or "")
    if freshness_at is not None and max_age is not None and (current - freshness_at).total_seconds() > max_age:
        fresh = False
        stale_reason = f"{freshness_kind}_expired"
    if registration and registration.get("active") and freshness_at is not None:
        start = _registration_boundary(registration.get("start"))
        if start is not None and freshness_at < start:
            fresh = False
            stale_reason = (
                "fetched_before_registration_window"
                if freshness_kind == "source_fetch"
                else "verification_before_registration_window"
            )
    if not verified:
        fresh = False
        stale_reason = stale_reason or "unverified"
    if status.get("fresh") and not fresh and stale_reason is None:
        stale_reason = "source_fetch_missing_or_expired"
    status["fresh"] = fresh
    status["verified"] = verified
    if stale_reason:
        status["stale_reason"] = stale_reason
    return status


def _evaluated_components(
    statuses: dict[str, Any] | None,
    *,
    observed_at: Any = None,
    source_fetched_at: Any = None,
) -> dict[str, Any]:
    """Fill missing components and evaluate their freshness at read time."""

    registration = _registration_window()
    result = dict(statuses or {})
    for component in COMPONENTS:
        result.setdefault(
            component,
            {
                "component": component,
                "source_status": "unknown",
                "verified": False,
                "fresh": False,
                "observed_at": _iso(observed_at),
                "source_fetched_at": _iso(source_fetched_at),
            },
        )
        result[component] = _effective_component_status(
            result[component], component=component, registration=registration
        )
    return result


def _catalog_metadata(revision: CatalogCourseRevision, release: CatalogRelease | None) -> dict[str, Any]:
    return {
        "release_id": str(release.id) if release else None,
        "course_revision_id": str(revision.id),
        "components": _jsonable(
            _evaluated_components(
                dict(revision.component_status or {}),
                observed_at=revision.observed_at,
                source_fetched_at=revision.source_fetched_at,
            )
        ),
        "registration_window": _registration_window(),
    }


def serialize_draft(
    draft: CatalogDraft,
    *,
    course_code: str | None = None,
    term: str | None = None,
) -> dict[str, Any]:
    payload = {
        "id": str(draft.id),
        "revision": int(draft.revision),
        "state": draft.state,
        "term_id": str(draft.term_id),
        "course_id": str(draft.course_id),
        "base_revision_id": str(draft.base_revision_id) if draft.base_revision_id else None,
        "published_revision_id": str(draft.published_revision_id) if draft.published_revision_id else None,
        "data": _jsonable(draft.data or {}),
        "issues": _jsonable(draft.issues or []),
        "field_overrides": _jsonable(draft.field_overrides or {}),
        "reason": draft.reason,
        "created_by": str(draft.created_by) if draft.created_by else None,
        "updated_by": str(draft.updated_by) if draft.updated_by else None,
        "created_at": _iso(draft.created_at),
        "updated_at": _iso(draft.updated_at),
    }
    if course_code is not None:
        payload["course_code"] = course_code
    if term is not None:
        payload["term"] = term
    return payload


def serialize_import_job(job: CatalogImportJob) -> dict[str, Any]:
    raw_checkpoint = dict(job.checkpoint or {})
    steps = raw_checkpoint.get("steps")
    # The operation plan can contain thousands of source calls.  The admin
    # list only needs a bounded progress summary; the full plan stays private
    # to the worker and is never copied into every polling response.
    try:
        offset = int(getattr(job, "checkpoint_offset", 0) or 0)
    except (TypeError, ValueError):
        offset = 0
    total = len(steps) if isinstance(steps, list) else raw_checkpoint.get("total", 0)
    try:
        total = max(0, int(total or 0))
    except (TypeError, ValueError):
        total = 0
    phase = raw_checkpoint.get("phase")
    if not phase and isinstance(steps, list) and 0 <= offset < len(steps):
        next_step = steps[offset]
        if isinstance(next_step, dict):
            phase = next_step.get("tool")
    progress_checkpoint: dict[str, Any] = {
        "offset": offset,
        "total": total,
        "phase": str(phase or job.status),
    }
    for key in ("retry_at", "conflicts"):
        if key in raw_checkpoint:
            progress_checkpoint[key] = _jsonable(raw_checkpoint[key])
    return {
        "id": str(job.id),
        "organization_id": str(job.organization_id),
        "term": job.term,
        "department": job.department,
        "course_codes": _jsonable(job.course_codes or []),
        "payload": _jsonable(job.payload or {}),
        "status": job.status,
        "checkpoint": progress_checkpoint,
        "attempts": job.attempts,
        "lease_until": _iso(job.lease_until),
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "dedup_key": job.dedup_key,
        "reason": job.reason,
        "priority": job.priority,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
    }


def _freshness_from_component_status(statuses: dict[str, Any] | None) -> str:
    decision = ("listing", "details", "sections", "constraints", "prerequisites")
    effective = _evaluated_components(statuses)
    if all(bool(effective[component].get("verified")) for component in decision):
        return "fresh" if all(bool(effective[component].get("fresh")) for component in decision) else "stale"
    return "unknown"


def _freshness(revision: CatalogCourseRevision) -> str:
    return _freshness_from_component_status(revision.component_status or {})


def _has_conflicts(issues: Any) -> bool:
    return any(isinstance(item, dict) and item.get("code") == "source_conflict" for item in (issues or []))


async def _active_draft_for(
    db: AsyncSession, organization_id: UUID, term_id: UUID, course_id: UUID
) -> CatalogDraft | None:
    return await db.scalar(
        select(CatalogDraft).where(
            CatalogDraft.organization_id == organization_id,
            CatalogDraft.term_id == term_id,
            CatalogDraft.course_id == course_id,
            CatalogDraft.state == "draft",
        )
    )


async def _latest_revision_for(
    db: AsyncSession, organization_id: UUID, term_id: UUID, course_id: UUID
) -> CatalogCourseRevision | None:
    return await db.scalar(
        select(CatalogCourseRevision)
        .where(
            CatalogCourseRevision.organization_id == organization_id,
            CatalogCourseRevision.term_id == term_id,
            CatalogCourseRevision.course_id == course_id,
        )
        .order_by(CatalogCourseRevision.revision.desc())
        .limit(1)
    )


async def _admin_course_pairs(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    release: CatalogRelease | None,
) -> list[tuple[CatalogCourse, CatalogCourseRevision]]:
    if release is not None:
        return await _release_revisions(db, organization_id, release)
    return list((await db.execute(
        select(CatalogCourse, CatalogCourseRevision)
        .join(CatalogCourseRevision, CatalogCourseRevision.course_id == CatalogCourse.id)
        .where(CatalogCourse.organization_id == organization_id,
               CatalogCourseRevision.organization_id == organization_id,
               CatalogCourseRevision.term_id == term.id)
        .distinct(CatalogCourseRevision.course_id)
        .order_by(CatalogCourseRevision.course_id, CatalogCourseRevision.revision.desc())
    )).all())


def _course_summary(
    course: CatalogCourse,
    revision: CatalogCourseRevision,
    release: CatalogRelease | None,
    *,
    draft: CatalogDraft | None,
    section_count: int,
) -> dict[str, Any]:
    data = dict(draft.data or {}) if draft is not None else {}

    def draft_field(key: str, fallback: Any) -> Any:
        # An active draft is what the review surface acts on, so its content
        # fields win here exactly as they do in the course detail.  Without
        # this the row said "draft" while showing the published revision.
        if draft is not None and key in data and data[key] is not None:
            return data[key]
        return fallback

    draft_completeness = data.get("completeness") if draft is not None else None
    completeness = draft_completeness if isinstance(draft_completeness, dict) else (revision.completeness or {})
    draft_statuses = data.get("component_status") if draft is not None else None
    component_status = draft_statuses if isinstance(draft_statuses, dict) else dict(revision.component_status or {})
    draft_sections = data.get("sections") if draft is not None else None
    if isinstance(draft_sections, list):
        section_count = len(draft_sections)
    catalog = _catalog_metadata(revision, release)
    if draft is not None:
        catalog["components"] = _jsonable(_evaluated_components(component_status))
    return {
        "course_code": course.course_code,
        # The spelling on the timetable, the transcript and the door of the
        # room: "CENG 331", not 5670331. The numeric code stays because it is
        # the catalog's key; this is what a person searches and reads.
        "display_code": display_code(course.course_code),
        "department": draft_field("department", course.department),
        "title": draft_field("title", revision.title),
        "local_credits": _number(draft_field("local_credits", revision.local_credits)),
        "ects": _number(draft_field("ects", revision.ects)),
        "level": draft_field("level", revision.level),
        "availability": draft_field("availability", revision.availability),
        "campus": draft_field("campus", revision.campus),
        "state": "draft" if draft else revision.state,
        "completeness": _jsonable(completeness),
        "freshness": _freshness_from_component_status(component_status),
        "source_conflicts": _has_conflicts(draft.issues if draft else revision.issues),
        "draft_id": str(draft.id) if draft else None,
        "course_revision_id": str(revision.id),
        "section_count": section_count,
        "_catalog": catalog,
    }


async def list_course_rows(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    *,
    department: str | None = None,
    query: str | None = None,
    state: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        return {
            "courses": [],
            "total": 0,
            "release_id": None,
            "counts": {
                "total": 0,
                "listed": 0,
                "detailed": 0,
                "schedulable": 0,
                "restrictions_verified": 0,
                "prerequisites_verified": 0,
                "source_conflicts": 0,
            },
        }
    release = await _release_for_term(db, organization_id, term, required=False)

    def _text_match(course_code: Any, title: Any, department_value: Any, value: str | None) -> Any:
        """Match what a person types, not only what the catalog stores.

        The catalog's key is 5670331 and its department is 567; nobody calls
        the course that. Typing "CENG 331" or "ceng331" found nothing, and
        typing "CENG" found nothing either, so the only way to search this
        panel was to already know the seven digits. The words are resolved
        against METU's own department directory - the same one the planner
        uses - and the numeric spellings they mean are matched alongside the
        raw text, which still matches titles.
        """
        if not value:
            return literal(True)
        haystack = func.lower(func.concat_ws(" ", course_code, title, department_value))
        matches = [func.strpos(haystack, literal(value)) > 0]
        lettered = department_directory.expand_course_code(value)
        if lettered is not None:
            matches.append(course_code == literal(lettered[0]))
        else:
            # A bare abbreviation or department name lists the department.
            found = department_directory.resolve(value)
            if found is not None and not value.strip().isdigit():
                matches.append(department_value == literal(found.code))
        return or_(*matches) if len(matches) > 1 else matches[0]

    def _filters(stmt: Any, *, course_code: Any, title: Any, department_value: Any, state_value: Any) -> Any:
        department_query = str(department or "").strip().casefold()
        text_query = str(query or "").strip().casefold()
        if department_query:
            # The field's own placeholder says "CNG", and until now the column
            # behind it held 567, so the one thing it invited you to type was
            # the one thing that never matched. An abbreviation or a name is
            # resolved to its code; digits still work as they always did.
            named = department_directory.resolve(department_query)
            if named is not None and not department_query.isdigit():
                stmt = stmt.where(department_value == literal(named.code))
            else:
                stmt = stmt.where(func.strpos(func.lower(department_value), literal(department_query)) > 0)
        if text_query:
            stmt = stmt.where(_text_match(course_code, title, department_value, text_query))
        if state:
            stmt = stmt.where(state_value == literal(state))
        return stmt

    # Build the published side directly in SQL.  Filtering happens before the
    # count and page queries so a large release does not become a Python list
    # just to return 50 rows.
    if release is not None:
        published_from = (
            select(
                CatalogCourse.id.label("course_id"),
                CatalogCourseRevision.id.label("revision_id"),
                CatalogCourse.course_code.label("course_code"),
                CatalogCourse.department.label("department"),
                CatalogCourseRevision.title.label("title"),
                CatalogCourseRevision.local_credits.label("local_credits"),
                CatalogCourseRevision.ects.label("ects"),
                CatalogCourseRevision.level.label("level"),
                CatalogCourseRevision.availability.label("availability"),
                CatalogCourseRevision.campus.label("campus"),
                CatalogCourseRevision.is_thesis.label("is_thesis"),
                CatalogCourseRevision.state.label("state"),
                CatalogCourseRevision.completeness.label("completeness"),
                CatalogCourseRevision.issues.label("issues"),
            )
            .join(CatalogReleaseItem, CatalogReleaseItem.course_id == CatalogCourse.id)
            .join(CatalogCourseRevision, CatalogCourseRevision.id == CatalogReleaseItem.course_revision_id)
            .where(
                CatalogReleaseItem.release_id == release.id,
                CatalogReleaseItem.organization_id == organization_id,
                CatalogCourse.organization_id == organization_id,
                CatalogCourseRevision.organization_id == organization_id,
            )
        ).subquery("catalog_release_courses")
        course_id = published_from.c.course_id
        revision_id = published_from.c.revision_id
        course_code = published_from.c.course_code
        department_value = published_from.c.department
        revision_title = published_from.c.title
        revision_local_credits = published_from.c.local_credits
        revision_ects = published_from.c.ects
        revision_level = published_from.c.level
        revision_availability = published_from.c.availability
        revision_campus = published_from.c.campus
        revision_state = published_from.c.state
        revision_completeness = published_from.c.completeness
        revision_is_thesis = published_from.c.is_thesis
        revision_issues = published_from.c.issues
        published_join = published_from
        published_revision_id = revision_id
    else:
        latest = (
            select(
                CatalogCourseRevision.id.label("revision_id"),
                CatalogCourseRevision.course_id.label("course_id"),
                func.row_number()
                .over(
                    partition_by=CatalogCourseRevision.course_id,
                    order_by=(CatalogCourseRevision.revision.desc(), CatalogCourseRevision.id.desc()),
                )
                .label("row_number"),
            )
            .where(
                CatalogCourseRevision.organization_id == organization_id,
                CatalogCourseRevision.term_id == term.id,
            )
            .subquery("catalog_latest_revisions")
        )
        published_join = (
            select(
                CatalogCourse.id.label("course_id"),
                CatalogCourseRevision.id.label("revision_id"),
                CatalogCourse.course_code.label("course_code"),
                CatalogCourse.department.label("department"),
                CatalogCourseRevision.title.label("title"),
                CatalogCourseRevision.local_credits.label("local_credits"),
                CatalogCourseRevision.ects.label("ects"),
                CatalogCourseRevision.level.label("level"),
                CatalogCourseRevision.availability.label("availability"),
                CatalogCourseRevision.campus.label("campus"),
                CatalogCourseRevision.is_thesis.label("is_thesis"),
                CatalogCourseRevision.state.label("state"),
                CatalogCourseRevision.completeness.label("completeness"),
                CatalogCourseRevision.issues.label("issues"),
            )
            .join(latest, and_(latest.c.course_id == CatalogCourse.id, latest.c.row_number == 1))
            .join(CatalogCourseRevision, CatalogCourseRevision.id == latest.c.revision_id)
            .where(CatalogCourse.organization_id == organization_id)
        ).subquery("catalog_latest_courses")
        course_id = published_join.c.course_id
        revision_id = published_join.c.revision_id
        course_code = published_join.c.course_code
        department_value = published_join.c.department
        revision_title = published_join.c.title
        revision_local_credits = published_join.c.local_credits
        revision_ects = published_join.c.ects
        revision_level = published_join.c.level
        revision_availability = published_join.c.availability
        revision_campus = published_join.c.campus
        revision_state = published_join.c.state
        revision_completeness = published_join.c.completeness
        revision_is_thesis = published_join.c.is_thesis
        revision_issues = published_join.c.issues
        published_revision_id = revision_id

    section_count = (
        select(func.count(CatalogSection.id))
        .where(
            CatalogSection.organization_id == organization_id,
            CatalogSection.course_revision_id == published_revision_id,
        )
        .correlate(published_join)
        .scalar_subquery()
    )
    draft_join = and_(
        CatalogDraft.organization_id == organization_id,
        CatalogDraft.term_id == term.id,
        CatalogDraft.course_id == course_id,
        CatalogDraft.state == "draft",
    )
    published_state = case(
        (CatalogDraft.id.is_not(None), literal("draft")),
        else_=revision_state,
    )
    published_stmt = select(
        course_id.label("course_id"),
        published_revision_id.label("revision_id"),
        CatalogDraft.id.label("draft_id"),
        course_code.label("course_code"),
        department_value.label("department"),
        revision_title.label("title"),
        revision_local_credits.label("local_credits"),
        revision_ects.label("ects"),
        revision_level.label("level"),
        revision_availability.label("availability"),
        revision_campus.label("campus"),
        revision_is_thesis.label("is_thesis"),
        published_state.label("state"),
        revision_completeness.label("completeness"),
        case((CatalogDraft.id.is_not(None), CatalogDraft.issues), else_=revision_issues).label("issues"),
        section_count.label("section_count"),
        literal(False).label("draft_only"),
    ).select_from(published_join).outerjoin(CatalogDraft, draft_join)
    published_stmt = _filters(
        published_stmt,
        course_code=course_code,
        title=revision_title,
        department_value=department_value,
        state_value=published_state,
    )

    # A draft-only course is part of the admin review surface even if no
    # revision has ever been published.  Keep that branch in the same SQL
    # union so it participates in filtering, counts and pagination.
    if release is not None:
        published_identity_exists = exists(
            select(literal(1)).select_from(CatalogReleaseItem).where(
                CatalogReleaseItem.organization_id == organization_id,
                CatalogReleaseItem.release_id == release.id,
                CatalogReleaseItem.course_id == CatalogCourse.id,
            )
        )
    else:
        published_identity_exists = exists(
            select(literal(1)).select_from(CatalogCourseRevision).where(
                CatalogCourseRevision.organization_id == organization_id,
                CatalogCourseRevision.term_id == term.id,
                CatalogCourseRevision.course_id == CatalogCourse.id,
            )
        )
    draft_sections = CatalogDraft.data["sections"]
    draft_section_count = case(
        (func.jsonb_typeof(draft_sections) == literal("array"), func.jsonb_array_length(draft_sections)),
        else_=literal(0),
    )
    draft_stmt = select(
        CatalogCourse.id.label("course_id"),
        cast(literal(None), CatalogCourseRevision.id.type).label("revision_id"),
        CatalogDraft.id.label("draft_id"),
        CatalogCourse.course_code.label("course_code"),
        CatalogCourse.department.label("department"),
        CatalogDraft.data["title"].as_string().label("title"),
        CatalogDraft.data["local_credits"].as_float().label("local_credits"),
        CatalogDraft.data["ects"].as_float().label("ects"),
        CatalogDraft.data["level"].as_string().label("level"),
        CatalogDraft.data["availability"].as_string().label("availability"),
        CatalogDraft.data["campus"].as_string().label("campus"),
        CatalogDraft.data["is_thesis"].as_boolean().label("is_thesis"),
        literal("draft").label("state"),
        CatalogDraft.data["completeness"].label("completeness"),
        CatalogDraft.issues.label("issues"),
        draft_section_count.label("section_count"),
        literal(True).label("draft_only"),
    ).select_from(CatalogCourse).join(
        CatalogDraft,
        and_(
            CatalogDraft.organization_id == organization_id,
            CatalogDraft.term_id == term.id,
            CatalogDraft.course_id == CatalogCourse.id,
            CatalogDraft.state == "draft",
        ),
    ).where(
        CatalogCourse.organization_id == organization_id,
        ~published_identity_exists,
    )
    draft_stmt = _filters(
        draft_stmt,
        course_code=CatalogCourse.course_code,
        title=CatalogDraft.data["title"].as_string(),
        department_value=CatalogCourse.department,
        state_value=literal("draft"),
    )

    candidates = union_all(published_stmt, draft_stmt).subquery("catalog_admin_candidates")
    truthy = lambda key: candidates.c.completeness.contains({key: True})
    aggregate_stmt = select(
        func.count(candidates.c.course_id),
        func.coalesce(func.sum(case((truthy("listing"), 1), else_=0)), 0),
        func.coalesce(func.sum(case((truthy("details"), 1), else_=0)), 0),
        func.coalesce(
            func.sum(case((and_(truthy("sections"), candidates.c.section_count > 0), 1), else_=0)),
            0,
        ),
        func.coalesce(func.sum(case((truthy("constraints"), 1), else_=0)), 0),
        func.coalesce(func.sum(case((truthy("prerequisites"), 1), else_=0)), 0),
        func.coalesce(func.sum(case((candidates.c.issues.contains([{"code": "source_conflict"}]), 1), else_=0)), 0),
    )
    totals = tuple((await db.execute(aggregate_stmt)).one())
    page_stmt = (
        select(candidates)
        .order_by(candidates.c.course_code)
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit), 500)))
    )
    page_rows = (await db.execute(page_stmt)).mappings().all()
    course_ids = {row["course_id"] for row in page_rows}
    revision_ids = {row["revision_id"] for row in page_rows if row["revision_id"] is not None}
    draft_ids = {row["draft_id"] for row in page_rows if row["draft_id"] is not None}
    courses = {
        row.id: row
        for row in (await db.scalars(select(CatalogCourse).where(CatalogCourse.id.in_(course_ids)))).all()
    } if course_ids else {}
    revisions = {
        row.id: row
        for row in (await db.scalars(select(CatalogCourseRevision).where(CatalogCourseRevision.id.in_(revision_ids)))).all()
    } if revision_ids else {}
    drafts = {
        row.id: row
        for row in (await db.scalars(select(CatalogDraft).where(CatalogDraft.id.in_(draft_ids)))).all()
    } if draft_ids else {}
    rows: list[dict[str, Any]] = []
    for row in page_rows:
        if not row["draft_only"]:
            course = courses.get(row["course_id"])
            revision = revisions.get(row["revision_id"])
            if course is None or revision is None:
                continue
            rows.append(
                _course_summary(
                    course,
                    revision,
                    release,
                    draft=drafts.get(row["draft_id"]),
                    section_count=int(row["section_count"] or 0),
                )
            )
            continue
        completeness = _jsonable(row["completeness"] or {})
        rows.append(
            {
                "course_code": row["course_code"],
                "display_code": display_code(row["course_code"]),
                "department": row["department"],
                "title": row["title"],
                "local_credits": _number(row["local_credits"]),
                "ects": _number(row["ects"]),
                "level": row["level"],
                "availability": row["availability"],
                "campus": row["campus"],
                "state": "draft",
                "completeness": completeness,
                "freshness": "unknown",
                "source_conflicts": _has_conflicts(row["issues"] or []),
                "draft_id": str(row["draft_id"]),
                "course_revision_id": None,
                "section_count": int(row["section_count"] or 0),
                "_catalog": {
                    "release_id": None,
                    "course_revision_id": None,
                    "components": _jsonable((drafts[row["draft_id"]].data or {}).get("component_status") or {})
                    if row["draft_id"] in drafts
                    else {},
                },
            }
        )
    return {
        "courses": rows,
        "total": int(totals[0] or 0),
        "release_id": str(release.id) if release else None,
        "counts": {
            "total": int(totals[0] or 0),
            "listed": int(totals[1] or 0),
            "detailed": int(totals[2] or 0),
            "schedulable": int(totals[3] or 0),
            "restrictions_verified": int(totals[4] or 0),
            "prerequisites_verified": int(totals[5] or 0),
            "source_conflicts": int(totals[6] or 0),
        },
    }


async def _draft_detail(
    db: AsyncSession,
    draft: CatalogDraft,
    course: CatalogCourse,
    term: CatalogTerm,
    release: CatalogRelease | None,
) -> dict[str, Any]:
    data = _jsonable(dict(draft.data or {}))
    data.setdefault("course_code", course.course_code)
    data.setdefault("department", course.department)
    data.setdefault("term", term.term_code)
    data.setdefault("sections", [])
    data.setdefault("prerequisite_groups", [])
    data.setdefault("replacements", [])
    data.setdefault("completeness", {})
    data["draft_id"] = str(draft.id)
    data["state"] = draft.state
    data["issues"] = _jsonable(draft.issues or [])
    data["field_overrides"] = _jsonable(draft.field_overrides or {})
    source_ids = []
    for value in (draft.data or {}).get("_source_observation_ids", []):
        try:
            source_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    observations = (
        await db.scalars(
            select(CatalogSourceObservation)
            .where(
                CatalogSourceObservation.organization_id == draft.organization_id,
                CatalogSourceObservation.id.in_(source_ids),
            )
            .order_by(CatalogSourceObservation.observed_at.desc(), CatalogSourceObservation.id)
        )
    ).all() if source_ids else []
    data["source_observations"] = [
        {
            "id": str(row.id),
            "tool": row.tool,
            "term": row.term,
            "department": row.department,
            "course_code": row.course_code,
            "section_code": row.section_code,
            "status": row.status,
            "issues": _jsonable(row.issues or []),
            "observed_at": _iso(row.observed_at),
            "source_fetched_at": _iso(row.source_fetched_at),
        }
        for row in observations
    ]
    data["_catalog"] = {
        "release_id": str(release.id) if release else None,
        "course_revision_id": None,
        "components": _jsonable(data.get("component_status") or {}),
    }
    data["draft_revision"] = int(draft.revision)
    data["draft"] = serialize_draft(draft)
    return data


async def course_detail(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    course_code: str,
) -> dict[str, Any]:
    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Academic term not found")
    course = await db.scalar(
        select(CatalogCourse).where(
            CatalogCourse.organization_id == organization_id,
            CatalogCourse.course_code == normalize_course_code(course_code),
        )
    )
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    release = await _release_for_term(db, organization_id, term, required=False)
    draft = await _active_draft_for(db, organization_id, term.id, course.id)
    pair = (await _release_revisions(db, organization_id, release, course_code=course.course_code)) if release else []
    if pair:
        _, revision = pair[0]
        data = await _revision_data(db, revision)
        data["term"] = term.term_code
        data["state"] = revision.state
        data["course_revision_id"] = str(revision.id)
        data["release_id"] = str(release.id) if release else None
        data["_catalog"] = _catalog_metadata(revision, release)
        # Issues are admin review material, so they are attached here rather
        # than in _revision_data, which the published reader also builds from.
        data["issues"] = _jsonable(revision.issues or [])
        if draft is not None:
            # The published revision stays at the top level - content fields and
            # release metadata - but the pending draft rides beside it.  Without
            # this, a course that already has a release could not be edited
            # after the next import created a draft: the editor read the
            # published revision's number and the save was rejected.  The
            # draft's state, issues and overrides are what the review surface
            # acts on, so they win at the top level.
            data["draft"] = serialize_draft(draft)
            data["draft_id"] = str(draft.id)
            data["draft_revision"] = int(draft.revision)
            data["state"] = draft.state
            data["issues"] = _jsonable(draft.issues or [])
            data["field_overrides"] = _jsonable(draft.field_overrides or {})
    else:
        revision = await _latest_revision_for(db, organization_id, term.id, course.id)
        if draft is not None:
            data = await _draft_detail(db, draft, course, term, release)
        elif revision is not None:
            data = await _revision_data(db, revision)
            data["term"] = term.term_code
            data["state"] = revision.state
            data["course_revision_id"] = str(revision.id)
            data["_catalog"] = _catalog_metadata(revision, release)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Course is not available for this term")
    history_rows = (
        await db.scalars(
            select(CatalogCourseRevision)
            .where(
                CatalogCourseRevision.organization_id == organization_id,
                CatalogCourseRevision.course_id == course.id,
                CatalogCourseRevision.term_id == term.id,
            )
            .order_by(CatalogCourseRevision.revision.desc())
        )
    ).all()
    data["history"] = [
        {
            "id": str(item.id),
            "action": item.state,
            "revision": item.revision,
            "state": item.state,
            "created_at": _iso(item.created_at),
            "source_fetched_at": _iso(item.source_fetched_at),
        }
        for item in history_rows
    ]
    observations = (
        await db.scalars(
            select(CatalogSourceObservation)
            .where(
                CatalogSourceObservation.organization_id == organization_id,
                CatalogSourceObservation.term == term.term_code,
                or_(
                    CatalogSourceObservation.course_code == course.course_code,
                    CatalogSourceObservation.candidate_data["courses"].contains(
                        [{"course_code": course.course_code}]
                    ),
                ),
            )
            .order_by(CatalogSourceObservation.observed_at.desc(), CatalogSourceObservation.id)
            .limit(100)
        )
    ).all()
    data["source_observations"] = [
        {
            "id": str(row.id),
            "tool": row.tool,
            "term": row.term,
            "department": row.department,
            "course_code": row.course_code,
            "section_code": row.section_code,
            "status": row.status,
            "issues": _jsonable(row.issues or []),
            "observed_at": _iso(row.observed_at),
            "source_fetched_at": _iso(row.source_fetched_at),
        }
        for row in observations
    ]
    return _jsonable(data)


_OPERATION_RESULT_KEY = "_result"


def _operation_request_payload(value: Any) -> dict[str, Any]:
    """Return the request portion of an operation's durable JSON envelope.

    Publication operations predate a dedicated response column in the frozen
    schema.  New rows therefore retain the exact response beside the request
    under a private key.  Keeping the two portions separate here means
    retries still compare the caller's request only.
    """

    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key != _OPERATION_RESULT_KEY}


def _operation_stored_result(operation: CatalogPublicationOperation) -> dict[str, Any] | None:
    value = (operation.request_payload or {}).get(_OPERATION_RESULT_KEY)
    return _jsonable(value) if isinstance(value, dict) else None


def _operation_response(
    operation: CatalogPublicationOperation,
    *,
    term: CatalogTerm,
    stored_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if stored_result is not None:
        return stored_result
    return {
        "operation_id": str(operation.id),
        "operation": operation.operation,
        "status": operation.status,
        "term": term.term_code,
        "release_id": str(operation.result_release_id) if operation.result_release_id else None,
        "target_release_id": str(operation.target_release_id) if operation.target_release_id else None,
        "idempotency_key": operation.idempotency_key,
    }


async def _active_pointer_for_update(
    db: AsyncSession, organization_id: UUID, term_id: UUID
) -> CatalogTermActiveRelease | None:
    return await db.scalar(
        select(CatalogTermActiveRelease)
        .where(
            CatalogTermActiveRelease.organization_id == organization_id,
            CatalogTermActiveRelease.term_id == term_id,
        )
        .with_for_update()
    )


async def _release_by_id(
    db: AsyncSession, organization_id: UUID, term_id: UUID, release_id: UUID | None
) -> CatalogRelease | None:
    if release_id is None:
        return None
    return await db.scalar(
        select(CatalogRelease).where(
            CatalogRelease.id == release_id,
            CatalogRelease.organization_id == organization_id,
            CatalogRelease.term_id == term_id,
        )
    )


def _blocking_draft_issues(draft: CatalogDraft) -> list[dict[str, Any]]:
    return [
        item
        for item in (draft.issues or [])
        if isinstance(item, dict) and item.get("severity", "error") == "error"
    ]


async def publish_drafts(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    draft_ids: list[UUID],
    *,
    expected_release_id: UUID | None,
    idempotency_key: str,
    reason: str,
    created_by: UUID | None = None,
    acknowledge_conflicts: bool = False,
) -> dict[str, Any]:
    """Atomically publish selected drafts and advance the term pointer."""

    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Academic term not found")
    # Lock the term row before looking up/creating the pointer.  A
    # ``FOR UPDATE`` on a missing pointer does not lock anything, so two first
    # publications could otherwise both choose release number 1 and race the
    # active pointer insert.
    term = await db.scalar(
        select(CatalogTerm)
        .where(CatalogTerm.id == term.id, CatalogTerm.organization_id == organization_id)
        .with_for_update()
    ) or term
    request_payload = {
        "term": term.term_code,
        "draft_ids": [str(item) for item in draft_ids],
        "expected_release_id": str(expected_release_id) if expected_release_id else None,
        "acknowledge_conflicts": bool(acknowledge_conflicts),
        "reason": reason,
    }
    existing_operation = await db.scalar(
        select(CatalogPublicationOperation).where(
            CatalogPublicationOperation.organization_id == organization_id,
            CatalogPublicationOperation.term_id == term.id,
            CatalogPublicationOperation.operation == "publish",
            CatalogPublicationOperation.idempotency_key == idempotency_key,
        )
    )
    if existing_operation is not None:
        if _digest(_operation_request_payload(existing_operation.request_payload)) != _digest(request_payload):
            raise CatalogConflict("Idempotency key was already used for a different publication request")
        return _operation_response(
            existing_operation,
            term=term,
            stored_result=_operation_stored_result(existing_operation),
        )
    pointer = await _active_pointer_for_update(db, organization_id, term.id)
    current = await _release_by_id(db, organization_id, term.id, pointer.release_id if pointer else None)
    current_id = current.id if current else None
    if current_id != expected_release_id:
        raise CatalogConflict(
            "The active release changed since it was read",
            expected_release_id=str(expected_release_id) if expected_release_id else None,
            current_release_id=str(current_id) if current_id else None,
        )
    drafts = (
        await db.scalars(
            select(CatalogDraft).where(
                CatalogDraft.organization_id == organization_id,
                CatalogDraft.term_id == term.id,
                CatalogDraft.id.in_(draft_ids),
            ).order_by(CatalogDraft.id).with_for_update()
        )
    ).all()
    if len(drafts) != len(set(draft_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One or more drafts were not found for this term")
    if any(draft.state != "draft" for draft in drafts):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only active drafts can be published")
    if len({draft.course_id for draft in drafts}) != len(drafts):
        raise HTTPException(status.HTTP_409_CONFLICT, "A publication request may contain one draft per course")
    # A draft may have been built from a previous release.  Check its base
    # against the exact revision currently selected for that course, rather
    # than the highest historical revision (which may have been rolled back).
    current_items = (
        await db.scalars(select(CatalogReleaseItem).where(CatalogReleaseItem.release_id == current.id))
    ).all() if current is not None else []
    active_course_revisions = {item.course_id: item.course_revision_id for item in current_items}
    stale_bases = []
    for draft in drafts:
        current_revision_id = active_course_revisions.get(draft.course_id)
        if current_revision_id is not None and draft.base_revision_id != current_revision_id:
            stale_bases.append(
                {
                    "draft_id": str(draft.id),
                    "base_revision_id": str(draft.base_revision_id) if draft.base_revision_id else None,
                    "current_revision_id": str(current_revision_id),
                }
            )
        elif current_revision_id is None and draft.base_revision_id is not None and current is not None:
            stale_bases.append(
                {
                    "draft_id": str(draft.id),
                    "base_revision_id": str(draft.base_revision_id),
                    "current_revision_id": None,
                }
            )
    if stale_bases:
        raise CatalogConflict("One or more drafts are based on a stale release revision", stale_bases=stale_bases)
    conflicts = [issue for draft in drafts for issue in (draft.issues or []) if isinstance(issue, dict) and issue.get("code") == "source_conflict"]
    if conflicts and not acknowledge_conflicts:
        raise CatalogConflict(
            "Source conflicts require explicit acknowledgement before publication",
            source_conflicts=conflicts,
        )
    blocking = [issue for draft in drafts for issue in _blocking_draft_issues(draft)]
    if blocking:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "draft_invalid", "message": "Draft contains blocking validation issues", "issues": blocking},
        )

    revisions: list[CatalogCourseRevision] = []
    for draft in drafts:
        revisions.append(await _materialize_revision(db, draft, created_by=created_by))
    latest_number = int(
        await db.scalar(
            select(func.coalesce(func.max(CatalogRelease.release_number), 0)).where(
                CatalogRelease.organization_id == organization_id,
                CatalogRelease.term_id == term.id,
            )
        )
        or 0
    )
    release = CatalogRelease(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        release_number=latest_number + 1,
        operation="publish",
        reason=reason,
        created_by=created_by,
        idempotency_key=idempotency_key,
        expected_release_id=expected_release_id,
        metadata_json={"acknowledge_conflicts": acknowledge_conflicts},
    )
    db.add(release)
    await db.flush()
    items: dict[UUID, UUID] = {}
    if current is not None:
        existing_items = (
            await db.scalars(select(CatalogReleaseItem).where(CatalogReleaseItem.release_id == current.id))
        ).all()
        items.update({item.course_id: item.course_revision_id for item in existing_items})
    for draft, revision in zip(drafts, revisions, strict=True):
        items[draft.course_id] = revision.id
        draft.state = "published"
        draft.published_revision_id = revision.id
    courses = {
        course.id: course
        for course in (
            await db.scalars(select(CatalogCourse).where(CatalogCourse.organization_id == organization_id, CatalogCourse.id.in_(items.keys())))
        ).all()
    }
    for course_id, revision_id in items.items():
        course = courses.get(course_id)
        if course is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Release contains a course outside this organization")
        db.add(
            CatalogReleaseItem(
                release_id=release.id,
                organization_id=organization_id,
                course_id=course_id,
                course_revision_id=revision_id,
                course_code=course.course_code,
            )
        )
    operation = CatalogPublicationOperation(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        operation="publish",
        idempotency_key=idempotency_key,
        expected_release_id=expected_release_id,
        result_release_id=release.id,
        status="completed",
        reason=reason,
        request_payload=request_payload,
        created_by=created_by,
        completed_at=datetime.now(UTC),
    )
    result = {
        **_operation_response(operation, term=term),
        "release_number": release.release_number,
        "published_draft_ids": [str(item.id) for item in drafts],
        "course_revision_ids": [str(item.id) for item in revisions],
    }
    # Store the exact response in the same immutable operation row.  The
    # operation and release UUIDs are assigned explicitly above so this is
    # complete before the row's INSERT (publication history is immutable once
    # persisted).
    operation.request_payload = {**request_payload, _OPERATION_RESULT_KEY: result}
    db.add(operation)
    if pointer is None:
        db.add(CatalogTermActiveRelease(organization_id=organization_id, term_id=term.id, release_id=release.id))
    else:
        pointer.release_id = release.id
    await db.flush()
    return result


async def rollback_release(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    target_release_id: UUID,
    *,
    expected_release_id: UUID | None,
    idempotency_key: str,
    reason: str,
    created_by: UUID | None = None,
) -> dict[str, Any]:
    """Create a new release pointing at a prior immutable release."""

    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Academic term not found")
    term = await db.scalar(
        select(CatalogTerm)
        .where(CatalogTerm.id == term.id, CatalogTerm.organization_id == organization_id)
        .with_for_update()
    ) or term
    request_payload = {
        "term": term.term_code,
        "target_release_id": str(target_release_id),
        "expected_release_id": str(expected_release_id) if expected_release_id else None,
        "reason": reason,
    }
    existing_operation = await db.scalar(
        select(CatalogPublicationOperation).where(
            CatalogPublicationOperation.organization_id == organization_id,
            CatalogPublicationOperation.term_id == term.id,
            CatalogPublicationOperation.operation == "rollback",
            CatalogPublicationOperation.idempotency_key == idempotency_key,
        )
    )
    if existing_operation is not None:
        if _digest(_operation_request_payload(existing_operation.request_payload)) != _digest(request_payload):
            raise CatalogConflict("Idempotency key was already used for a different rollback request")
        return _operation_response(
            existing_operation,
            term=term,
            stored_result=_operation_stored_result(existing_operation),
        )
    pointer = await _active_pointer_for_update(db, organization_id, term.id)
    current = await _release_by_id(db, organization_id, term.id, pointer.release_id if pointer else None)
    current_id = current.id if current else None
    if current_id != expected_release_id:
        raise CatalogConflict(
            "The active release changed since it was read",
            expected_release_id=str(expected_release_id) if expected_release_id else None,
            current_release_id=str(current_id) if current_id else None,
        )
    target = await _release_by_id(db, organization_id, term.id, target_release_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target release not found")
    latest_number = int(
        await db.scalar(
            select(func.coalesce(func.max(CatalogRelease.release_number), 0)).where(
                CatalogRelease.organization_id == organization_id,
                CatalogRelease.term_id == term.id,
            )
        )
        or 0
    )
    release = CatalogRelease(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        release_number=latest_number + 1,
        operation="rollback",
        reason=reason,
        created_by=created_by,
        idempotency_key=idempotency_key,
        expected_release_id=expected_release_id,
        target_release_id=target.id,
        metadata_json={"target_release_id": str(target.id)},
    )
    db.add(release)
    await db.flush()
    target_items = (await db.scalars(select(CatalogReleaseItem).where(CatalogReleaseItem.release_id == target.id))).all()
    for item in target_items:
        db.add(
            CatalogReleaseItem(
                release_id=release.id,
                organization_id=organization_id,
                course_id=item.course_id,
                course_revision_id=item.course_revision_id,
                course_code=item.course_code,
            )
        )
    operation = CatalogPublicationOperation(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        operation="rollback",
        idempotency_key=idempotency_key,
        expected_release_id=expected_release_id,
        result_release_id=release.id,
        target_release_id=target.id,
        status="completed",
        reason=reason,
        request_payload=request_payload,
        created_by=created_by,
        completed_at=datetime.now(UTC),
    )
    result = {
        **_operation_response(operation, term=term),
        "release_number": release.release_number,
        "target_release_id": str(target.id),
        "course_count": len(target_items),
    }
    operation.request_payload = {**request_payload, _OPERATION_RESULT_KEY: result}
    db.add(operation)
    if pointer is None:
        db.add(CatalogTermActiveRelease(organization_id=organization_id, term_id=term.id, release_id=release.id))
    else:
        pointer.release_id = release.id
    await db.flush()
    return result


# ---------------------------------------------------------------------------
# Published consumer reads
# ---------------------------------------------------------------------------


async def _organization_for_user(db: AsyncSession, user_id: UUID) -> UUID:
    """Resolve the tenant from an active account, never from a client hint."""

    from app.db.models import AccountDirectory, AccountStatus

    account = await db.scalar(
        select(AccountDirectory).where(
            AccountDirectory.user_id == user_id,
            AccountDirectory.status == AccountStatus.active,
        )
    )
    if account is None or account.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is inactive")
    info = getattr(db, "info", {}) or {}
    hinted = info.get("catalog_organization_id") if isinstance(info, dict) else None
    if hinted is None and isinstance(info, dict):
        hinted = info.get("organization_id")
    if hinted is not None:
        try:
            if UUID(str(hinted)) != account.organization_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Catalog organization scope mismatch")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Catalog organization scope mismatch") from exc
    return account.organization_id


def _release_metadata(
    release: CatalogRelease,
    term: CatalogTerm,
    *,
    course_count: int | None = None,
) -> dict[str, Any]:
    return {
        "catalog_release_id": str(release.id),
        "release_id": str(release.id),
        "release_number": release.release_number,
        "operation": release.operation,
        "term": term.term_code,
        "course_count": course_count,
        "components": {},
        "registration_window": _registration_window(),
    }


async def active_release_metadata(
    db: AsyncSession,
    user_id: UUID,
    term: str,
) -> dict[str, Any]:
    """Return the immutable release pin used by a consumer request."""

    organization_id = await _organization_for_user(db, user_id)
    term_row = await _term_for_read(db, organization_id, term)
    if term_row is None:
        raise CatalogUnavailable(term)
    release = await _release_for_term(db, organization_id, term_row, required=True)
    return _release_metadata(release, term_row)


async def _bulk_revision_components(
    db: AsyncSession,
    organization_id: UUID,
    revisions: list[CatalogCourseRevision],
) -> dict[str, dict[UUID, list[Any]]]:
    """Load all release children with bounded queries for planner batches."""

    revision_ids = [item.id for item in revisions]
    if not revision_ids:
        return {
            "sections": {},
            "meetings": {},
            "instructors": {},
            "restrictions": {},
            "groups": {},
            "requirements": {},
            "replacements": {},
        }
    section_rows = (
        await db.scalars(
            select(CatalogSection)
            .where(
                CatalogSection.organization_id == organization_id,
                CatalogSection.course_revision_id.in_(revision_ids),
            )
            .order_by(CatalogSection.course_revision_id, CatalogSection.section_code)
        )
    ).all()
    section_ids = [row.id for row in section_rows]
    meeting_rows = (
        await db.scalars(
            select(CatalogMeeting)
            .where(
                CatalogMeeting.organization_id == organization_id,
                CatalogMeeting.section_id.in_(section_ids),
            )
            .order_by(CatalogMeeting.section_id, CatalogMeeting.weekday, CatalogMeeting.start_minute, CatalogMeeting.id)
        )
    ).all() if section_ids else []
    instructor_rows = (
        await db.execute(
            select(CatalogSectionInstructor, CatalogInstructor)
            .join(CatalogInstructor, CatalogInstructor.id == CatalogSectionInstructor.instructor_id)
            .where(
                CatalogSectionInstructor.organization_id == organization_id,
                CatalogSectionInstructor.section_id.in_(section_ids),
            )
            .order_by(CatalogSectionInstructor.section_id, CatalogSectionInstructor.id)
        )
    ).all() if section_ids else []
    restriction_rows = (
        await db.scalars(
            select(CatalogRestriction)
            .where(
                CatalogRestriction.organization_id == organization_id,
                CatalogRestriction.course_revision_id.in_(revision_ids),
            )
            .order_by(
                CatalogRestriction.course_revision_id,
                CatalogRestriction.section_id,
                CatalogRestriction.restriction_group,
                CatalogRestriction.row_index,
                CatalogRestriction.id,
            )
        )
    ).all()
    group_rows = (
        await db.scalars(
            select(CatalogPrerequisiteGroup)
            .where(
                CatalogPrerequisiteGroup.organization_id == organization_id,
                CatalogPrerequisiteGroup.course_revision_id.in_(revision_ids),
            )
            .order_by(CatalogPrerequisiteGroup.course_revision_id, CatalogPrerequisiteGroup.group_no, CatalogPrerequisiteGroup.id)
        )
    ).all()
    group_ids = [row.id for row in group_rows]
    requirement_rows = (
        await db.scalars(
            select(CatalogPrerequisiteRequirement)
            .where(
                CatalogPrerequisiteRequirement.organization_id == organization_id,
                CatalogPrerequisiteRequirement.group_id.in_(group_ids),
            )
            .order_by(CatalogPrerequisiteRequirement.group_id, CatalogPrerequisiteRequirement.position, CatalogPrerequisiteRequirement.id)
        )
    ).all() if group_ids else []
    replacement_rows = (
        await db.scalars(
            select(CatalogCourseReplacement)
            .where(
                CatalogCourseReplacement.organization_id == organization_id,
                CatalogCourseReplacement.course_revision_id.in_(revision_ids),
            )
            .order_by(CatalogCourseReplacement.course_revision_id, CatalogCourseReplacement.related_course_code, CatalogCourseReplacement.id)
        )
    ).all()

    grouped: dict[str, dict[UUID, list[Any]]] = {
        "sections": {},
        "meetings": {},
        "instructors": {},
        "restrictions": {},
        "groups": {},
        "requirements": {},
        "replacements": {},
    }
    for row in section_rows:
        grouped["sections"].setdefault(row.course_revision_id, []).append(row)
    for row in meeting_rows:
        grouped["meetings"].setdefault(row.section_id, []).append(row)
    for link, instructor in instructor_rows:
        grouped["instructors"].setdefault(link.section_id, []).append((link, instructor))
    for row in restriction_rows:
        key = row.section_id or row.course_revision_id
        grouped["restrictions"].setdefault(key, []).append(row)
    for row in group_rows:
        grouped["groups"].setdefault(row.course_revision_id, []).append(row)
    for row in requirement_rows:
        grouped["requirements"].setdefault(row.group_id, []).append(row)
    for row in replacement_rows:
        grouped["replacements"].setdefault(row.course_revision_id, []).append(row)
    return grouped


def _public_section_payload(
    section: CatalogSection,
    children: dict[str, dict[UUID, list[Any]]],
) -> dict[str, Any]:
    meetings = [
        {
            "weekday": row.weekday,
            "day": row.weekday,
            "start_minute": row.start_minute,
            "end_minute": row.end_minute,
            "room": row.room,
            "status": row.status,
            "raw_label": row.raw_label,
        }
        for row in children["meetings"].get(section.id, [])
    ]
    instructors = [
        {
            "source_name": link.source_name,
            "name": link.source_name,
            "position": instructor.position,
            "researcher_id": link.researcher_id,
            "match_method": link.match_method,
            "resolution_status": link.resolution_status,
        }
        for link, instructor in children["instructors"].get(section.id, [])
    ]
    restrictions = [
        _restriction_dict(row)
        for row in children["restrictions"].get(section.id, [])
    ]
    payload = {
        "section_code": section.section_code,
        "section": section.section_code,
        "section_number": section.section_code,
        "status": section.status,
        "notes": section.notes,
        "syllabus_url": section.syllabus_url,
        "syllabus_available": section.syllabus_available,
        "meetings_status": section.meetings_status,
        "meeting_status": section.meetings_status,
        "restrictions_status": section.restrictions_status,
        "restrictions_observed_at": _iso(section.restrictions_observed_at),
        "restrictions_source_fetched_at": _iso(section.restrictions_source_fetched_at),
        "meetings": meetings,
        "schedule": meetings,
        "instructors": instructors,
        "restrictions": restrictions,
    }
    if instructors:
        payload["instructor"] = ", ".join(item["source_name"] for item in instructors)
    return payload


def _public_groups(
    revision: CatalogCourseRevision,
    children: dict[str, dict[UUID, list[Any]]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group in children["groups"].get(revision.id, []):
        requirements = [
            {
                "course_code": item.course_code,
                "prerequisite_course_code": item.course_code,
                "minimum_grade": item.minimum_grade,
                "requirement_type": item.requirement_type,
                "position": item.position,
                "raw_text": item.raw_text,
                "program_code": group.program_code,
                "curriculum_version": group.curriculum_version,
            }
            for item in children["requirements"].get(group.id, [])
        ]
        groups.append(
            {
                "group_no": group.group_no,
                "logic": group.logic,
                "program_code": group.program_code,
                "curriculum_version": group.curriculum_version,
                "applicability": _jsonable(group.applicability or {}),
                "verified": group.verified,
                "raw_text": group.raw_text,
                "requirements": requirements,
            }
        )
    return groups


def _flat_prerequisites(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        requirements = group.get("requirements") or []
        logic = str(group.get("logic") or "AND").upper()
        for position, requirement in enumerate(requirements):
            if not isinstance(requirement, dict):
                continue
            row = dict(requirement)
            row.setdefault("set_no", str(group.get("group_no") or 1))
            if logic == "OR":
                row["set_no"] = f"{group.get('group_no') or 1}:{position}"
            rows.append(row)
    return rows


def _prerequisite_semantics() -> dict[str, Any]:
    """State how the groups combine, which the group objects alone do not.

    A course is satisfied when ANY one group is complete; inside a group the
    group's `logic` decides (AND = every requirement). Nothing in the payload
    said so, and the model merged two alternative groups into one AND list -
    it told a student ENG 102 required ENG 101 and its alternative at once.
    """
    return {
        "groups_are_alternatives": True,
        "satisfied_by": "any_group",
        "semantics": (
            "The prerequisite is met when ANY one group is complete. Requirements inside a group "
            "combine with that group's `logic` (AND = all of them). Present the groups as "
            "alternatives; never merge them into a single combined list."
        ),
    }


async def _course_titles(
    db: AsyncSession, organization_id: UUID, term_id: UUID, codes: set[str]
) -> dict[str, str]:
    """Titles for course codes, so a prerequisite is never rendered from a guess.

    The prerequisite rows carry a code and a minimum grade and nothing else, so
    a model that wanted a readable name invented one - MATH 260 came back as
    "Diferansiyel Denklemler" when the catalog says Basic Linear Algebra.
    """
    if not codes:
        return {}
    titles: dict[str, str] = {}

    async def collect(where_codes: set[str], term_scoped: bool) -> None:
        conditions = [
            CatalogCourse.organization_id == organization_id,
            CatalogCourse.course_code.in_(where_codes),
        ]
        if term_scoped:
            conditions.append(CatalogCourseRevision.term_id == term_id)
        rows = await db.execute(
            select(CatalogCourse.course_code, CatalogCourseRevision.title)
            .join(CatalogCourseRevision, CatalogCourseRevision.course_id == CatalogCourse.id)
            .where(*conditions)
            .order_by(CatalogCourse.course_code, CatalogCourseRevision.revision.desc())
        )
        for code, title in rows:
            if title:
                titles.setdefault(code, title)

    await collect(codes, True)
    missing = codes - set(titles)
    if missing:
        # A prerequisite can be a course this term does not offer; its name is
        # still knowable from an earlier term, which beats inventing one.
        await collect(missing, False)
    return titles


async def _attach_course_titles(
    db: AsyncSession, organization_id: UUID, term_id: UUID, groups: list[dict[str, Any]]
) -> None:
    """Put each requirement's real title beside its code, in place."""
    codes = {
        str(requirement.get("course_code") or "")
        for group in groups
        for requirement in (group.get("requirements") or [])
        if isinstance(requirement, dict)
    }
    codes.discard("")
    if not codes:
        return
    titles = await _course_titles(db, organization_id, term_id, codes)
    for group in groups:
        for requirement in group.get("requirements") or []:
            if isinstance(requirement, dict):
                requirement["course_title"] = titles.get(str(requirement.get("course_code") or ""))


def _public_replacements(
    revision: CatalogCourseRevision,
    children: dict[str, dict[UUID, list[Any]]],
) -> list[dict[str, Any]]:
    return [
        {
            "relationship_type": row.relationship_type,
            "related_course_code": row.related_course_code,
            "program_code": row.program_code,
            "curriculum_version": row.curriculum_version,
            "verified": row.verified,
            "raw_text": row.raw_text,
        }
        for row in children["replacements"].get(revision.id, [])
    ]


async def _student_profile_for_catalog(
    db: AsyncSession,
    user_id: UUID,
    term: CatalogTerm,
) -> dict[str, Any] | None:
    """Load only the student fields needed for section eligibility."""

    try:
        from app.db.models import StudentAcademicSnapshot, StudentContext

        context = await db.get(StudentContext, user_id)
        snapshot = await db.scalar(
            select(StudentAcademicSnapshot).where(
                StudentAcademicSnapshot.user_id == user_id,
                StudentAcademicSnapshot.term == term.term_code,
            )
        )
    except Exception:
        return None
    if context is None and snapshot is None:
        return None
    credits = float(snapshot.current_credits) if snapshot and snapshot.current_credits else 0.0
    points = float(snapshot.current_grade_points) if snapshot else 0.0
    return {
        "department": (context.department or context.program_code) if context else None,
        "surname": context.surname_prefix if context else None,
        "year": context.year_of_study if context else None,
        "cgpa": points / credits if credits else None,
        "completed": list(snapshot.completed_courses or []) if snapshot else [],
    }


def _prior_grade(completed: list[Any], course_code: str) -> str | None:
    from app.campus.departments import expand_course_code

    def canonical(value: str) -> str:
        normalized = normalize_course_code(value)
        expanded = expand_course_code(normalized)
        return expanded[0] if expanded is not None else normalized

    try:
        wanted = canonical(course_code)
    except ValueError:
        return None
    for item in completed:
        if not isinstance(item, dict):
            continue
        try:
            code = canonical(str(item.get("course_code") or item.get("code") or ""))
        except ValueError:
            continue
        if code == wanted:
            grade = item.get("grade")
            if grade not in (None, ""):
                return str(grade)
    return None


def _section_constraint_metadata(section: CatalogSection, revision: CatalogCourseRevision) -> dict[str, Any]:
    meta = dict((revision.component_status or {}).get("constraints") or {})
    meta.update({
        "component": "constraints",
        "verified": section.restrictions_status == "verified",
        "fresh": section.restrictions_status == "verified",
        "observed_at": _iso(section.restrictions_observed_at),
        "source_fetched_at": _iso(section.restrictions_source_fetched_at),
    })
    if section.restrictions_source_fetched_at is not None:
        meta["source_status"] = "success"
    return _effective_component_status(meta, component="constraints", registration=_registration_window())


_UNAVAILABLE_CATALOG_MARKERS = (
    "closed",
    "cancelled",
    "canceled",
    "notoffered",
    "unavailable",
    "archived",
    "kapaliders",
    "kapali",
)


def _course_or_section_unavailable(
    revision: CatalogCourseRevision,
    section: CatalogSection | None,
) -> bool:
    """Recognize explicit source/admin withdrawal states.

    These rows remain visible in catalog browsing, but a known closed course
    must never become an automatically selectable planner offering merely
    because its historical meetings and prerequisites are complete.
    """

    values = [revision.availability]
    if section is not None:
        values.append(section.status)
    normalized = " ".join(_key(value) for value in values if value not in (None, ""))
    return any(marker in normalized for marker in _UNAVAILABLE_CATALOG_MARKERS)


def _section_eligibility(
    restrictions: list[CatalogRestriction],
    revision: CatalogCourseRevision,
    course_code: str,
    section: CatalogSection | None,
    profile: dict[str, Any] | None,
) -> tuple[bool | None, str | None]:
    component = (revision.component_status or {}).get("constraints")
    if not isinstance(component, dict) or component.get("verified") is not True:
        return None, "section restrictions could not be verified"
    if section is None or section.restrictions_status != "verified":
        return None, "section restrictions could not be verified"
    if not _section_constraint_metadata(section, revision).get("fresh"):
        return None, "section restrictions are stale or lack verification evidence"
    if not restrictions:
        return True, None
    if profile is None:
        return None, "student context is required to verify section restrictions"
    if not profile.get("department"):
        return None, "student department is required to verify section restrictions"
    from app.campus.eligibility import evaluate

    verdict = evaluate(
        [_restriction_dict(row) for row in restrictions],
        department=profile.get("department"),
        surname=profile.get("surname"),
        cgpa=profile.get("cgpa"),
        year=profile.get("year"),
        prior_grade=_prior_grade(profile.get("completed") or [], course_code),
    )
    return verdict.eligible, verdict.reason or None


async def _published_rows(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    release: CatalogRelease,
    *,
    course_codes: list[str] | None = None,
) -> tuple[list[tuple[CatalogCourse, CatalogCourseRevision]], dict[str, dict[UUID, list[Any]]]]:
    pairs = await _release_revisions(
        db, organization_id, release, course_codes=course_codes
    )
    revisions = [revision for _, revision in pairs]
    children = await _bulk_revision_components(db, organization_id, revisions)
    return pairs, children


def _release_catalog_envelope(
    release: CatalogRelease,
    term: CatalogTerm,
    *,
    components: dict[str, Any] | None = None,
    course_count: int | None = None,
) -> dict[str, Any]:
    metadata = _release_metadata(release, term, course_count=course_count)
    metadata["components"] = _jsonable(components or {})
    return metadata


async def _directory_read(
    db: AsyncSession,
    organization_id: UUID,
    *,
    term_code: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    releases: list[tuple[CatalogTerm, CatalogRelease]] = []
    if term_code:
        term = await _term_for_read(db, organization_id, term_code)
        if term is None:
            raise CatalogUnavailable(term_code)
        release = await _release_for_term(db, organization_id, term, required=True)
        releases.append((term, release))
    else:
        terms = (
            await db.scalars(
                select(CatalogTerm)
                .join(
                    CatalogTermActiveRelease,
                    (CatalogTermActiveRelease.term_id == CatalogTerm.id)
                    & (CatalogTermActiveRelease.organization_id == organization_id),
                )
                .where(CatalogTerm.organization_id == organization_id)
                .order_by(CatalogTerm.term_code)
            )
        ).all()
        for term in terms:
            release = await _release_for_term(db, organization_id, term, required=False)
            if release is not None:
                releases.append((term, release))
        if not releases:
            raise CatalogUnavailable()
    department_codes: set[str] = set()
    semester_rows: list[dict[str, Any]] = []
    release_ids: list[str] = []
    for term, release in releases:
        release_ids.append(str(release.id))
        semester_rows.append({"code": term.term_code, "name": term.label or term.term_code})
        pairs = await _release_revisions(db, organization_id, release)
        department_codes.update(course.department for course, _ in pairs if course.department)
    departments_payload: list[dict[str, str]] = []
    for code in sorted(department_codes):
        try:
            from app.campus import departments as department_directory

            known = department_directory.by_code(code)
        except Exception:
            known = None
        departments_payload.append({
            "code": code,
            "name": known.name if known else code,
        })
    if query:
        folded = str(query).strip().casefold()
        departments_payload = [
            row for row in departments_payload
            if folded in row["code"].casefold() or folded in row["name"].casefold()
        ]
    return {
        "departments": departments_payload,
        "semesters": semester_rows,
        "_catalog": {
            "release_id": release_ids[0] if len(release_ids) == 1 else None,
            "release_ids": release_ids,
            "course_revision_id": None,
            "components": {},
            "registration_window": _registration_window(),
        },
    }


async def _published_summary_rows(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    release: CatalogRelease,
    *,
    department: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    # Apply the public search predicates in SQL so a whole release is not
    # hydrated before the Course Info adapter can discard most rows.  The
    # public contract intentionally returns every matching course; callers do
    # not request a page, so only filtering moves across the boundary here.
    stmt = (
        select(CatalogCourse, CatalogCourseRevision)
        .join(CatalogReleaseItem, CatalogReleaseItem.course_id == CatalogCourse.id)
        .join(CatalogCourseRevision, CatalogCourseRevision.id == CatalogReleaseItem.course_revision_id)
        .where(
            CatalogReleaseItem.release_id == release.id,
            CatalogReleaseItem.organization_id == organization_id,
            CatalogCourse.organization_id == organization_id,
            CatalogCourseRevision.organization_id == organization_id,
        )
        .order_by(CatalogCourse.course_code)
    )
    department_query = str(department or "").strip().casefold()
    text_query = str(query or "").strip().casefold()
    if department_query:
        stmt = stmt.where(func.strpos(func.lower(CatalogCourse.department), literal(department_query)) > 0)
    if text_query:
        stmt = stmt.where(
            func.strpos(
                func.lower(func.concat_ws(" ", CatalogCourse.course_code, CatalogCourseRevision.title, CatalogCourse.department)),
                literal(text_query),
            ) > 0
        )
    pairs = list((await db.execute(stmt)).all())
    revision_ids = [revision.id for _, revision in pairs]
    section_counts: dict[UUID, int] = {}
    if revision_ids:
        counts = await db.execute(
            select(CatalogSection.course_revision_id, func.count(CatalogSection.id))
            .where(
                CatalogSection.organization_id == organization_id,
                CatalogSection.course_revision_id.in_(revision_ids),
            )
            .group_by(CatalogSection.course_revision_id)
        )
        section_counts = {key: int(value) for key, value in counts.all()}
    rows: list[dict[str, Any]] = []
    for course, revision in pairs:
        row = {
            "course_code": course.course_code,
            "department": course.department,
            "title": revision.title,
            "name": revision.title,
            "local_credits": float(revision.local_credits) if revision.local_credits is not None else None,
            "credits": float(revision.local_credits) if revision.local_credits is not None else None,
            "ects": float(revision.ects) if revision.ects is not None else None,
            "level": revision.level,
            "availability": revision.availability,
            "campus": revision.campus,
            "is_thesis": bool(revision.is_thesis),
            "state": revision.state,
            "completeness": _jsonable(revision.completeness or {}),
            "freshness": _freshness(revision),
            "course_revision_id": str(revision.id),
            "section_count": section_counts.get(revision.id, 0),
            "_catalog": _catalog_metadata(revision, release),
        }
        rows.append(row)
    return rows


async def _published_course_detail(
    db: AsyncSession,
    organization_id: UUID,
    term: CatalogTerm,
    release: CatalogRelease,
    course: CatalogCourse,
    revision: CatalogCourseRevision,
) -> dict[str, Any]:
    data = await _revision_data(db, revision)
    data.update(
        {
            "term": term.term_code,
            "state": revision.state,
            "credits": data.get("local_credits"),
            "name": data.get("title"),
            "release_id": str(release.id),
            "course_revision_id": str(revision.id),
            "_catalog": _catalog_metadata(revision, release),
        }
    )
    for section in data.get("sections", []):
        section.setdefault("section", section.get("section_code"))
        section.setdefault("section_number", section.get("section_code"))
        section.setdefault("schedule", section.get("meetings", []))
        section.setdefault("meeting_status", section.get("meetings_status", "unknown"))
    return _jsonable(data)


async def read_tool(
    db: AsyncSession,
    user_id: UUID,
    tool_suffix: str,
    values: dict[str, Any],
) -> Any:
    """Read one shared Course Info operation from one pinned release."""

    if tool_suffix not in CATALOG_TOOLS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Catalog tool is unavailable: {tool_suffix}")
    organization_id = await _organization_for_user(db, user_id)
    values = dict(values or {})
    term_code, department, requested_course, section_code = _scope(values)
    department, requested_course = _resolve_catalog_scope(department, requested_course)
    if tool_suffix in {"get_departments_and_semesters", "search_departments"}:
        return await _directory_read(
            db,
            organization_id,
            term_code=term_code,
            query=_field(values, "query", "keyword", "search"),
        )
    if term_code is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A semester/term is required")
    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        raise CatalogUnavailable(term_code)
    release = await _release_for_term(db, organization_id, term, required=True)
    if tool_suffix == "list_program_courses":
        rows = await _published_summary_rows(
            db,
            organization_id,
            term,
            release,
            department=department,
            query=_field(values, "query", "keyword", "search"),
        )
        return {
            "courses": rows,
            "_catalog": _release_catalog_envelope(release, term, course_count=len(rows)),
        }
    pairs = await _release_revisions(db, organization_id, release, course_code=requested_course)
    if tool_suffix == "get_thesis_courses":
        rows = [
            {
                "course_code": course.course_code,
                "department": course.department,
                "title": revision.title,
                "name": revision.title,
                "credits": float(revision.local_credits) if revision.local_credits is not None else None,
                "is_thesis": True,
                "_catalog": _catalog_metadata(revision, release),
            }
            for course, revision in pairs
            if revision.is_thesis and (not department or course.department == department)
        ]
        return {
            "courses": rows,
            "_catalog": _release_catalog_envelope(release, term, course_count=len(rows)),
        }
    if requested_course is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A course code is required")
    match = [
        (course, revision)
        for course, revision in pairs
        if course.course_code == requested_course
    ]
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course is not available in the published release")
    course, revision = match[0]
    if tool_suffix == "get_course_info":
        return await _published_course_detail(db, organization_id, term, release, course, revision)
    children = await _bulk_revision_components(db, organization_id, [revision])
    groups = _public_groups(revision, children)
    if tool_suffix == "get_course_prerequisites":
        await _attach_course_titles(db, organization_id, term.id, groups)
        component = (revision.component_status or {}).get("prerequisites", {})
        effective = _effective_component_status(component if isinstance(component, dict) else {})
        return {
            "prerequisites": _flat_prerequisites(groups),
            "prerequisite_groups": groups,
            **_prerequisite_semantics(),
            "status": "verified" if effective.get("verified") else "unknown",
            "_catalog": _catalog_metadata(revision, release),
        }
    if tool_suffix == "get_course_replacements":
        replacements = _public_replacements(revision, children)
        return {
            "replacements": replacements,
            "exclusions": [
                item["related_course_code"]
                for item in replacements
                if item["relationship_type"].casefold() in {"exclusion", "excludes"}
            ],
            "_catalog": _catalog_metadata(revision, release),
        }
    sections = children["sections"].get(revision.id, [])
    selected_sections = [
        row for row in sections
        if section_code is None or row.section_code == section_code
    ]
    if section_code is not None and not selected_sections:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Section is not available in the published release")
    restrictions: list[dict[str, Any]] = []
    for section in selected_sections:
        restrictions.extend(
            _restriction_dict(row)
            for row in children["restrictions"].get(section.id, [])
        )
    metadata = _catalog_metadata(revision, release)
    section_evidence = [_section_constraint_metadata(row, revision) for row in selected_sections]
    effective = dict(section_evidence[0]) if len(section_evidence) == 1 else {
        "verified": bool(section_evidence) and all(row.get("verified") is True for row in section_evidence),
        "fresh": bool(section_evidence) and all(row.get("fresh") is True for row in section_evidence),
    }
    metadata["components"]["constraints"] = effective
    return {
        "section_code": section_code,
        "constraints": restrictions,
        "restrictions": restrictions,
        "status": "verified" if effective.get("verified") and effective.get("fresh") else "unknown",
        "_catalog": metadata,
    }


async def published_plan_inputs(
    db: AsyncSession,
    user_id: UUID,
    term: str,
    course_codes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return offerings and rules from one immutable release in one batch.

    When ``course_codes`` is supplied, only those release revisions and their
    children are loaded. This keeps a planner request proportional to the
    student's pool instead of repeatedly reading the whole term catalog.
    """

    organization_id = await _organization_for_user(db, user_id)
    term_row = await _term_for_read(db, organization_id, term)
    if term_row is None:
        raise CatalogUnavailable(term)
    release = await _release_for_term(db, organization_id, term_row, required=True)
    pairs, children = await _published_rows(
        db, organization_id, term_row, release, course_codes=course_codes
    )
    profile = await _student_profile_for_catalog(db, user_id, term_row)
    offerings: list[dict[str, Any]] = []
    rules: dict[str, Any] = {}
    aggregate: dict[str, dict[str, Any]] = {}
    for course, revision in pairs:
        metadata = _catalog_metadata(revision, release)
        for component, value in (metadata.get("components") or {}).items():
            if not isinstance(value, dict):
                continue
            target = aggregate.setdefault(
                component,
                {"component": component, "verified": True, "fresh": True, "course_count": 0},
            )
            target["course_count"] += 1
            target["verified"] = bool(target["verified"] and value.get("verified") is True)
            target["fresh"] = bool(target["fresh"] and value.get("fresh") is True)
        section_rows = children["sections"].get(revision.id, [])
        if not section_rows:
            section_rows = [None]
        for section in section_rows:
            section_payload = _public_section_payload(section, children) if section is not None else {
                "section_code": "1",
                "section": "1",
                "section_number": "1",
                "status": "unknown",
                "meetings_status": "unknown",
                "meeting_status": "unknown",
                "restrictions_status": "unknown",
                "meetings": [],
                "schedule": [],
                "instructors": [],
                "restrictions": [],
            }
            restriction_rows = (
                children["restrictions"].get(section.id, [])
                if section is not None
                else []
            )
            eligible, eligibility_reason = _section_eligibility(
                restriction_rows, revision, course.course_code, section, profile
            )
            unavailable = _course_or_section_unavailable(revision, section)
            if unavailable:
                eligible = False
                eligibility_reason = "course is not offered in this term"
            complete = all(
                bool((revision.completeness or {}).get(component))
                for component in ("listing", "details", "sections")
            ) and revision.local_credits is not None
            data_status = "unavailable" if unavailable else _freshness(revision)
            section_payload.update(
                {
                    "course_code": course.course_code,
                    "title": revision.title,
                    "name": revision.title,
                    "credits": float(revision.local_credits) if revision.local_credits is not None else None,
                    "local_credits": float(revision.local_credits) if revision.local_credits is not None else None,
                    "ects": float(revision.ects) if revision.ects is not None else None,
                    "department": course.department,
                    "campus": revision.campus,
                    "eligible": eligible,
                    "eligibility_status": "unavailable" if unavailable else "verified" if eligible is not None else "unknown",
                    "eligibility_reason": eligibility_reason,
                    "data_status": data_status,
                    "complete": complete,
                    "catalog_release_id": str(release.id),
                    "course_revision_id": str(revision.id),
                    "aliases": list(course.aliases or []),
                    "_catalog": metadata,
                }
            )
            offerings.append(_jsonable(section_payload))
        groups = _public_groups(revision, children)
        await _attach_course_titles(db, organization_id, term_row.id, groups)
        prerequisite_component = (revision.component_status or {}).get("prerequisites", {})
        effective_rule = _effective_component_status(
            prerequisite_component if isinstance(prerequisite_component, dict) else {}
        )
        rules[course.course_code] = {
            "course_code": course.course_code,
            "prerequisites": _flat_prerequisites(groups),
            "prerequisite_groups": groups,
            **_prerequisite_semantics(),
            "replacements": _public_replacements(revision, children),
            "exclusions": [
                row.related_course_code
                for row in children["replacements"].get(revision.id, [])
                if row.relationship_type.casefold() in {"exclusion", "excludes"}
            ],
            "data_status": "verified" if effective_rule.get("verified") else "unknown",
            "fresh": bool(effective_rule.get("fresh")),
            "catalog_release_id": str(release.id),
            "course_revision_id": str(revision.id),
            "_catalog": metadata,
        }
    metadata = _release_catalog_envelope(
        release,
        term_row,
        components=aggregate,
        course_count=len(pairs),
    )
    metadata["catalog_release_id"] = str(release.id)
    # Keep the release-level provenance useful for a multi-course plan.  The
    # per-offering field remains the authoritative value for a single course;
    # this mapping lets consumers retain the exact revision identity without
    # guessing from a release-wide timestamp or a mutable course row.
    metadata["course_revision_ids"] = {
        course.course_code: str(revision.id)
        for course, revision in pairs
    }
    return offerings, rules, metadata


async def list_import_jobs(
    db: AsyncSession,
    organization_id: UUID,
    *,
    term: str | None = None,
    job_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Admin listing for durable import state, with private payload retained."""

    stmt = (
        select(CatalogImportJob)
        .where(CatalogImportJob.organization_id == organization_id)
        .order_by(CatalogImportJob.created_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if term:
        stmt = stmt.where(CatalogImportJob.term == normalize_term(term))
    if job_status:
        stmt = stmt.where(CatalogImportJob.status == str(job_status).strip().lower())
    rows = (await db.scalars(stmt)).all()
    return {
        "imports": [serialize_import_job(row) for row in rows],
        "total": len(rows),
    }


async def list_catalog_releases(
    db: AsyncSession,
    organization_id: UUID,
    term_code: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """List immutable publication/rollback releases for the admin tab."""

    term = await _term_for_read(db, organization_id, term_code)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Academic term not found")
    pointer = await db.scalar(
        select(CatalogTermActiveRelease).where(
            CatalogTermActiveRelease.organization_id == organization_id,
            CatalogTermActiveRelease.term_id == term.id,
        )
    )
    stmt = (
        select(CatalogRelease)
        .where(
            CatalogRelease.organization_id == organization_id,
            CatalogRelease.term_id == term.id,
        )
        .order_by(CatalogRelease.release_number.desc())
        .limit(max(1, min(limit, 500)))
    )
    rows = (await db.scalars(stmt)).all()
    release_ids = [row.id for row in rows]
    course_counts: dict[UUID, int] = {}
    if release_ids:
        course_counts = {
            release_id: int(count)
            for release_id, count in (
                await db.execute(
                    select(CatalogReleaseItem.release_id, func.count())
                    .where(
                        CatalogReleaseItem.organization_id == organization_id,
                        CatalogReleaseItem.release_id.in_(release_ids),
                    )
                    .group_by(CatalogReleaseItem.release_id)
                )
            ).all()
        }
    return {
        "releases": [
            {
                "id": str(row.id),
                "release_number": row.release_number,
                "operation": row.operation,
                "reason": row.reason,
                "created_by": str(row.created_by) if row.created_by else None,
                "created_at": _iso(row.created_at),
                "expected_release_id": str(row.expected_release_id) if row.expected_release_id else None,
                "target_release_id": str(row.target_release_id) if row.target_release_id else None,
                "course_count": course_counts.get(row.id, 0),
                "metadata": _jsonable(row.metadata_json or {}),
                "active": bool(pointer and pointer.release_id == row.id),
            }
            for row in rows
        ],
        "active_release_id": str(pointer.release_id) if pointer else None,
        "term": term.term_code,
    }


__all__ = [
    "CATALOG_TOOLS",
    "CatalogConflict",
    "CatalogUnavailable",
    "active_release_metadata",
    "course_detail",
    "create_draft",
    "enqueue_import",
    "get_draft",
    "import_dedup_key",
    "ingest_observation",
    "list_catalog_releases",
    "list_course_rows",
    "list_import_jobs",
    "patch_draft",
    "publish_drafts",
    "published_plan_inputs",
    "read_tool",
    "rollback_release",
    "serialize_draft",
    "serialize_import_job",
]
