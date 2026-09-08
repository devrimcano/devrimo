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
    "set_mode",
    "set_locks",
    "regenerate",
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


def _check_nested_budget(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Bound untyped JSON planner fields before they reach PostgreSQL."""

    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 20_000:
        raise ValueError("planner state is too large")
    if depth > 10:
        raise ValueError("planner state is too deeply nested")
    if isinstance(value, str) and len(value) > 10_000:
        raise ValueError("planner state contains an oversized text value")
    if isinstance(value, dict):
        if len(value) > 500:
            raise ValueError("planner state contains too many fields")
        for key, child in value.items():
            _check_nested_budget(key, depth=depth + 1, nodes=nodes)
            _check_nested_budget(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise ValueError("planner state contains too many items")
        for child in value:
            _check_nested_budget(child, depth=depth + 1, nodes=nodes)


def check_plan_state_budget(state: Any) -> None:
    """Bound every untyped or nested planner branch before persistence."""

    for value in (
        getattr(state, "sections", None),
        getattr(state, "what_if_backup", None),
        getattr(state, "alternatives", None),
        getattr(state, "favorites", None),
    ):
        _check_nested_budget(value)


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
            data["start_minute"] = _legacy_start(data.get("start", 0))
        if "duration_minutes" not in data:
            data["duration_minutes"] = _legacy_duration(data.get("duration", 1))
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
    # ``verified`` means the server evaluated this student's section against a
    # current SAIS context.  The other values are deliberately retained all
    # the way to the browser so an unavailable read can never look like an
    # open section.
    eligibility_status: Literal["verified", "stale", "unavailable", "unverified"] = "unverified"
    reason: str = Field(default="", max_length=1000)
    # Opaque proof issued by the owner service. Normal timetable writes must
    # present it with the same student-context versions used for the verdict.
    eligibility_token: str | None = Field(default=None, max_length=160)
    # The signed verdict uses the display alias and the catalog's raw identity
    # separately. Keep both through the response model so the planner can bind
    # a visible MATH119 row to its authoritative 2360119 catalog record.
    eligibility_course_code: str | None = Field(default=None, max_length=32)
    eligibility_raw_code: str | None = Field(default=None, max_length=32)


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

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "start_minute" not in data:
            data["start_minute"] = _legacy_start(data.get("start", 0))
        if "duration_minutes" not in data:
            data["duration_minutes"] = _legacy_duration(data.get("duration", 1))
        return data

    @model_validator(mode="after")
    def fit_in_day(self) -> PlanEntry:
        if self.start_minute + self.duration_minutes > 24 * 60:
            raise ValueError("entry must end before midnight")
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
        }


class PlanCourse(BaseModel):
    # Course identity and metadata are server-owned evidence. Unknown fields
    # from an older browser must not survive into the canonical JSON payload.
    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=240)
    credits: float = Field(default=0, ge=0, le=60)
    raw_code: str | None = Field(default=None, max_length=32)
    required: bool = True

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

    # Legacy camel-case aliases are normalized in from_legacy_payload; the
    # persisted state itself has no open-ended JSON extension point.
    model_config = ConfigDict(extra="ignore")

    state_version: int = Field(default=2, ge=1, le=2)
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
    # What-if is an explicit escape hatch for a student who wants to explore
    # an unverified or otherwise ineligible section. Normal plans require a
    # positive server verdict before an entry can be saved.
    what_if: bool = False
    # When what-if exploration is enabled, the untouched normal draft is kept
    # here so leaving exploration restores the registration-ready draft. This
    # is persisted with the revision rather than relying on browser memory.
    what_if_backup: dict[str, Any] | None = None
    locked_sections: dict[str, str] = Field(default_factory=dict, max_length=40)
    unscheduled_courses: list[str] = Field(default_factory=list, max_length=40)
    generation_error: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def normalize_indexes(self) -> PlanState:
        check_plan_state_budget(self)
        if self.alternatives:
            self.alternative_index = min(self.alternative_index, len(self.alternatives) - 1)
        else:
            self.alternative_index = 0
        if self.favorites:
            self.favorite_index = min(self.favorite_index, len(self.favorites) - 1)
        else:
            self.favorite_index = -1
        self.empty_days = list(dict.fromkeys(self.empty_days))
        self.locked_sections = {
            str(code): str(section)
            for code, section in self.locked_sections.items()
            if str(code).strip() and str(section).strip()
        }
        self.unscheduled_courses = list(dict.fromkeys(str(code) for code in self.unscheduled_courses if str(code).strip()))
        return self

    @classmethod
    def from_legacy_payload(cls, payload: Any) -> PlanState:
        if not isinstance(payload, dict):
            return cls()
        data = dict(payload)
        # Pre-canonical browser projections had no eligibility evidence. Keep
        # those plans readable, but make their exploratory nature explicit so
        # the first normal-plan write cannot accidentally certify them.
        if "state_version" not in data:
            data["state_version"] = 2
            if data.get("entries") or data.get("courses"):
                data["what_if"] = True
        if "department_label" not in data and "departmentLabel" in data:
            data["department_label"] = data["departmentLabel"]
        if "empty_days" not in data and "emptyDays" in data:
            data["empty_days"] = data["emptyDays"]
        if "avoid_conflicts" not in data and "avoidConflicts" in data:
            data["avoid_conflicts"] = data["avoidConflicts"]
        if "ignore_constraints" not in data and "ignoreConstraints" in data:
            data["ignore_constraints"] = data["ignoreConstraints"]
        if "what_if" not in data and "whatIf" in data:
            data["what_if"] = data["whatIf"]
        if "what_if_backup" not in data and "whatIfBackup" in data:
            data["what_if_backup"] = data["whatIfBackup"]
        if "alternative_index" not in data and "alternativeIndex" in data:
            data["alternative_index"] = data["alternativeIndex"]
        if "favorite_index" not in data and "favoriteIndex" in data:
            data["favorite_index"] = data["favoriteIndex"]
        if "locked_sections" not in data and "lockedSections" in data:
            data["locked_sections"] = data["lockedSections"]
        data["pool"] = [
            {
                **course,
                "raw_code": course.get("raw_code") or course.get("rawCode") or course.get("code"),
            }
            if isinstance(course, dict)
            else course
            for course in data.get("pool", [])
        ]
        if "entries" not in data:
            entries: list[dict[str, Any]] = []
            for course in data.get("courses", []):
                if not isinstance(course, dict):
                    continue
                meetings = course.get("meetings", [])
                for index, meeting in enumerate(meetings if isinstance(meetings, list) else []):
                    if not isinstance(meeting, dict):
                        continue
                    entries.append(
                        {
                            "id": f"course:{course.get('code', '')}:{course.get('section', '')}:{index}",
                            "code": course.get("code", ""),
                            "name": course.get("name", ""),
                            "section": course.get("section", ""),
                            "credits": course.get("credits", 0) if index == 0 else 0,
                            "kind": "course",
                            "instructor": course.get("instructor", ""),
                            **meeting,
                        }
                    )
            for block in data.get("busy_blocks", []):
                if not isinstance(block, dict):
                    continue
                meetings = block.get("meetings", [])
                for index, meeting in enumerate(meetings if isinstance(meetings, list) else []):
                    if not isinstance(meeting, dict):
                        continue
                    entries.append(
                        {
                            "id": f"block:{block.get('name', 'busy')}:{index}",
                            "code": f"BLOCK:{block.get('name', 'busy')}",
                            "name": block.get("name", "busy"),
                            "kind": "block",
                            **meeting,
                        }
                    )
            data["entries"] = entries
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
                "stateVersion": self.state_version,
                "departmentLabel": self.department_label,
                "emptyDays": self.empty_days,
                "avoidConflicts": self.avoid_conflicts,
                "ignoreConstraints": self.ignore_constraints,
                "alternativeIndex": self.alternative_index,
                "favoriteIndex": self.favorite_index,
                "whatIf": self.what_if,
                "whatIfBackup": self.what_if_backup,
                "lockedSections": self.locked_sections,
                "unscheduledCourses": self.unscheduled_courses,
                "generationError": self.generation_error,
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
    what_if: bool | None = None
    locked_sections: dict[str, str] | None = Field(default=None, max_length=40)


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


class PlanConflictError(Exception):
    def __init__(self, current: PlanEnvelope):
        self.current = current
        super().__init__("The timetable changed in another tab. Reload it before saving.")


class PlanValidationError(ValueError):
    pass


class PlanNotFoundError(LookupError):
    pass
