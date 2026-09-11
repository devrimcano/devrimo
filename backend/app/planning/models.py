"""Canonical timetable resource contracts.

The browser used to persist a large, browser-specific object and only sent a
small projection to the broker.  These models are the boundary shared by the
schedule API, the assistant gateway and the planning service.  They accept the
old hour-shaped meeting values on input, but the persisted representation is
always minute precise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Day = Literal["Mon", "Tue", "Wed", "Thu", "Fri"]
PlanOperation = Literal[
    "replace",
    "replace_projection",
    "set_entries",
    "apply_proposal",
    "add_entry",
    "remove_entry",
    "set_pool",
    "set_options",
    "set_alternatives",
    "select_alternative",
    "favorite",
    "next_favorite",
    "clear_pool",
    "solve",
    "import_legacy",
]


def _legacy_start(value: Any) -> int:
    """Convert the pre-canonical ``start`` hour into METU minutes.

    The visual planner labels rows as ``08:40`` and old payloads only carried
    the row hour.  Retaining the forty minute offset means an imported plan
    draws and answers exactly as it did before the migration.
    """

    if isinstance(value, bool):
        raise ValueError("start must be a number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return number * 60 + 40


def _legacy_duration(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("duration must be a number")
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = 1
    return max(1, hours * 60 - 10)


def _legacy_number(value: Any) -> float | None:
    """Match the browser's numeric coercion without leaking NaN into JSON."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _legacy_day(value: Any) -> Day:
    # The previous client accepted only its five compact day tokens and mapped
    # every other value to Monday. Keep that compatibility rule in the one
    # server-side converter so all clients share it.
    return value if isinstance(value, str) and value in {"Mon", "Tue", "Wed", "Thu", "Fri"} else "Mon"


def _legacy_text(value: Any, fallback: str = "", *, limit: int) -> str:
    if value is None:
        value = fallback
    return str(value)[:limit]


def _legacy_entry(value: Any, index: int, kind: Literal["course", "block"] = "course") -> dict[str, Any] | None:
    """Convert one old entry exactly once, at the server compatibility edge."""

    if not isinstance(value, dict):
        return None
    item = dict(value)
    actual_kind: Literal["course", "block"] = "block" if item.get("kind") == "block" else kind
    if item.get("start_minute") is not None:
        start_number = _legacy_number(item.get("start_minute"))
        start = int(start_number) if start_number is not None else 0
    elif item.get("startMinute") is not None:
        start_number = _legacy_number(item.get("startMinute"))
        start = int(start_number) if start_number is not None else 0
    else:
        start_number = _legacy_number(item.get("start", 0))
        start = int(start_number * 60 + 40) if start_number is not None else 0
    if item.get("duration_minutes") is not None:
        duration_number = _legacy_number(item.get("duration_minutes"))
        duration = int(duration_number) if duration_number is not None else 1
    elif item.get("durationMinutes") is not None:
        duration_number = _legacy_number(item.get("durationMinutes"))
        duration = int(duration_number) if duration_number is not None else 1
    else:
        duration_number = _legacy_number(item.get("duration", 1))
        duration = int(duration_number * 60 - 10) if duration_number is not None else 1
    start = max(0, min(1439, start))
    duration = max(1, min(1440, duration))
    # PlanEntry intentionally rejects a cross-midnight meeting. Legacy browser
    # state clamped each field independently, so finish the same normalization
    # here rather than making imports fail on a single edge-of-day row.
    duration = min(duration, 1440 - start)

    name = _legacy_text(item.get("name"), limit=240)
    fallback_code = f"BLOCK:{name or 'busy'}" if actual_kind == "block" else ""
    code = _legacy_text(item.get("code"), fallback_code, limit=32)
    if not code:
        # A course without an identifier cannot be addressed or removed later;
        # dropping it is safer than inventing a course that was never imported.
        return None
    entry_id = _legacy_text(item.get("id"), f"{actual_kind}:{index}", limit=128)
    if not entry_id:
        return None
    credits_number = _legacy_number(item.get("credits", 0))
    color_number = _legacy_number(item.get("color", 0))
    tentative = bool(item.get("tentative", item.get("isTentative", False)))
    verification_status = _legacy_text(
        item.get("verification_status", item.get("verificationStatus")), limit=32
    ) or ("tentative" if tentative else "verified")
    if verification_status not in {
        "verified", "tentative", "unverified_constraints", "restriction_overridden"
    }:
        verification_status = "tentative" if tentative else "verified"
    release_id = _legacy_text(
        item.get("catalog_release_id", item.get("catalogReleaseId")), limit=128
    ) or None
    return {
        "id": entry_id,
        "code": code,
        "name": name,
        "section": _legacy_text(item.get("section"), limit=32),
        "credits": max(0.0, min(60.0, credits_number if credits_number is not None else 0.0)),
        "color": max(0, min(64, int(color_number) if color_number is not None else 0)),
        "kind": actual_kind,
        "instructor": _legacy_text(item.get("instructor"), limit=240),
        "day": _legacy_day(item.get("day")),
        "start_minute": start,
        "duration_minutes": duration,
        "room": _legacy_text(item.get("room"), limit=128),
        # A student may deliberately keep a hand-entered course while catalog
        # data is unavailable. It remains visibly tentative and is never
        # treated as a verified registration/timetable fact.
        "tentative": tentative,
        "verification_status": verification_status,
        "verification_reason": _legacy_text(
            item.get("verification_reason", item.get("verificationReason")), limit=1000
        ),
        "restriction_override_scope": (
            item.get("restriction_override_scope", item.get("restrictionOverrideScope"))
            if item.get("restriction_override_scope", item.get("restrictionOverrideScope"))
            in {"section", "global"}
            else None
        ),
        "catalog_release_id": release_id,
    }


