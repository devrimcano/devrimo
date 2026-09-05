"""Store the student's surname alongside the rest of their SAIS context.

METU restricts sections by surname range, so the schedule planner has to know
it to tell a student which sections they can actually register for. It was
previously a box they had to type into by hand, which meant the restriction was
enforced for almost nobody — SAIS already reports it on the same student card
the department comes from.

Nullable with no backfill: it fills on each student's next context sync.
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_student_surname"
down_revision = "0013_schedule_data_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("student_contexts", sa.Column("surname", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("student_contexts", "surname")
