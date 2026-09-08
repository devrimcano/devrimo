"""Record canonical request payloads for timetable idempotency."""

import sqlalchemy as sa

from alembic import op

revision = "0032_timetable_request_digest"
down_revision = "0031_academic_data_fence"
branch_labels = None
depends_on = None

# Rows written before the digest existed cannot be reconstructed from their
# resulting snapshot. Marking them with this value makes retries fail closed;
# only requests written after this migration can be safely replayed.
LEGACY_REQUEST_DIGEST = "0" * 64


def upgrade() -> None:
    op.add_column(
        "timetable_revisions",
        sa.Column("request_digest", sa.String(length=64), nullable=True),
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE timetable_revisions "
            "SET request_digest = :legacy "
            "WHERE request_digest IS NULL"
        ),
        {"legacy": LEGACY_REQUEST_DIGEST},
    )
    op.alter_column("timetable_revisions", "request_digest", nullable=False)


def downgrade() -> None:
    op.drop_column("timetable_revisions", "request_digest")
