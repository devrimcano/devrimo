"""Fence stale academic reads from writing after erasure."""

import sqlalchemy as sa

from alembic import op

revision = "0031_academic_data_fence"
down_revision = "0030_database_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_directory",
        sa.Column("academic_data_fence", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("account_directory", "academic_data_fence")
