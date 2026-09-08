"""Stable resource vocabulary; upstream method names never become model tools."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ResourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ResourceKind
    key: str | None = Field(default=None, max_length=2048)
    department: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    program_type: str | None = Field(default=None, max_length=32)
    folder: str | None = Field(default=None, max_length=255)
    attachment: str | None = Field(default=None, max_length=255)
    term: str | None = Field(default=None, max_length=32)
    section: str | None = Field(default=None, max_length=32)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: ResourceRef
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
