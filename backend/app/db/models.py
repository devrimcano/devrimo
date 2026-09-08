import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentStatus(enum.StrEnum):
    provisioning = "provisioning"
    running = "running"
    stopped = "stopped"
    error = "error"
    destroying = "destroying"


class AccountStatus(enum.StrEnum):
    active = "active"
    suspended = "suspended"
    deletion_pending = "deletion_pending"
    deleted = "deleted"


class AdminRole(enum.StrEnum):
    super_admin = "super_admin"
    operator = "operator"
    campus_admin = "campus_admin"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccountDirectory(Base):
    __tablename__ = "account_directory"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'deletion_pending', 'deleted')",
            name="ck_account_directory_status",
        ),
        Index("ix_account_directory_org_status_created", "organization_id", "status", "created_at", "user_id"),
        Index("ix_account_directory_org_last_seen", "organization_id", "last_seen_at", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_normalized: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, native_enum=False, length=32), default=AccountStatus.active, nullable=False
    )
    auth_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Monotonically advances whenever derived academic data is erased. Any
    # campus read that started before the erase must match this fence before it
    # can write a fetched context or snapshot back to PostgreSQL.
    academic_data_fence: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminMembership(Base):
    __tablename__ = "admin_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('super_admin', 'operator', 'campus_admin')",
            name="ck_admin_memberships_role",
        ),
        Index("ix_admin_memberships_org_role", "organization_id", "role", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole, native_enum=False, length=32), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminAuditEvent(Base):
    __tablename__ = "admin_audit_events"
    __table_args__ = (
        Index("ix_admin_audit_created_id", "created_at", "id"),
        Index("ix_admin_audit_org_created", "organization_id", "created_at", "id"),
        Index("ix_admin_audit_target_created", "target_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    before_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentRuntimeSettings(Base):
    __tablename__ = "agent_runtime_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    legacy_history_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scholar_history_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learning_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Per single token, not per million — matches the PostHog $ai_*_token_price
    # properties these feed directly.
    input_token_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_token_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Agent(Base):
    """A user's entitlement to an agent, plus its last known health.

    Execution belongs to :mod:`app.assistant.worker`; durable requests and
    event sequences live separately from this entitlement. Conversation history
    lives in the same PostgreSQL database's Agno schema.
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True, nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, native_enum=False, length=32), default=AgentStatus.running, nullable=False
    )

    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chat_sessions: Mapped[list["ChatSession"]] = relationship(back_populates="agent", cascade="all, delete-orphan")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)

    # Agno's own session key. Kept distinct from ``id`` (which the client
    # mints) so the two can diverge without a migration.
    agno_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="chat_sessions")


class UserProfile(Base):
    """Per-user preferences and onboarding progress.

    Kept separate from :class:`CampusCredential` so the common read path — "has
    this student finished onboarding?" — never loads a row containing secrets.
    Rows are created lazily on first read; there is no signup hook to hang
    creation off, since identity lives in Supabase.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    mail_facts_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The step the student would land on if they reloaded mid-flow. Free-form
    # rather than an enum so the frontend can re-order the wizard without a
    # migration.
    onboarding_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def onboarding_completed(self) -> bool:
        return self.onboarding_completed_at is not None


class CampusCredential(Base):
    """One student's METU credentials, encrypted at rest.

    These exist because the campus MCP servers authenticate to METU as the
    student — there is no delegated-token flow at student.metu.edu.tr to use
    instead. Each server is spawned with only its own entry's credentials in
    its process environment; see :mod:`app.campus.mcp_config`. The password is
    Fernet-encrypted with ``SECRET_ENCRYPTION_KEY``
    (see ``app/core/crypto.py``), is decrypted only in
    ``app/campus/credentials.py``, and is never included in any response
    schema.
    """

    __tablename__ = "campus_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True, nullable=False)

    metu_username: Mapped[str] = mapped_column(String(255), nullable=False)
    metu_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Optional alternative to the password for ODTUClass specifically: a Moodle
    # web service token the student can mint and revoke themselves.
    odtuclass_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    odtuclass_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    locale: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    enabled_tools: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the credentials change, cleared once the resident agent has
    # been rebuilt with the new toolset. Lets the UI say "restart to apply"
    # instead of silently serving a stale toolset.
    config_dirty: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Monotonic generation checked by every broker replica. Tool ids alone do
    # not change when a password or token is rotated, so they cannot be used as
    # a cache key for credential-bearing MCP subprocesses.
    credential_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentToolAudit(Base):
    """Minimal audit trail for external mutations; never stores message bodies."""

    __tablename__ = "agent_tool_audit"
    __table_args__ = (Index("ix_agent_tool_audit_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # No separate index here: this table is insert-heavy and
    # ``ix_agent_tool_audit_user_created`` already serves user_id-only lookups
    # on its leading column, so a second one would only cost write amplification.
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # SHA-256 of canonicalized arguments supports incident correlation without
    # retaining recipients, subjects, bodies, or campus records.
    argument_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CampusSource(Base):
    """Admin-configured public or curated campus information source."""

    __tablename__ = "campus_sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('drupal', 'html_page', 'html_table', 'rss', 'ical', 'json', 'pdf', "
            "'approved_social', 'email_facts', 'curated')",
            name="ck_campus_sources_kind",
        ),
        CheckConstraint("status IN ('draft', 'published', 'disabled')", name="ck_campus_sources_status"),
        Index("ix_campus_sources_org_status", "organization_id", "status", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    authority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    audience: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    schedule_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    active_revision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CampusSourceRevision(Base):
    __tablename__ = "campus_source_revisions"
    __table_args__ = (
        UniqueConstraint("source_id", "revision", name="uq_campus_source_revisions_source_revision"),
        CheckConstraint("status IN ('draft', 'valid', 'published', 'rejected')", name="ck_source_revision_status"),
        Index("ix_source_revisions_source_created", "source_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campus_sources.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    validation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeEmbeddingSettings(Base):
    """Per-organization embedding provider configuration.

    API keys are encrypted. Supported native vector widths each have their own
    HNSW index, avoiding zero padding while retaining indexed ANN retrieval.
    """

    __tablename__ = "knowledge_embedding_settings"
    __table_args__ = (
        CheckConstraint("provider IN ('disabled', 'local', 'remote')", name="ck_embedding_settings_provider"),
        CheckConstraint("dimensions IN (384, 768, 1536)", name="ck_embedding_settings_dimensions"),
        CheckConstraint("batch_size BETWEEN 1 AND 128", name="ck_embedding_settings_batch_size"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(16), default="disabled", nullable=False)
    model: Mapped[str] = mapped_column(Text, default="text-embedding-3-small", nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimensions: Mapped[int] = mapped_column(Integer, default=1536, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=32, nullable=False)
    # Retrieval models are trained asymmetrically: a short question and the
    # passage that answers it are embedded with different instructions. The
    # exact strings are provider-specific ("query: "/"passage: " for E5-family
    # models, task-type wording for others), so they stay admin-editable.
    query_prefix: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    document_prefix: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CampusIngestionJob(Base):
    __tablename__ = "campus_ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'leased', 'completed', 'failed', 'dead')", name="ck_ingestion_jobs_status"
        ),
        CheckConstraint("kind IN ('ingest', 'reembed')", name="ck_ingestion_jobs_kind"),
        CheckConstraint(
            "phase IN ('queued', 'fetching', 'parsing', 'embedding', 'storing', 'completed', 'failed')",
            name="ck_ingestion_jobs_phase",
        ),
        Index("ix_ingestion_jobs_claim", "status", "available_at", "created_at"),
        Index("ix_ingestion_jobs_source_created", "source_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campus_sources.id", ondelete="CASCADE"), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campus_source_revisions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), default="ingest", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    phase: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    total_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedded_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampusKnowledgeRecord(Base):
    """Canonical public fact. Only this table is eligible for embeddings."""

    __tablename__ = "campus_knowledge_records"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_knowledge_records_source_external"),
        CheckConstraint(
            "record_type IN ('announcement', 'calendar', 'event', 'service_status', 'guide', 'course', 'policy')",
            name="ck_knowledge_records_type",
        ),
        Index("ix_knowledge_records_source_current", "source_id", "is_current"),
        Index("ix_knowledge_records_source_revision", "source_revision_id"),
        Index(
            "ix_knowledge_records_url_current",
            "url",
            "source_id",
            postgresql_where=text("is_current"),
        ),
        Index("ix_knowledge_records_type_dates", "record_type", "starts_at", "ends_at"),
        Index("ix_knowledge_records_audience", "campus", "department", "degree_level"),
        Index("ix_knowledge_records_published", "published_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campus_sources.id", ondelete="CASCADE"), nullable=False)
    source_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campus_source_revisions.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    campus: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    degree_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audience: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StudentContext(Base):
    __tablename__ = "student_contexts"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only the first two letters, because that is the whole of what a section's
    # eligibility range compares: METU writes those bounds as two characters
    # ("AA"-"İZ"), so the rest of the surname is personal data the feature has
    # no use for. Read from the same SAIS student card the department comes from.
    surname_prefix: Mapped[str | None] = mapped_column(String(8), nullable=True)
    degree_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Year of study, 1-9. A section's eligibility row carries a min/max year
    # band, and that band is compared against this — the registrar's own number
    # from the SAIS student card, not the number of years since enrolment.
    year_of_study: Mapped[int | None] = mapped_column(Integer, nullable=True)
    program_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    campus: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StudentTimetable(Base):
    """The timetable a student built in the planner, so chat can talk about it.

    Not the schedule SAIS holds — that is what they are *registered* for, and
    it answers a different question. This is the week they are putting
    together for the term ahead, which is the thing they actually ask about,
    and it lived only in one browser's localStorage until now.

    Stored as a compact projection rather than the planner's whole state: the
    courses, their sections and when they meet. The pool, the alternatives and
    the cached section lists are working state, are far larger, and would be
    injected into every chat turn for nothing.
    """

    __tablename__ = "student_timetables"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # {"courses": [{code, name, section, credits, instructor, meetings: [...]}],
    #  "busy_blocks": [{name, meetings: [...]}]}
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_preferences_user_key"),
        CheckConstraint("provenance IN ('explicit', 'learned')", name="ck_user_preferences_provenance"),
        Index("ix_user_preferences_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    provenance: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserUpdateState(Base):
    __tablename__ = "user_update_states"
    __table_args__ = (Index("ix_user_update_states_user_updated", "user_id", "updated_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campus_knowledge_records.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StudentAcademicSnapshot(Base):
    """Sensitive planning inputs; deliberately separate and never embedded."""

    __tablename__ = "student_academic_snapshots"
    __table_args__ = (Index("ix_academic_snapshots_user_fetched", "user_id", "fetched_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(32), primary_key=True)
    completed_courses: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    enrolled_courses: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    current_credits: Mapped[float] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    current_grade_points: Mapped[float] = mapped_column(Numeric(10, 3), default=0, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="sais", nullable=False)


class ScheduleDataCache(Base):
    """Expiring schedule result cache. Keys and owners are one-way hashes."""

    __tablename__ = "schedule_data_cache"
    __table_args__ = (
        Index("ix_schedule_data_cache_expires", "expires_at"),
        Index("ix_schedule_data_cache_owner", "owner_hash"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CourseOffering(Base):
    __tablename__ = "course_offerings"
    __table_args__ = (
        UniqueConstraint("term", "course_code", "section", name="uq_course_offerings_term_course_section"),
        Index("ix_course_offerings_term_course", "term", "course_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    term: Mapped[str] = mapped_column(String(32), nullable=False)
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    section: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    credits: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    schedule: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    campus: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CourseRule(Base):
    __tablename__ = "course_rules"

    course_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    prerequisites: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    exclusions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    catalog_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlanningPolicy(Base):
    __tablename__ = "planning_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", "revision", name="uq_planning_policies_org_name_revision"),
        Index("ix_planning_policies_org_active", "organization_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CourseGroupLink(Base):
    __tablename__ = "course_group_links"
    __table_args__ = (
        UniqueConstraint("organization_id", "course_code", "section", name="uq_course_group_links_course_section"),
        Index("ix_course_group_links_lookup", "organization_id", "course_code", "section", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    section: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    invite_url_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    eligibility: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CourseGroupAccessAudit(Base):
    __tablename__ = "course_group_access_audit"
    __table_args__ = (Index("ix_course_group_access_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserMailFact(Base):
    """Opt-in structured mail fact. Raw message bodies are never stored."""

    __tablename__ = "user_mail_facts"
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_user_mail_facts_user_external"),
        Index("ix_user_mail_facts_user_dates", "user_id", "starts_at", "valid_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sender_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# Register the public academic directory with the shared Alembic metadata.
from app.knowledge.index_models import (  # noqa: E402,F401
    KnowledgeIndexActivation,
    KnowledgeIndexGeneration,
    KnowledgeIndexJob,
    KnowledgeIndexVector,
)
from app.researchers.models import (  # noqa: E402,F401
    Researcher,
    ResearcherImportItem,
    ResearcherImportRun,
    ResearcherSection,
)


class StudentResourceRevision(Base):
    """Revision history and idempotency for student-owned editable resources."""

    __tablename__ = "student_resource_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "resource", "idempotency_key", name="uq_student_resource_idempotency"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    resource: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


from app.workspace.models import WorkspaceMemoryMutation  # noqa: E402,F401


class StudentTimetableRevision(Base):
    """Resulting plan snapshots and replay keys per student and term."""

    __tablename__ = "timetable_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "term", "revision", name="uq_timetable_revision"),
        UniqueConstraint("user_id", "term", "idempotency_key", name="uq_timetable_idempotency"),
        Index("ix_timetable_revisions_user_term_created", "user_id", "term", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    term: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


from app.assistant.models import AssistantRun, AssistantRunEvent  # noqa: E402,F401