def _legacy_entries(values: Any, *, kind: Literal["course", "block"] = "course") -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [entry for index, value in enumerate(values) if (entry := _legacy_entry(value, index, kind)) is not None]


class PlanMeeting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: Day
    start_minute: int = Field(ge=0, le=1439)
    duration_minutes: int = Field(ge=1, le=1440)
    room: str = Field(default="", max_length=128)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "start_minute" not in data:
            data["start_minute"] = (
                data["startMinute"] if "startMinute" in data else _legacy_start(data.get("start", 0))
            )
        if "duration_minutes" not in data:
            data["duration_minutes"] = (
                data["durationMinutes"]
                if "durationMinutes" in data
                else _legacy_duration(data.get("duration", 1))
            )
        return data

    @model_validator(mode="after")
    def fit_in_day(self) -> PlanMeeting:
        if self.start_minute + self.duration_minutes > 24 * 60:
            raise ValueError("meeting must end before midnight")
        return self

    def to_legacy(self) -> dict[str, Any]:
        """Return the compact shape understood by the existing grid and chat."""

        return {
            "day": self.day,
            "start": self.start_minute // 60,
            "duration": max(1, (self.duration_minutes + 59) // 60),
            "room": self.room,
            "start_minute": self.start_minute,
            "duration_minutes": self.duration_minutes,
        }


class PlanSection(BaseModel):
    """Catalog section returned to planner clients in one typed shape."""

    model_config = ConfigDict(extra="ignore")

    section: str = Field(min_length=1, max_length=32)
    instructor: str = Field(default="", max_length=240)
    meetings: list[PlanMeeting] = Field(default_factory=list, max_length=12)
    constraint: str = Field(default="", max_length=2000)
    # Eligibility is filled by the server's constraint endpoint. ``None`` is
    # intentionally distinct from ``False`` while a catalog restriction read
    # is unavailable; callers may display that uncertainty without inventing a
    # rejection.
    eligible: bool | None = None
    reason: str = Field(default="", max_length=1000)
    eligibility_status: str = Field(default="unknown", max_length=32)
    # Missing catalog evidence is unknown. Legacy section payloads are still
    # accepted, but a response model must not turn an omitted status into a
    # claim that the row was reviewed.
    data_status: str = Field(default="unknown", max_length=32)
    meetings_status: str = Field(default="unknown", max_length=32)
    catalog_release_id: str | None = Field(default=None, max_length=128)


class PlanEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=240)
    section: str = Field(default="", max_length=32)
    credits: float = Field(default=0, ge=0, le=60)
    color: int = Field(default=0, ge=0, le=64)
    kind: Literal["course", "block"] = "course"
    instructor: str = Field(default="", max_length=240)
    day: Day
    start_minute: int = Field(ge=0, le=1439)
    duration_minutes: int = Field(ge=1, le=1440)
    room: str = Field(default="", max_length=128)
    tentative: bool = False
    verification_status: Literal[
        "verified", "tentative", "unverified_constraints", "restriction_overridden"
    ] = "verified"
    verification_reason: str = Field(default="", max_length=1000)
    restriction_override_scope: Literal["section", "global"] | None = None
    catalog_release_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "start_minute" not in data:
            data["start_minute"] = (
                data["startMinute"] if "startMinute" in data else _legacy_start(data.get("start", 0))
            )
        if "duration_minutes" not in data:
            data["duration_minutes"] = (
                data["durationMinutes"]
                if "durationMinutes" in data
                else _legacy_duration(data.get("duration", 1))
            )
        return data

    @model_validator(mode="after")
    def fit_in_day(self) -> PlanEntry:
        if self.start_minute + self.duration_minutes > 24 * 60:
            raise ValueError("entry must end before midnight")
        # Keep the two wire fields coherent. A browser may send either the
        # explicit flag or the status label; both describe a hand-entered
        # course that has not been verified against the published catalog.
        if self.kind == "course" and (
            self.tentative or self.verification_status != "verified"
        ):
            self.tentative = True
            if self.verification_status == "verified":
                self.verification_status = "tentative"
        return self

    def to_legacy(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "section": self.section,
            "day": self.day,
            "start": self.start_minute // 60,
            "duration": max(1, (self.duration_minutes + 59) // 60),
            "room": self.room,
            "credits": self.credits,
            "color": self.color,
            "kind": self.kind,
            "instructor": self.instructor,
            "start_minute": self.start_minute,
            "duration_minutes": self.duration_minutes,
            "tentative": self.tentative,
            "verification_status": self.verification_status,
            "verification_reason": self.verification_reason,
            "restriction_override_scope": self.restriction_override_scope,
            "catalog_release_id": self.catalog_release_id,
        }


class PlanCourse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=240)
    credits: float = Field(default=0, ge=0, le=60)
    raw_code: str | None = Field(default=None, max_length=32)
    # The regular planner pool contains candidate courses.  Proposal
    # application marks the selected subset explicitly so a credit-bearing
    # course with no calendar meetings can survive in the saved state without
    # being mistaken for a scheduled entry.
    selected: bool = False
    timing_status: str | None = Field(default=None, max_length=32)
    selected_section: str | None = Field(default=None, max_length=32)

    @field_validator("raw_code", mode="before")
    @classmethod
    def accept_camel_raw_code(cls, value: Any) -> Any:
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_raw_code(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "raw_code" not in data and "rawCode" in data:
            data["raw_code"] = data["rawCode"]
        return data


class PlanState(BaseModel):
    """All student-owned planner state for one academic term."""

    model_config = ConfigDict(extra="allow")

    entries: list[PlanEntry] = Field(default_factory=list, max_length=200)
    department: str = Field(default="", max_length=64)
    department_label: str = Field(default="", max_length=240)
    empty_days: list[Day] = Field(default_factory=list, max_length=5)
    avoid_conflicts: bool = True
    ignore_constraints: bool = False
    pool: list[PlanCourse] = Field(default_factory=list, max_length=40)
    sections: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    alternatives: list[list[PlanEntry]] = Field(default_factory=list, max_length=200)
    alternative_index: int = Field(default=0, ge=0)
    favorites: list[list[PlanEntry]] = Field(default_factory=list, max_length=10)
    favorite_index: int = Field(default=-1, ge=-1)
    # The immutable academic catalog release and student snapshot used to
    # produce course-linked entries. A later catalog publication marks this
    # state for revalidation without rewriting the student's plan.
    catalog_release_id: str | None = Field(default=None, max_length=128)
    academic_snapshot_fetched_at: datetime | None = None
    needs_revalidation: bool = False

    @model_validator(mode="after")
    def normalize_indexes(self) -> PlanState:
        if self.alternatives:
            self.alternative_index = min(self.alternative_index, len(self.alternatives) - 1)
        else:
            self.alternative_index = 0
        if self.favorites:
            self.favorite_index = min(self.favorite_index, len(self.favorites) - 1)
        else:
            self.favorite_index = -1
        self.empty_days = list(dict.fromkeys(self.empty_days))
        return self

    @classmethod
    def from_legacy_payload(cls, payload: Any) -> PlanState:
        """Convert the browser's v1 projection into the canonical state.

        This is the sole compatibility boundary. In particular, callers must
        pass raw legacy data for ``import_legacy`` rather than reproducing these
        defaults in a browser adapter.
        """

        if not isinstance(payload, dict):
            return cls()
        data = dict(payload)
        aliases = {
            "department_label": "departmentLabel",
            "empty_days": "emptyDays",
            "avoid_conflicts": "avoidConflicts",
            "ignore_constraints": "ignoreConstraints",
            "alternative_index": "alternativeIndex",
            "favorite_index": "favoriteIndex",
            "catalog_release_id": "catalogReleaseId",
            "academic_snapshot_fetched_at": "academicSnapshotFetchedAt",
            "needs_revalidation": "needsRevalidation",
        }
        for canonical, legacy in aliases.items():
            if (canonical not in data or data[canonical] is None) and legacy in data:
                data[canonical] = data[legacy]

        def is_explicitly_untimed(value: Any) -> bool:
            return str(value or "").strip().casefold() in {"untimed", "explicitly_untimed"}

        pool: list[dict[str, Any]] = []
        raw_pool = data.get("pool")
        if isinstance(raw_pool, list):
            for value in raw_pool:
                if not isinstance(value, dict):
                    continue
                course = dict(value)
                code = _legacy_text(course.get("code"), limit=32).strip()
                if not code:
                    continue
                credits = _legacy_number(course.get("credits", 0))
                raw_code = course.get("raw_code")
                if raw_code is None:
                    raw_code = course.get("rawCode")
                pool.append(
                    {
                        **course,
                        "code": code,
                        "name": _legacy_text(course.get("name"), code, limit=240),
                        "credits": max(0.0, min(60.0, credits if credits is not None else 0.0)),
                        "raw_code": _legacy_text(raw_code, code, limit=32),
                        "selected": course.get("selected") is True,
                        "timing_status": (
                            "untimed"
                            if is_explicitly_untimed(course.get("timing_status"))
                            or course.get("untimed") is True
                            else None
                        ),
                        "selected_section": _legacy_text(
                            course.get("selected_section", course.get("selectedSection")), limit=32
                        ) or None,
                    }
                )

        # The old projection had no timetable entry shape for a credit-bearing
        # course without meeting times. Preserve an explicitly untimed course
        # in the canonical pool when importing that projection; an empty or
        # malformed schedule alone never implies this status.
        legacy_courses = data.get("courses") if isinstance(data.get("courses"), list) else []
        for value in legacy_courses:
            if not isinstance(value, dict):
                continue
            meetings = value.get("meetings") if isinstance(value.get("meetings"), list) else []
            if meetings or not (
                is_explicitly_untimed(value.get("timing_status")) or value.get("untimed") is True
            ):
                continue
            code = _legacy_text(value.get("code"), limit=32).strip()
            if not code:
                continue
            raw_code = _legacy_text(value.get("raw_code", value.get("rawCode", code)), code, limit=32)
            identity = "".join(code.upper().split())
            existing = next((item for item in pool if "".join(str(item.get("code", "")).upper().split()) == identity), None)
            if existing is not None:
                existing["selected"] = True
                existing["timing_status"] = "untimed"
                continue
            credits = _legacy_number(value.get("credits", 0))
            pool.append(
                {
                    "code": code,
                    "name": _legacy_text(value.get("name"), code, limit=240),
                    "credits": max(0.0, min(60.0, credits if credits is not None else 0.0)),
                    "raw_code": raw_code,
                    "selected": True,
                    "timing_status": "untimed",
                    "selected_section": _legacy_text(
                        value.get("section", value.get("section_number")), limit=32
                    ) or None,
                }
            )
        data["pool"] = pool

        # Explicit entries win. If the old projection has no entries, expand
        # courses and busy blocks once, with generated ids applied after each
        # meeting so a meeting cannot overwrite its stable compatibility id.
        if isinstance(data.get("entries"), list):
            raw_entries = data["entries"]
        else:
            raw_entries = []
            courses = legacy_courses
            for course in courses:
                if not isinstance(course, dict):
                    continue
                meetings = course.get("meetings") if isinstance(course.get("meetings"), list) else []
                for index, meeting in enumerate(meetings):
                    if not isinstance(meeting, dict):
                        continue
                    raw_entries.append(
                        {
                            **meeting,
                            "id": f"course:{course.get('code', '')}:{course.get('section', '')}:{index}",
                            "code": course.get("code", ""),
                            "name": course.get("name", ""),
                            "section": course.get("section", ""),
                            "credits": course.get("credits", 0) if index == 0 else 0,
                            "kind": "course",
                            "instructor": course.get("instructor", ""),
                        }
                    )
            blocks = data.get("busy_blocks") if isinstance(data.get("busy_blocks"), list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                name = block.get("name", "busy")
                meetings = block.get("meetings") if isinstance(block.get("meetings"), list) else []
                for index, meeting in enumerate(meetings):
                    if not isinstance(meeting, dict):
                        continue
                    raw_entries.append(
                        {
                            **meeting,
                            "id": f"block:{name}:{index}",
                            "code": f"BLOCK:{name}",
                            "name": name,
                            "kind": "block",
                        }
                    )
        data["entries"] = _legacy_entries(raw_entries)

        def legacy_entry_groups(value: Any) -> list[list[dict[str, Any]]]:
            if not isinstance(value, list):
                return []
            groups: list[list[dict[str, Any]]] = []
            for group in value:
                if not isinstance(group, list):
                    continue
                groups.append(_legacy_entries(group))
            return groups

        data["alternatives"] = legacy_entry_groups(data.get("alternatives"))
        data["favorites"] = legacy_entry_groups(data.get("favorites"))

        raw_empty_days = data.get("empty_days")
        data["empty_days"] = (
            [
                day
                for day in raw_empty_days
                if isinstance(day, str) and day in {"Mon", "Tue", "Wed", "Thu", "Fri"}
            ]
            if isinstance(raw_empty_days, list)
            else []
        )
        data["department"] = _legacy_text(data.get("department"), limit=64)
        data["department_label"] = _legacy_text(data.get("department_label"), limit=240)
        data["avoid_conflicts"] = (
            data["avoid_conflicts"]
            if isinstance(data.get("avoid_conflicts"), bool)
            else data.get("avoidConflicts") is not False
        )
        data["ignore_constraints"] = (
            data["ignore_constraints"]
            if isinstance(data.get("ignore_constraints"), bool)
            else data.get("ignoreConstraints") is True
        )
        alternative_index = _legacy_number(data.get("alternative_index", 0))
        favorite_default = len(data["favorites"]) - 1 if data["favorites"] else -1
        favorite_index = _legacy_number(data.get("favorite_index", favorite_default))
        data["alternative_index"] = max(0, int(alternative_index) if alternative_index is not None else 0)
        data["favorite_index"] = int(favorite_index) if favorite_index is not None else favorite_default
        data["catalog_release_id"] = _legacy_text(data.get("catalog_release_id"), limit=128) or None
        snapshot_time = data.get("academic_snapshot_fetched_at")
        data["academic_snapshot_fetched_at"] = snapshot_time
        data["needs_revalidation"] = bool(data.get("needs_revalidation", False))

        raw_sections = data.get("sections")
        sections: dict[str, list[dict[str, Any]]] = {}
        if isinstance(raw_sections, dict):
            for key, value in raw_sections.items():
                if isinstance(value, list):
                    sections[str(key)] = [item for item in value if isinstance(item, dict)]
                elif isinstance(value, dict):
                    sections[str(key)] = [item for item in value.values() if isinstance(item, dict)]
        data["sections"] = sections
        return cls.model_validate(data)

    def to_payload(self) -> dict[str, Any]:
        """Serialize the canonical state with compact legacy aliases included."""

        data = self.model_dump(mode="json")
        data["entries"] = [entry.to_legacy() for entry in self.entries]
        data["alternatives"] = [[entry.to_legacy() for entry in alternative] for alternative in self.alternatives]
        data["favorites"] = [[entry.to_legacy() for entry in favorite] for favorite in self.favorites]
        # Keep the old names for clients that have one release of the browser
        # cached.  New clients use the snake-case canonical fields above.
        data.update(
            {
                "departmentLabel": self.department_label,
                "emptyDays": self.empty_days,
                "avoidConflicts": self.avoid_conflicts,
                "ignoreConstraints": self.ignore_constraints,
                "alternativeIndex": self.alternative_index,
                "favoriteIndex": self.favorite_index,
                "catalogReleaseId": self.catalog_release_id,
                "academicSnapshotFetchedAt": (
                    self.academic_snapshot_fetched_at.isoformat()
                    if self.academic_snapshot_fetched_at is not None
                    else None
                ),
                "needsRevalidation": self.needs_revalidation,
            }
        )
        return data


class PlanChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: PlanOperation
    state: PlanState | None = None
    entry: PlanEntry | None = None
    entry_id: str | None = Field(default=None, max_length=128)
    entries: list[PlanEntry] | None = Field(default=None, max_length=200)
    pool: list[PlanCourse] | None = Field(default=None, max_length=40)
    sections: dict[str, list[dict[str, Any]]] | None = None
    empty_days: list[Day] | None = Field(default=None, max_length=5)
    avoid_conflicts: bool | None = None
    ignore_constraints: bool | None = None
    alternatives: list[list[PlanEntry]] | None = Field(default=None, max_length=200)
    alternative_index: int | None = Field(default=None, ge=0)
    projection: dict[str, Any] | None = None


class PlanEnvelope(BaseModel):
    resource: Literal["timetable"] = "timetable"
    user_id: str
    term: str
    revision: int = Field(ge=0)
    updated_at: datetime | None = None
    state: PlanState
    can_undo: bool = False
    previous_revision: int | None = None
    operation: str | None = None
    idempotency_key: str | None = None
    catalog_release_id: str | None = None
    needs_revalidation: bool = False


class PlanConflictError(Exception):
    def __init__(self, current: PlanEnvelope):
        self.current = current
        super().__init__("The timetable changed in another tab. Reload it before saving.")


class PlanValidationError(ValueError):
    pass


class PlanIdempotencyError(PlanValidationError):
    """An idempotency key was reused with a different request payload."""


class PlanNotFoundError(LookupError):
    pass
