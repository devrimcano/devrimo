"""Store the student's year of study.

A section's eligibility table carries a min/max year band, and until now
nothing was compared against it: the comparator accepted a ``year`` argument
that no caller ever passed, so a section restricted to second-years was
reported as open to everyone whose department was listed.

Revision ID: 0017_student_year_of_study
Revises: 0016_confirm_verified_context
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_student_year_of_study"
down_revision = "0016_confirm_verified_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("student_contexts", sa.Column("year_of_study", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("student_contexts", "year_of_study")
