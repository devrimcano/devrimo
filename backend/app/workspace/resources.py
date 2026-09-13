"""Stable resource vocabulary; upstream method names never become model tools."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResourceKind = Literal[
    "researcher",
    "my.academic_snapshot",
    "my.updates",
    "my.preferences",
    "my.memory",
    "my.update_state",
    "campus.knowledge",
    "campus.page",
    "catalog.department",
    "catalog.sections",
    "catalog.eligibility",
    "catalog.departments",
    "catalog.courses",
    "catalog.prerequisites",
    "catalog.replacements",
    "catalog.theses",
    "student.categories",
    "student.category_courses",
    "student.curriculum",
    "student.info",
    "student.transcript",
    "student.registered_schedule",
    "student.announcements",
    "class.courses",
    "class.announcements",
    "class.syllabus",
    "class.assignments",
    "class.labs",
    "mail.status",
    "mail.folders",
    "mail.messages",
    "mail.message",
    "mail.attachment",
    "planning.timetable",
    "planning.proposal",
    "planning.course_group",
]


# Only these kinds answer a text search. Everything else is read with a key,
# and WorkspaceService.search already said so - but only after the call had been
# made, so a model that tried `search catalog.courses` spent a whole turn
# discovering it could not. Restricting the search schema to this set makes the
# wrong call impossible instead of punishable.
SearchableKind = Literal[
    "researcher",
    "campus.knowledge",
    "catalog.departments",
    "catalog.department",
    "mail.messages",
]

# Kinds whose read needs `key` - a course code, message id, page url or
# preference key. The service answered "A course code is required" for these,
# again only after the call had gone through.
KEY_REQUIRED_KINDS = frozenset(
    {
        "catalog.sections",
        "catalog.eligibility",
        "catalog.prerequisites",
        "catalog.replacements",
        "catalog.theses",
        "catalog.department",
        "mail.message",
        "mail.attachment",
        "my.preferences",
        "my.update_state",
        "campus.page",
        "planning.course_group",
    }
)

_SCOPED_DESCRIPTION = (
    "Choose `kind`, then the field it needs. Course kinds take the code in `key` "
    '("EE 201" or 5670201) or a department code/abbreviation in `department`. `term` defaults to '
    "the active term."
)


class _ScopedRef(BaseModel):
    """Fields shared by the read and search references."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"description": _SCOPED_DESCRIPTION})

    key: str | None = Field(default=None, max_length=2048, description="Course code, message id, page url or key.")
    department: str | None = Field(default=None, max_length=255, description="Code (571) or abbreviation (CENG).")
    category: str | None = Field(default=None, max_length=255, description="Category id from student.categories.")
    program_type: str | None = Field(default=None, max_length=32)
    folder: str | None = Field(default=None, max_length=255, description="Mailbox folder, e.g. INBOX.")
    attachment: str | None = Field(default=None, max_length=255)
    term: str | None = Field(default=None, max_length=32, description="Defaults to the active term.")
    section: str | None = Field(default=None, max_length=32)
    expand: bool = Field(
        default=False,
        description="Set true only after the student asked for every item; never on the first read.",
    )


class ResourceRef(_ScopedRef):
    kind: ResourceKind = Field(description="Which resource.")

    @model_validator(mode="after")
    def _require_the_field_the_kind_needs(self):
        """Refuse a keyless read before it becomes a wasted tool call.

        The message names the field and shows a working example, because the
        model corrects itself from a schema it can read - not from a 422 it
        only sees after spending a turn.
        """
        if self.kind in KEY_REQUIRED_KINDS and not (self.key or "").strip():
            raise ValueError(
                f'kind "{self.kind}" needs "key" - e.g. {{"kind": "{self.kind}", "key": "5710331"}}'
            )
        return self


class SearchResource(_ScopedRef):
    kind: SearchableKind = Field(description="One of the searchable kinds.")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: SearchResource
    query: str = Field(default="", max_length=2000)
    limit: int = Field(default=10, ge=1, le=25)
    record_types: list[str] = Field(default_factory=list, max_length=25)
    starts_after: str | None = None
    starts_before: str | None = None


class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(min_length=1, max_length=1000)
    subject: str = Field(max_length=1000)
    body: str = Field(min_length=1, max_length=100000)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    body_html: str | None = Field(default=None, max_length=100000)
    reply_to: str | None = Field(default=None, max_length=1000)
    reply_to_message_id: str | None = Field(default=None, max_length=255)
    folder: str = Field(default="INBOX", max_length=255)


# Closed resource-to-adapter table, including all previously allowed reads.
UPSTREAM = {
    "student.info": ("sais", "get_student_info"),
    "student.transcript": ("sais", "get_transcript"),
    "student.registered_schedule": ("sais", "get_schedule"),
    "student.announcements": ("sais", "get_announcements"),
    "class.courses": ("odtuclass", "get_enrolled_courses"),
    "class.announcements": ("odtuclass", "get_course_announcements"),
    "class.syllabus": ("odtuclass", "get_course_syllabus"),
    "class.assignments": ("odtuclass", "get_upcoming_assignments"),
    "class.labs": ("odtuclass", "get_lab_recitation_info"),
    "mail.status": ("webmail", "get_mailbox_status"),
    "mail.folders": ("webmail", "list_folders"),
    "mail.messages": ("webmail", "list_emails"),
    "mail.message": ("webmail", "read_email"),
    "mail.attachment": ("webmail", "get_attachment"),
    "catalog.departments": ("course_info", "get_departments_and_semesters"),
    "catalog.courses": ("course_info", "list_program_courses"),
    "catalog.prerequisites": ("course_info", "get_course_prerequisites"),
    "catalog.replacements": ("course_info", "get_course_replacements"),
    "catalog.theses": ("course_info", "get_thesis_courses"),
    "student.categories": ("course_info", "get_student_course_categories"),
    "student.category_courses": ("course_info", "get_student_courses_by_category"),
    "student.curriculum": ("course_info", "get_student_curriculum"),
}


class PreferenceChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: dict | None


class UpdateStateChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool | None = None
    dismissed: bool | None = None
