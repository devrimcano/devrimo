"""Keep only the two letters section eligibility actually compares.

METU states a section's surname restriction as a two-character range — "AA"-"İZ",
"DJ"-"KA" — so the comparison never reads past the surname's first two letters.
Storing the whole surname was therefore holding personal data the feature does
not use.

Renamed rather than dropped-and-added because the column is one migration old
and still NULL everywhere, so nothing is lost and the intent stays legible in
the schema: the field is a prefix, not a name.
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_surname_prefix"
down_revision = "0014_student_surname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("student_contexts", "surname", new_column_name="surname_prefix", type_=sa.String(8))


def downgrade() -> None:
    op.alter_column("student_contexts", "surname_prefix", new_column_name="surname", type_=sa.Text())
