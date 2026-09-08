"""Version student plans per term, preserving the current projection."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0021_timetable_revisions"
down_revision = "0020_agno_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("student_timetables_pkey", "student_timetables", type_="primary")
    op.create_primary_key("student_timetables_pkey", "student_timetables", ["user_id", "term"])
    op.add_column("student_timetables", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "timetable_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("term", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "term", "revision", name="uq_timetable_revision"),
        sa.UniqueConstraint("user_id", "term", "idempotency_key", name="uq_timetable_idempotency"),
    )
    op.create_index(
        "ix_timetable_revisions_user_term_created", "timetable_revisions", ["user_id", "term", "created_at"]
    )


def downgrade():
    # Collapse only when lossless; multiple terms must never be silently lost.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM student_timetables GROUP BY user_id HAVING count(*) > 1)")
    ):
        raise RuntimeError("Cannot downgrade plans containing multiple terms per student")
    op.drop_table("timetable_revisions")
    op.drop_column("student_timetables", "revision")
    op.drop_constraint("student_timetables_pkey", "student_timetables", type_="primary")
    op.create_primary_key("student_timetables_pkey", "student_timetables", ["user_id"])
