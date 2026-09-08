"""API-authenticated run requests and assistant-owned execution state."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AssistantRun(Base):
    __tablename__ = "assistant_runs"
    __table_args__ = (
        CheckConstraint("kind IN ('chat','confirmation')", name="ck_assistant_run_kind"),
        CheckConstraint(
            "status IN ('queued','running','paused','completed','failed','cancelled','interrupted')",
            name="ck_assistant_run_status",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_assistant_run_idempotency"),
        Index(
            "ix_assistant_one_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index("ix_assistant_runs_claim", "status", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    approval_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    agno_run_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AssistantRunEvent(Base):
    __tablename__ = "assistant_run_events"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assistant_runs.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
