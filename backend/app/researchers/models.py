"""Source identity is separate from localized academic content."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Researcher(Base):
    __tablename__ = "researchers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # METU AVESIS id
    network_id: Mapped[int | None] = mapped_column(BigInteger)
    alias: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    affiliation: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearcherSection(Base):
    __tablename__ = "researcher_sections"
    __table_args__ = (UniqueConstraint("researcher_id", "section", "language", name="uq_researcher_section_language"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    researcher_id: Mapped[int] = mapped_column(ForeignKey("researchers.id", ondelete="CASCADE"))
    section: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, default="en")
    source_url: Mapped[str] = mapped_column(Text)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearcherImportRun(Base):
    __tablename__ = "researcher_import_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(Text, default="running")
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    discovery: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearcherImportItem(Base):
    __tablename__ = "researcher_import_items"
    __table_args__ = (Index("ix_researcher_import_items_run_status", "run_id", "status"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("researcher_import_runs.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    identity: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="pending")
    outcome: Mapped[str | None] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    completed_sections: Mapped[list] = mapped_column(JSONB, default=list)
