"""Rate-limit controls on the runtime settings row, off by default.

The mechanism lives in ``app.core.rate_limit`` and the admin panel writes
these columns; this migration only carries the switch and the two budgets.
NULL means "use the environment default", which is what every deployment has
until someone flips the switch, so the upgrade changes no behaviour.
"""

import sqlalchemy as sa

from alembic import op

revision = "0040_rate_limit_settings"
down_revision = "0039_legacy_purge"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agent_runtime_settings") as batch:
        batch.add_column(sa.Column("rate_limit_enabled", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("rate_limit_chat_per_minute", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("rate_limit_catalog_per_minute", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("agent_runtime_settings") as batch:
        batch.drop_column("rate_limit_catalog_per_minute")
        batch.drop_column("rate_limit_chat_per_minute")
        batch.drop_column("rate_limit_enabled")
