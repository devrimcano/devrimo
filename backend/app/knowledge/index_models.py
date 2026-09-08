"""Rebuildable semantic indexes, separate from authoritative campus content.

Administrators own immutable generation configurations and the active pointer.
The embedding worker owns vectors and job progress only.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KnowledgeIndexGeneration(Base):
    __tablename__ = "knowledge_index_generations"
    __table_args__ = (
        CheckConstraint("provider IN ('disabled', 'local', 'remote')", name="ck_index_provider"),
        CheckConstraint("dimensions IN (384, 768, 1536)", name="ck_index_dimensions"),
        CheckConstraint("batch_size BETWEEN 1 AND 128", name="ck_index_batch_size"),
        Index("ix_index_generation_org", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    batch_size: Mapped[int] = mapped_column(Integer)
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    query_prefix: Mapped[str] = mapped_column(Text, default="", server_default="")
    document_prefix: Mapped[str] = mapped_column(Text, default="", server_default="")
    model_label: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeIndexActivation(Base):
    __tablename__ = "knowledge_index_activations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_index_generations.id"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeIndexVector(Base):
    __tablename__ = "knowledge_index_vectors"
    __table_args__ = tuple(
        Index(
            f"ix_index_vector_{width}_hnsw",
            f"embedding_{width}",
            postgresql_using="hnsw",
            postgresql_ops={f"embedding_{width}": "vector_cosine_ops"},
            postgresql_where=text(f"embedding_{width} IS NOT NULL"),
        )
        for width in (384, 768, 1536)
    ) + (Index("ix_index_vector_record", "record_id"),)

    generation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_index_generations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campus_knowledge_records.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    embedding_384: Mapped[list[float] | None] = mapped_column(Vector(384))
    embedding_768: Mapped[list[float] | None] = mapped_column(Vector(768))
    embedding_1536: Mapped[list[float] | None] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeIndexJob(Base):
    __tablename__ = "knowledge_index_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_index_job_status"),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_index_generations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    lease_owner: Mapped[str | None] = mapped_column(Text)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
