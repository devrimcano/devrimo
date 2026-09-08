"""Close Data API metadata access and bound existing runtime logins."""

import sqlalchemy as sa

from alembic import op

revision = "0030_database_hardening"
down_revision = "0029_extension_schema_access"
branch_labels = None
depends_on = None

# API at 8+1 and seven workers at 2+1, plus one Agno connection in both
# API and assistant = 32. Catalog has two processes; assistant has two pools
# plus the API's Agno pool. Reserve capacity for Supabase and release tooling.
LOGIN_LIMITS = {"api": 9, "assistant": 5, "catalog": 6, "knowledge": 3, "embedding": 3,
                "researcher": 3, "directory": 3, "planning": 3, "student": 3}


def upgrade():
    db = op.get_bind()
    op.execute("ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.alembic_version FROM PUBLIC")
    for role in ("anon", "authenticated", "service_role"):
        if db.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}):
            op.execute(f'REVOKE ALL ON TABLE public.alembic_version FROM "{role}"')
            # Applies to objects created by this migration identity, not to
            # Supabase-managed schemas or unrelated object owners.
            op.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM "{role}"')
    for group, limit in LOGIN_LIMITS.items():
        rows = db.execute(sa.text("""
            SELECT rolname FROM pg_roles
            WHERE rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)
              AND pg_has_role(oid, :group, 'MEMBER')
        """), {"group": f"devrimo_{group}"})
        for (login,) in rows:
            quoted = db.dialect.identifier_preparer.quote(login)
            op.execute(f"ALTER ROLE {quoted} CONNECTION LIMIT {limit}")


def downgrade():
    # Application rollback must not reopen public mutation of release metadata.
    pass
