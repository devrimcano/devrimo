"""Relational, revisioned academic catalog models.

The catalog is deliberately kept separate from the student's SAIS snapshot and
from the legacy ``course_offerings`` cache.  A source observation is evidence;
only a revision referenced by a catalog release is student visible.  The
models in this module are imported by :mod:`app.db.models` at application
startup so Alembic and runtime workers see the same metadata.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _json_object_default() -> dict:
    return {}


def _json_array_default() -> list:
    return []


class CatalogCourse(Base):
    """A stable organization-owned course identity.

    Title, credits and rules belong to a term revision.  Keeping the identity
    stable lets a release replace one offering without changing saved student
    references or aliases.
    """

    __tablename__ = "catalog_courses"
    __table_args__ = (
        UniqueConstraint("organization_id", "course_code", name="uq_catalog_courses_org_code"),
        Index("ix_catalog_courses_org_department_code", "organization_id", "department", "course_code"),
        Index("ix_catalog_courses_org_code", "organization_id", "course_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str] = mapped_column(String(32), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, default=_json_array_default, server_default=text("'[]'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CatalogTerm(Base):
    """An organization-scoped academic term discovered from the source."""

    __tablename__ = "catalog_terms"
    __table_args__ = (
        UniqueConstraint("organization_id", "term_code", name="uq_catalog_terms_org_code"),
        Index("ix_catalog_terms_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term_code: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CatalogCourseRevision(Base):
    """Immutable once published; drafts are materialized as new revisions."""

    __tablename__ = "catalog_course_revisions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_catalog_course_revisions_revision_positive"),
        CheckConstraint("state IN ('draft', 'published', 'retired')", name="ck_catalog_course_revisions_state"),
        UniqueConstraint("course_id", "term_id", "revision", name="uq_catalog_course_revisions_course_term_revision"),
        Index("ix_catalog_course_revisions_org_term_state", "organization_id", "term_id", "state", "revision"),
        Index("ix_catalog_course_revisions_course_term", "course_id", "term_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_courses.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft", nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_credits: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    ects: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    availability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    campus: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_thesis: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    completeness: Mapped[dict] = mapped_column(
        JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False
    )
    component_status: Mapped[dict] = mapped_column(
        JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False
    )
    issues: Mapped[list] = mapped_column(
        JSONB, default=_json_array_default, server_default=text("'[]'::jsonb"), nullable=False
    )
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_source_observations.id", ondelete="SET NULL"), nullable=True
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CatalogSection(Base):
    __tablename__ = "catalog_sections"
    __table_args__ = (
        UniqueConstraint("course_revision_id", "section_code", name="uq_catalog_sections_revision_code"),
        Index("ix_catalog_sections_org_revision", "organization_id", "course_revision_id", "section_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="CASCADE"), nullable=False
    )
    section_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="listed", server_default="listed", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    syllabus_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    syllabus_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    meetings_status: Mapped[str] = mapped_column(
        String(32), default="unknown", server_default="unknown", nullable=False
    )
    restrictions_status: Mapped[str] = mapped_column(
        String(32), default="unknown", server_default="unknown", nullable=False
    )
    restrictions_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restrictions_source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_source_observations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CatalogMeeting(Base):
    __tablename__ = "catalog_meetings"
    __table_args__ = (
        CheckConstraint("weekday IS NULL OR (weekday BETWEEN 0 AND 6)", name="ck_catalog_meetings_weekday"),
        CheckConstraint(
            "start_minute IS NULL OR (start_minute BETWEEN 0 AND 1439)", name="ck_catalog_meetings_start_minute"
        ),
        CheckConstraint(
            "end_minute IS NULL OR (end_minute BETWEEN 1 AND 1440)", name="ck_catalog_meetings_end_minute"
        ),
        CheckConstraint(
            "(start_minute IS NULL AND end_minute IS NULL) OR (start_minute IS NOT NULL AND end_minute IS NOT NULL AND end_minute > start_minute)",
            name="ck_catalog_meetings_time_range",
        ),
        CheckConstraint(
            "status <> 'scheduled' OR (weekday IS NOT NULL AND start_minute IS NOT NULL AND end_minute IS NOT NULL AND end_minute > start_minute)",
            name="ck_catalog_meetings_scheduled_complete",
        ),
        Index("ix_catalog_meetings_org_section", "organization_id", "section_id", "weekday", "start_minute"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_sections.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    room: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", server_default="scheduled", nullable=False)
    raw_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogInstructor(Base):
    __tablename__ = "catalog_instructors"
    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_name", "position", name="uq_catalog_instructors_org_name_position"),
        Index("ix_catalog_instructors_org_name", "organization_id", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A nullable FK keeps a source-name snapshot useful while ensuring a
    # resolved instructor can never point at a deleted/nonexistent AVESIS
    # researcher.  The section link stores the match metadata separately so a
    # later global researcher update cannot rewrite an already published
    # identity.
    researcher_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("researchers.id", ondelete="SET NULL"), nullable=True
    )
    resolution_status: Mapped[str] = mapped_column(
        String(32), default="unresolved", server_default="unresolved", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CatalogSectionInstructor(Base):
    __tablename__ = "catalog_section_instructors"
    __table_args__ = (
        UniqueConstraint("section_id", "instructor_id", name="uq_catalog_section_instructors_section_instructor"),
        Index("ix_catalog_section_instructors_org_section", "organization_id", "section_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_sections.id", ondelete="CASCADE"), nullable=False
    )
    instructor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_instructors.id", ondelete="CASCADE"), nullable=False
    )
    # Snapshot the resolved AVESIS id on the immutable section link.  The
    # instructor directory is mutable and a later rematch must not rewrite a
    # published course revision's historical identity.
    researcher_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    match_method: Mapped[str] = mapped_column(String(32), default="unmatched", server_default="unmatched", nullable=False)
    resolution_status: Mapped[str] = mapped_column(
        String(32), default="unresolved", server_default="unresolved", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogRestriction(Base):
    __tablename__ = "catalog_restrictions"
    __table_args__ = (
        Index("ix_catalog_restrictions_org_section", "organization_id", "section_id", "kind"),
        Index("ix_catalog_restrictions_org_revision", "organization_id", "course_revision_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_sections.id", ondelete="CASCADE"), nullable=True
    )
    restriction_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(16), nullable=True)
    minimum_grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    given_department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_char: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_char: Mapped[str | None] = mapped_column(String(16), nullable=True)
    min_cgpa: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    max_cgpa: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    min_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_grade: Mapped[str | None] = mapped_column(String(128), nullable=True)
    end_grade: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prior_course_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    program_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    curriculum_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", server_default="unknown", nullable=False)
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_source_observations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogPrerequisiteGroup(Base):
    __tablename__ = "catalog_prerequisite_groups"
    __table_args__ = (
        UniqueConstraint(
            "course_revision_id", "group_no", "program_code", "curriculum_version",
            name="uq_catalog_prerequisite_groups_revision_group_program",
        ),
        Index("ix_catalog_prerequisite_groups_org_revision", "organization_id", "course_revision_id", "group_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="CASCADE"), nullable=False
    )
    group_no: Mapped[int] = mapped_column(Integer, nullable=False)
    logic: Mapped[str] = mapped_column(String(16), default="AND", server_default="AND", nullable=False)
    program_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    curriculum_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applicability: Mapped[dict] = mapped_column(
        JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_source_observations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogPrerequisiteRequirement(Base):
    __tablename__ = "catalog_prerequisite_requirements"
    __table_args__ = (
        Index("ix_catalog_prerequisite_requirements_org_group", "organization_id", "group_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_prerequisite_groups.id", ondelete="CASCADE"), nullable=False
    )
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    minimum_grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    requirement_type: Mapped[str] = mapped_column(String(16), default="course", server_default="course", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogCourseReplacement(Base):
    __tablename__ = "catalog_course_replacements"
    __table_args__ = (
        UniqueConstraint(
            "course_revision_id", "relationship_type", "related_course_code", "program_code", "curriculum_version",
            name="uq_catalog_course_replacements_revision_related",
        ),
        Index("ix_catalog_course_replacements_org_revision", "organization_id", "course_revision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    related_course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    program_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    curriculum_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_source_observations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogImportJob(Base):
    """Durable, resumable source work item."""

    __tablename__ = "catalog_import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_catalog_import_jobs_status",
        ),
        Index("ix_catalog_import_jobs_org_status_created", "organization_id", "status", "created_at"),
        Index("ix_catalog_import_jobs_lease", "status", "lease_until"),
        Index(
            "uq_catalog_import_jobs_active_dedup",
            "organization_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    course_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", server_default="queued", nullable=False)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    # The operation plan can grow to thousands of source calls. Keep the hot
    # resume cursor in a scalar column so advancing one step does not rewrite
    # the full JSONB plan/TOAST value.
    checkpoint_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Fencing token assigned whenever a worker claims/reclaims a job.  It is
    # intentionally private and omitted from API serialization; worker writes
    # must include it so an expired worker cannot checkpoint a reclaimed job.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, server_default="50", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CatalogHttpBudget(Base):
    """Cross-worker request admission state for the source HTTP budget."""

    __tablename__ = "catalog_http_budgets"
    __table_args__ = (Index("ix_catalog_http_budgets_org_next", "organization_id", "next_request_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    budget_date: Mapped[date] = mapped_column(Date, primary_key=True)
    attempted_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=200, server_default="200", nullable=False)
    min_interval_seconds: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), default=Decimal("20"), server_default="20", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CatalogSourceObservation(Base):
    """Retained raw source evidence and normalized candidate data."""

    __tablename__ = "catalog_source_observations"
    __table_args__ = (
        Index("ix_catalog_source_observations_org_scope", "organization_id", "term", "course_code", "tool", "observed_at"),
        Index("ix_catalog_source_observations_hash", "organization_id", "payload_hash"),
        Index("ix_catalog_source_observations_job", "job_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_import_jobs.id", ondelete="SET NULL"), nullable=True
    )
    tool: Mapped[str] = mapped_column(String(96), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    term: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    course_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    section_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="1", server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issues: Mapped[list] = mapped_column(JSONB, default=_json_array_default, server_default=text("'[]'::jsonb"), nullable=False)
    candidate_data: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogRelease(Base):
    """Immutable set of revisions exposed to student-facing readers."""

    __tablename__ = "catalog_releases"
    __table_args__ = (
        CheckConstraint("release_number > 0", name="ck_catalog_releases_number_positive"),
        UniqueConstraint("organization_id", "term_id", "release_number", name="uq_catalog_releases_org_term_number"),
        UniqueConstraint(
            "organization_id",
            "term_id",
            "operation",
            "idempotency_key",
            name="uq_catalog_releases_org_term_operation_idempotency",
        ),
        Index("ix_catalog_releases_org_term_created", "organization_id", "term_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), nullable=False
    )
    release_number: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), default="publish", server_default="publish", nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogReleaseItem(Base):
    __tablename__ = "catalog_release_items"
    __table_args__ = (
        UniqueConstraint("release_id", "course_revision_id", name="uq_catalog_release_items_release_revision"),
        Index("ix_catalog_release_items_org_course", "organization_id", "course_id", "release_id"),
        Index("ix_catalog_release_items_org_code", "organization_id", "course_code", "release_id"),
    )

    release_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_releases.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_courses.id", ondelete="CASCADE"), primary_key=True
    )
    course_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogTermActiveRelease(Base):
    __tablename__ = "catalog_term_active_releases"
    __table_args__ = (Index("ix_catalog_term_active_releases_org_release", "organization_id", "release_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), primary_key=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_releases.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CatalogDraft(Base):
    """Mutable, optimistic-concurrency controlled administrator candidate."""

    __tablename__ = "catalog_drafts"
    __table_args__ = (
        CheckConstraint("state IN ('draft', 'published', 'discarded')", name="ck_catalog_drafts_state"),
        CheckConstraint("revision > 0", name="ck_catalog_drafts_revision_positive"),
        Index(
            "uq_catalog_drafts_active_course",
            "organization_id",
            "term_id",
            "course_id",
            unique=True,
            postgresql_where=text("state = 'draft'"),
        ),
        Index("ix_catalog_drafts_org_term_state_updated", "organization_id", "term_id", "state", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_courses.id", ondelete="CASCADE"), nullable=False
    )
    base_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="SET NULL"), nullable=True
    )
    published_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_course_revisions.id", ondelete="SET NULL"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft", nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    issues: Mapped[list] = mapped_column(JSONB, default=_json_array_default, server_default=text("'[]'::jsonb"), nullable=False)
    field_overrides: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CatalogAdminOverride(Base):
    __tablename__ = "catalog_admin_overrides"
    __table_args__ = (
        Index("ix_catalog_admin_overrides_org_draft_active", "organization_id", "draft_id", "active"),
        Index("ix_catalog_admin_overrides_org_course_field", "organization_id", "course_id", "field_name", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_courses.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_drafts.id", ondelete="SET NULL"), nullable=True
    )
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogPublicationOperation(Base):
    __tablename__ = "catalog_publication_operations"
    __table_args__ = (
        CheckConstraint("operation IN ('publish', 'rollback')", name="ck_catalog_publication_operations_operation"),
        UniqueConstraint(
            "organization_id", "term_id", "operation", "idempotency_key",
            name="uq_catalog_publication_operations_org_term_operation_key",
        ),
        Index("ix_catalog_publication_operations_org_term_created", "organization_id", "term_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("catalog_terms.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    result_release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed", server_default="completed", nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, default=_json_object_default, server_default=text("'{}'::jsonb"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "CatalogAdminOverride",
    "CatalogCourse",
    "CatalogCourseReplacement",
    "CatalogCourseRevision",
    "CatalogDraft",
    "CatalogHttpBudget",
    "CatalogImportJob",
    "CatalogInstructor",
    "CatalogMeeting",
    "CatalogPrerequisiteGroup",
    "CatalogPrerequisiteRequirement",
    "CatalogPublicationOperation",
    "CatalogRelease",
    "CatalogReleaseItem",
    "CatalogRestriction",
    "CatalogSection",
    "CatalogSectionInstructor",
    "CatalogSourceObservation",
    "CatalogTerm",
    "CatalogTermActiveRelease",
]
