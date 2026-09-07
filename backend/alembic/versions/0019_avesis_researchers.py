"""Public AVESIS researcher directory and resumable imports."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0019_avesis_researchers"
down_revision = "0018_student_timetable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researchers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("network_id", sa.BigInteger()),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("affiliation", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("photo_url", sa.Text()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "researcher_sections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "researcher_id", sa.BigInteger(), sa.ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("researcher_id", "section", "language", name="uq_researcher_section_language"),
    )
    op.create_table(
        "researcher_import_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column("discovery", JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "researcher_import_items",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("researcher_import_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("identity", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text()),
        sa.Column("errors", JSONB(), nullable=False),
        sa.Column("completed_sections", JSONB(), nullable=False),
    )
    op.create_index("ix_researcher_import_items_run_status", "researcher_import_items", ["run_id", "status"])


def downgrade() -> None:
    for table in ("researcher_import_items", "researcher_import_runs", "researcher_sections", "researchers"):
        op.drop_table(table)
