"""Retire canonical-record embeddings after generation backfill."""

from alembic import op

revision = "0025_retire_legacy_vectors"
down_revision = "0024_resource_revisions"
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL removes indexes depending on these columns. The generation
    # migration already copied compatible vectors before this executes.
    for column in ("embedding_384", "embedding_768", "embedding_1536", "embedding_model"):
        op.drop_column("campus_knowledge_records", column)
    op.execute("REVOKE SELECT ON knowledge_embedding_settings FROM devrimo_knowledge")
    op.execute("DROP POLICY IF EXISTS devrimo_knowledge_access ON knowledge_embedding_settings")
    for table in ("campus_knowledge_records", "campus_sources"):
        op.execute(f"GRANT SELECT ON {table} TO devrimo_student")
        op.execute(f"CREATE POLICY devrimo_student_access ON {table} FOR SELECT TO devrimo_student USING (true)")


def downgrade():
    raise RuntimeError("Legacy embedding storage is retired; restore via generation export if needed")
