"""Idempotent histories for student settings and assistant memory."""

from alembic import op

revision = "0024_resource_revisions"
down_revision = "0023_runtime_ownership"
branch_labels = None
depends_on = None

DDL = [
    "\n"
    "CREATE TABLE student_resource_revisions (\n"
    "\tuser_id UUID NOT NULL, \n"
    "\tresource VARCHAR(128) NOT NULL, \n"
    "\trevision INTEGER NOT NULL, \n"
    "\tidempotency_key VARCHAR(128) NOT NULL, \n"
    "\trequest_hash VARCHAR(64) NOT NULL, \n"
    "\tpayload JSON NOT NULL, \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (user_id, resource, revision), \n"
    "\tCONSTRAINT uq_student_resource_idempotency UNIQUE (user_id, resource, idempotency_key)\n"
    ")\n"
    "\n",
    "\n"
    "CREATE TABLE workspace_memory_mutations (\n"
    "\tid UUID NOT NULL, \n"
    "\tuser_id UUID NOT NULL, \n"
    "\trevision INTEGER NOT NULL, \n"
    "\tidempotency_key VARCHAR(128) NOT NULL, \n"
    "\trequest_digest VARCHAR(64) NOT NULL, \n"
    "\tbefore_content JSONB NOT NULL, \n"
    "\tafter_content JSONB NOT NULL, \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_workspace_memory_revision UNIQUE (user_id, revision), \n"
    "\tCONSTRAINT uq_workspace_memory_request UNIQUE (user_id, idempotency_key)\n"
    ")\n"
    "\n",
    "CREATE INDEX ix_workspace_memory_mutations_user_id ON workspace_memory_mutations (user_id)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    for table, owner in (("student_resource_revisions", "student"), ("workspace_memory_mutations", "assistant")):
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        # Supabase installations can automatically grant new public tables.
        op.execute(f"""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
                REVOKE ALL ON public.{table} FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
                REVOKE ALL ON public.{table} FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
                REVOKE ALL ON public.{table} FROM service_role;
            END IF;
        END $$""")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        for role in ("api", owner):
            op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON public.{table} TO devrimo_{role}")
            op.execute(
                f"CREATE POLICY devrimo_{role}_access ON public.{table} "
                f"TO devrimo_{role} USING (true) WITH CHECK (true)"
            )


def downgrade():
    op.drop_table("workspace_memory_mutations")
    op.drop_table("student_resource_revisions")
