"""Resolve Supabase-managed extensions without granting runtime schema writes."""

import sqlalchemy as sa

from alembic import op

revision = "0029_extension_schema_access"
down_revision = "0028_retire_turn_locks"
branch_labels = None
depends_on = None


def upgrade():
    if not op.get_bind().scalar(sa.text("SELECT to_regnamespace('extensions') IS NOT NULL")):
        return
    # Leave ownership and Supabase's existing extension/API grants intact.
    op.execute("REVOKE CREATE ON SCHEMA extensions FROM PUBLIC")
    for owner in (
        "api",
        "knowledge",
        "embedding",
        "researcher",
        "directory",
        "catalog",
        "assistant",
        "planning",
        "student",
    ):
        op.execute(f"GRANT USAGE ON SCHEMA extensions TO devrimo_{owner}")
        op.execute(f"REVOKE CREATE ON SCHEMA extensions FROM devrimo_{owner}")


def downgrade():
    # USAGE is harmless and may predate this migration; do not remove it.
    pass
