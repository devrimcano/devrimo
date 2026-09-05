"""Add persistent schedule result cache.

Revision ID: 0013_schedule_data_cache
Revises: 0012_native_vector_dimensions
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_schedule_data_cache"
down_revision = "0012_native_vector_dimensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_data_cache",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_hash", sa.String(length=64), nullable=True),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index("ix_schedule_data_cache_expires", "schedule_data_cache", ["expires_at"])
    op.create_index("ix_schedule_data_cache_owner", "schedule_data_cache", ["owner_hash"])


def downgrade() -> None:
    op.drop_index("ix_schedule_data_cache_owner", table_name="schedule_data_cache")
    op.drop_index("ix_schedule_data_cache_expires", table_name="schedule_data_cache")
    op.drop_table("schedule_data_cache")
