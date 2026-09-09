"""Keep the hot catalog-import cursor out of the growing JSONB plan."""

import sqlalchemy as sa

from alembic import op

revision = "0034_catalog_checkpoint_offset"
down_revision = "0033_academic_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalog_import_jobs",
        sa.Column("checkpoint_offset", sa.Integer(), server_default="0", nullable=False),
    )
    # Jobs created by 0033 stored their cursor in JSONB. Backfill the scalar
    # cursor once; the worker then advances only this column on ordinary steps.
    op.execute(
        sa.text(
            "UPDATE catalog_import_jobs "
            "SET checkpoint_offset = CASE "
            "  WHEN jsonb_typeof(checkpoint->'offset') = 'number' "
            "   THEN (checkpoint->>'offset')::integer "
            "  ELSE 0 END"
        )
    )


def downgrade() -> None:
    # Preserve the authoritative scalar cursor for a rollback to a worker
    # version that only understands the JSONB checkpoint.
    op.execute(
        sa.text(
            "UPDATE catalog_import_jobs "
            "SET checkpoint = jsonb_set(checkpoint, '{offset}', to_jsonb(checkpoint_offset), true)"
        )
    )
    op.drop_column("catalog_import_jobs", "checkpoint_offset")
