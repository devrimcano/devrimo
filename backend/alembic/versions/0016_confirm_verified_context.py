"""Treat an existing SAIS-verified context as confirmed.

The academic context used to be stored verified-but-unconfirmed, and the
student was shown a "Confirm" prompt before anything would act on it. That
prompt asked them to agree with their own registrar, which is not a decision
anyone can make, and it left every student who ignored it in a half-state.

SAIS reads now confirm themselves. This backfills the rows written before that
change so they do not stay stuck behind a prompt that no longer exists.

Revision ID: 0016_confirm_verified_context
Revises: 0015_surname_prefix
"""

from alembic import op

revision = "0016_confirm_verified_context"
down_revision = "0015_surname_prefix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE student_contexts
           SET confirmed_at = verified_at
         WHERE verified_at IS NOT NULL
           AND confirmed_at IS NULL
        """
    )


def downgrade() -> None:
    # Not reversible in any useful sense: which rows were confirmed by a
    # student and which by this backfill is not recorded anywhere, and
    # clearing every confirmation would put students who really did press the
    # button back behind the prompt. Leaving the data alone is the honest
    # downgrade — the old code reads these rows correctly, it just shows a
    # prompt the student has effectively already answered.
    pass
