"""Store the timetable a student builds in the planner.

It lived only in one browser's localStorage, so the assistant could not answer
"is my Tuesday free" about the very schedule the student was building in the
next tab. The SAIS schedule was reachable and is not the same thing: that is
what they are already registered for.

Revision ID: 0018_student_timetable
Revises: 0017_student_year_of_study
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_student_timetable"
down_revision = "0017_student_year_of_study"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_timetables",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("term", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("student_timetables")
