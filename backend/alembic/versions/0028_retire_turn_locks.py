"""Durable run leases replace the obsolete API-resident agent turn lock."""

from alembic import op

revision = "0028_retire_turn_locks"
down_revision = "0027_mail_approvals"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("agents", "turn_lock_until")
    op.drop_column("agents", "turn_lock_owner")


def downgrade():
    raise RuntimeError("API-resident execution is retired; durable runs own concurrency")
