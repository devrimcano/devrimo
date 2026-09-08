"""Assistant-owned revision history for explicit, non-sensitive memories."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceMemoryMutation(Base):
    __tablename__ = "workspace_memory_mutations"
    __table_args__ = (
        UniqueConstraint("user_id", "revision", name="uq_workspace_memory_revision"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_workspace_memory_request"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    revision: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    before_content: Mapped[list] = mapped_column(JSONB)
    after_content: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceMailApproval(Base):
    """API-issued authority; assistant workers have SELECT only."""

    __tablename__ = "workspace_mail_approvals"
    __table_args__ = (UniqueConstraint("user_id", "run_id", "draft_digest", name="uq_mail_approval_action"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    run_id: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    draft_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
