"""Pydantic contracts for the course catalog administration surface."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


def normalize_term(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("A term is required")
    if len(value) > 32:
        raise ValueError("Term is too long")
    return value


def normalize_course_code(value: str) -> str:
    value = str(value or "").strip().upper()
    value = re.sub(r"\s+", "", value)
    value = value.replace("-", "")
    if not value or len(value) > 32:
        raise ValueError("A course code is required")
    return value


class MeetingPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    weekday: int | None = Field(default=None, ge=0, le=6)
    start_minute: int | None = Field(default=None, ge=0, le=1439)
    end_minute: int | None = Field(default=None, ge=1, le=1440)
    room: str | None = Field(default=None, max_length=500)
    status: Literal["scheduled", "untimed", "unpublished", "invalid", "unknown"] = "scheduled"
    raw_label: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def valid_range(self) -> "MeetingPatch":
        if self.start_minute is None and self.end_minute is None:
            if self.status == "scheduled":
                raise ValueError("Scheduled meetings require a weekday and start/end minutes")
            return self
        if self.status == "scheduled" and self.weekday is None:
            raise ValueError("Scheduled meetings require a weekday")
        if self.start_minute is None or self.end_minute is None or self.end_minute <= self.start_minute:
            raise ValueError("Meeting start and end must form a positive range")
        return self


class InstructorPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_name: str = Field(min_length=1, max_length=500)
    position: str | None = Field(default=None, max_length=500)
    researcher_id: int | None = None
    match_method: str = Field(default="unmatched", max_length=32)
    resolution_status: str = Field(default="unresolved", max_length=32)


class RestrictionPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    restriction_group: str | None = Field(default=None, max_length=64)
    row_index: int = Field(default=0, ge=0)
    kind: str = Field(min_length=1, max_length=32)
    value_text: str | None = Field(default=None, max_length=2000)
    value_numeric: float | None = None
    operator: str | None = Field(default=None, max_length=16)
    minimum_grade: str | None = Field(default=None, max_length=8)
    given_department: str | None = Field(default=None, max_length=32)
    start_char: str | None = Field(default=None, max_length=16)
    end_char: str | None = Field(default=None, max_length=16)
    min_cgpa: float | None = None
    max_cgpa: float | None = None
    min_year: int | None = Field(default=None, ge=0, le=100)
    max_year: int | None = Field(default=None, ge=0, le=100)
    start_grade: str | None = Field(default=None, max_length=128)
    end_grade: str | None = Field(default=None, max_length=128)
    prior_course_code: str | None = Field(default=None, max_length=32)
    program_code: str | None = Field(default=None, max_length=64)
    curriculum_version: str | None = Field(default=None, max_length=64)
    raw_text: str | None = Field(default=None, max_length=4000)
    verified: bool = False
    status: Literal["verified", "unknown", "unavailable", "stale"] = "unknown"


class SectionPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    section_code: str = Field(min_length=1, max_length=32)
    status: str = Field(default="listed", max_length=32)
    notes: str | None = Field(default=None, max_length=4000)
    syllabus_url: str | None = Field(default=None, max_length=2000)
    syllabus_available: bool | None = None
    # ``untimed`` is an explicit source/admin assertion that this section has
    # no scheduled meeting.  Keep the legacy spelling accepted at the API
    # boundary so older clients can be migrated without losing the evidence;
    # materialization canonicalizes it to ``untimed``.
    meetings_status: Literal[
        "verified", "unknown", "untimed", "explicitly_untimed", "unpublished", "invalid"
    ] = "unknown"
    meetings: list[MeetingPatch] = Field(default_factory=list)
    instructors: list[InstructorPatch] = Field(default_factory=list)
    restrictions: list[RestrictionPatch] = Field(default_factory=list)

    @field_validator("section_code")
    @classmethod
    def normalize_section(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("A section code is required")
        return value


class PrerequisiteRequirementPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    course_code: str
    minimum_grade: str | None = Field(default=None, max_length=8)
    requirement_type: str = Field(default="course", max_length=16)
    position: int = Field(default=0, ge=0)
    raw_text: str | None = Field(default=None, max_length=4000)

    @field_validator("course_code")
    @classmethod
    def normalize_requirement_code(cls, value: str) -> str:
        return normalize_course_code(value)


class PrerequisiteGroupPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    group_no: int = Field(default=1, ge=1)
    logic: Literal["AND", "OR", "and", "or"] = "AND"
    program_code: str | None = Field(default=None, max_length=64)
    curriculum_version: str | None = Field(default=None, max_length=64)
    applicability: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    raw_text: str | None = Field(default=None, max_length=4000)
    requirements: list[PrerequisiteRequirementPatch] = Field(default_factory=list)

    @field_validator("logic")
    @classmethod
    def normalize_logic(cls, value: str) -> str:
        return value.upper()


class ReplacementPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    relationship_type: str = Field(min_length=1, max_length=32)
    related_course_code: str
    program_code: str | None = Field(default=None, max_length=64)
    curriculum_version: str | None = Field(default=None, max_length=64)
    verified: bool = False
    raw_text: str | None = Field(default=None, max_length=4000)

    @field_validator("related_course_code")
    @classmethod
    def normalize_related_code(cls, value: str) -> str:
        return normalize_course_code(value)


class CoursePatch(BaseModel):
    """Typed fields accepted by a draft patch.

    ``extra=allow`` is intentional for newly introduced source fields.  The
    service still rejects identity changes and serializes only JSON values;
    adding an academic field therefore does not require a frontend deploy.
    """

    model_config = ConfigDict(extra="allow")

    title: str | None = Field(default=None, max_length=2000)
    department: str | None = Field(default=None, max_length=32)
    local_credits: float | None = Field(default=None, ge=0, le=100)
    ects: float | None = Field(default=None, ge=0, le=100)
    level: str | None = Field(default=None, max_length=64)
    availability: str | None = Field(default=None, max_length=128)
    campus: str | None = Field(default=None, max_length=128)
    completeness: dict[str, Any] | None = None
    component_status: dict[str, Any] | None = None
    sections: list[SectionPatch] | None = None
    prerequisite_groups: list[PrerequisiteGroupPatch] | None = None
    replacements: list[ReplacementPatch] | None = None
    thesis_courses: list[dict[str, Any]] | None = None


# These values are derived from source observations or an explicit review
# action.  They must never be accepted from the browser as ordinary draft
# data, because doing so would let a client manufacture a fresh/verified
# catalog component.  Keep this check recursive: section and requirement
# payloads can otherwise hide the same fields below an otherwise valid patch.
_ADMIN_DERIVED_KEYS = frozenset(
    {
        "componentstatus",
        "completeness",
        "fresh",
        "freshness",
        "sourcestatus",
        "restrictionsstatus",
        "observedat",
        "verifiedat",
        "verificationevidence",
        "verified",
        "registrationwindow",
    }
)


def _is_admin_derived_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return (
        normalized in _ADMIN_DERIVED_KEYS
        or normalized.startswith("sourceobservation")
        or normalized.endswith("sourcefetchedat")
        or normalized.endswith("observedat")
    )


def _reject_admin_derived_values(
    value: Any,
    path: str = "data",
    *,
    allow_nested_verified: bool = False,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            # Row-level verified checkboxes are part of the existing Courses
            # tab payload.  They remain harmless until the service receives
            # verify=true plus review evidence, which controls component
            # provenance; all other verified claims are client-derived state.
            if normalized == "verified" and allow_nested_verified:
                pass
            elif _is_admin_derived_key(key):
                raise ValueError(f"{path}.{key} is derived and cannot be supplied by an admin client")
            _reject_admin_derived_values(
                child,
                f"{path}.{key}",
                allow_nested_verified=allow_nested_verified or normalized in {
                    "restrictions",
                    "prerequisitegroups",
                    "replacements",
                },
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_admin_derived_values(
                child,
                f"{path}[{index}]",
                allow_nested_verified=allow_nested_verified,
            )


def validate_admin_course_data(value: dict[str, Any]) -> dict[str, Any]:
    """Validate browser supplied course fields and strip model defaults.

    Source ingestion calls the service directly and intentionally bypasses
    this boundary.  The HTTP admin surface uses it for both create and patch
    requests, so typed section/meeting validation and provenance protection
    are applied consistently without weakening the trusted importer path.
    """

    if not isinstance(value, dict):
        raise ValueError("Draft data must be an object")
    identity_keys = {
        "coursecode",
        "courseid",
        "term",
        "termid",
        "organizationid",
        "state",
        "revision",
        "baserevisionid",
        "publishedrevisionid",
        "draftid",
        "releaseid",
        "catalogreleaseid",
        "courserevisionid",
    }
    for key in value:
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized in identity_keys:
            raise ValueError(f"data.{key} is an identity field and cannot be changed")
    _reject_admin_derived_values(value)
    try:
        parsed = CoursePatch.model_validate(value)
    except ValidationError as exc:
        # Keep FastAPI's admin endpoint response in its existing simple 422
        # shape while preserving the useful Pydantic field path in the text.
        raise ValueError(str(exc)) from exc
    return parsed.model_dump(mode="json", exclude_unset=True)


class DraftCreateIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    term: str
    course_code: str
    base_revision_id: UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, min_length=3, max_length=1000)

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        return normalize_term(value)

    @field_validator("course_code")
    @classmethod
    def validate_course_code(cls, value: str) -> str:
        return normalize_course_code(value)

    def typed_data(self) -> dict[str, Any]:
        value = dict(self.data)
        known = {"term", "course_code", "base_revision_id", "data", "reason"}
        value.update({key: item for key, item in self.model_dump(exclude_none=True).items() if key not in known})
        return validate_admin_course_data(value)


class DraftPatchIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    expected_revision: int = Field(ge=1)
    patch: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=1000)
    verify: bool = False
    verification_evidence: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_patch(self) -> "DraftPatchIn":
        if not self.patch:
            raise ValueError("At least one draft field must be changed")
        if self.verify and not str(self.verification_evidence or "").strip():
            raise ValueError("Explicit verification requires source or review evidence")
        return self

    def typed_patch(self) -> dict[str, Any]:
        return validate_admin_course_data(dict(self.patch))


class PublishIn(BaseModel):
    term: str
    draft_ids: list[UUID] = Field(min_length=1, max_length=500)
    expected_release_id: UUID | None = None
    acknowledge_conflicts: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        return normalize_term(value)

    @model_validator(mode="after")
    def unique_drafts(self) -> "PublishIn":
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("Draft IDs must be unique")
        return self


class RollbackIn(BaseModel):
    term: str
    target_release_id: UUID
    expected_release_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        return normalize_term(value)


class ImportIn(BaseModel):
    term: str
    department: str | None = Field(default=None, max_length=32)
    course_codes: list[str] | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        return normalize_term(value)

    @field_validator("course_codes")
    @classmethod
    def validate_course_codes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result = [normalize_course_code(code) for code in value]
        if len(set(result)) != len(result):
            raise ValueError("Course codes must be unique")
        return result


class RemoveOverridesIn(BaseModel):
    expected_revision: int = Field(ge=1)
    fields: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=3, max_length=1000)


class CourseListQuery(BaseModel):
    term: str
    department: str | None = None
    query: str | None = None
    state: str | None = None
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=50, ge=1, le=500)

    @field_validator("term")
    @classmethod
    def validate_term(cls, value: str) -> str:
        return normalize_term(value)


# Response contracts for the admin surface.  The nested catalog payloads are
# deliberately permissive because source adapters may add fields, while the
# stable envelope and identifiers remain typed in OpenAPI and generated
# frontend clients.
class CatalogCourseRowOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    course_code: str
    #: "CENG 331" - the spelling on a timetable, a transcript and a door, as
    #: opposed to 5670331, which is the catalog's key and nobody's vocabulary.
    display_code: str | None = None
    department: str | None = None
    title: str | None = None
    local_credits: float | None = None
    ects: float | None = None
    level: str | None = None
    availability: str | None = None
    campus: str | None = None
    state: str
    completeness: dict[str, Any] = Field(default_factory=dict)
    freshness: str = "unknown"
    source_conflicts: bool | int | list[dict[str, Any]] = False
    draft_id: str | None = None
    course_revision_id: str | None = None
    section_count: int = 0
    catalog: dict[str, Any] = Field(default_factory=dict, alias="_catalog")


class CatalogCourseListOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    courses: list[CatalogCourseRowOut] = Field(default_factory=list)
    total: int = 0
    release_id: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    term: str | None = None


class CatalogCourseDetailOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    course_code: str
    term: str | None = None
    department: str | None = None
    title: str | None = None
    credits: float | None = None
    local_credits: float | None = None
    ects: float | None = None
    level: str | None = None
    availability: str | None = None
    campus: str | None = None
    is_thesis: bool = False
    state: str
    completeness: dict[str, Any] = Field(default_factory=dict)
    component_status: dict[str, Any] = Field(default_factory=dict)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    prerequisite_groups: list[dict[str, Any]] = Field(default_factory=list)
    replacements: list[dict[str, Any]] = Field(default_factory=list)
    source_observations: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    catalog: dict[str, Any] = Field(default_factory=dict, alias="_catalog")
    issues: list[dict[str, Any]] = Field(default_factory=list)
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    draft_id: str | None = None
    draft_revision: int | None = None
    draft: dict[str, Any] | None = None


class CatalogDraftOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    revision: int
    state: str
    term: str | None = None
    course_code: str | None = None
    term_id: str
    course_id: str
    base_revision_id: str | None = None
    published_revision_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CatalogImportJobOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    organization_id: str
    term: str
    department: str | None = None
    course_codes: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    lease_until: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    dedup_key: str
    reason: str | None = None
    priority: int = 50
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class CatalogImportsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    imports: list[CatalogImportJobOut] = Field(default_factory=list)
    total: int = 0


class CatalogReleaseOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    release_number: int | None = None
    operation: str | None = None
    reason: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    expected_release_id: str | None = None
    target_release_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    active: bool = False


class CatalogReleasesOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    releases: list[CatalogReleaseOut] = Field(default_factory=list)
    active_release_id: str | None = None
    term: str | None = None


class CatalogOperationOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    operation_id: str
    operation: str
    status: str
    term: str
    release_id: str | None = None
    target_release_id: str | None = None
    idempotency_key: str
    release_number: int | None = None
    published_draft_ids: list[str] = Field(default_factory=list)
    course_revision_ids: list[str] = Field(default_factory=list)
    course_count: int | None = None


class CatalogSourceObservationOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    candidate_data: dict[str, Any] = Field(default_factory=dict)
    status: str
    observed_at: Any = None
    source_fetched_at: Any = None
    parser_version: str | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "CatalogCourseDetailOut",
    "CatalogCourseListOut",
    "CatalogCourseRowOut",
    "CatalogDraftOut",
    "CatalogImportJobOut",
    "CatalogImportsOut",
    "CatalogOperationOut",
    "CatalogReleaseOut",
    "CatalogReleasesOut",
    "CatalogSourceObservationOut",
    "CourseListQuery",
    "CoursePatch",
    "DraftCreateIn",
    "DraftPatchIn",
    "ImportIn",
    "PublishIn",
    "RollbackIn",
    "normalize_course_code",
    "normalize_term",
]
