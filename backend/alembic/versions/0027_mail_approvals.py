"""Durable assistant execution and exact email approval boundaries."""

from alembic import op

revision = "0027_mail_approvals"
down_revision = "0026_assistant_runs"
branch_labels = None
depends_on = None

DDL = [
    "\n"
    "CREATE TABLE workspace_mail_approvals (\n"
    "\tid UUID NOT NULL, \n"
    "\tuser_id UUID NOT NULL, \n"
    "\trun_id VARCHAR(128) NOT NULL, \n"
    "\ttoken_hash VARCHAR(64) NOT NULL, \n"
    "\tdraft_digest VARCHAR(64) NOT NULL, \n"
    "\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tresult JSONB, \n"
    "\tPRIMARY KEY (id), \n"
    "\tUNIQUE (token_hash)\n"
    ")\n"
    "\n",
    "CREATE INDEX ix_workspace_mail_approvals_user_id ON workspace_mail_approvals (user_id)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.create_unique_constraint(
        "uq_mail_approval_action", "workspace_mail_approvals", ["user_id", "run_id", "draft_digest"]
    )
    for table in ["workspace_mail_approvals"]:
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        op.execute(f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON public.{table} FROM anon; END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            REVOKE ALL ON public.{table} FROM authenticated;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
            REVOKE ALL ON public.{table} FROM service_role;
          END IF;
        END $$""")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON public.{table} TO devrimo_api")
        op.execute(f"CREATE POLICY devrimo_api_access ON public.{table} TO devrimo_api USING (true) WITH CHECK (true)")
        op.execute(f"GRANT SELECT ON public.{table} TO devrimo_assistant")
        op.execute(
            f"CREATE POLICY devrimo_assistant_access ON public.{table} "
            "TO devrimo_assistant USING (true) WITH CHECK (true)"
        )


def downgrade():
    op.drop_table("workspace_mail_approvals")
