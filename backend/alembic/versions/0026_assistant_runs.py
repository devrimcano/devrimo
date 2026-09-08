"""Durable assistant execution and exact email approval boundaries."""

from alembic import op

revision = "0026_assistant_runs"
down_revision = "0025_retire_legacy_vectors"
branch_labels = None
depends_on = None

DDL = [
    "\n"
    "CREATE TABLE assistant_runs (\n"
    "\tid UUID NOT NULL, \n"
    "\tuser_id UUID NOT NULL, \n"
    "\tsession_id VARCHAR(64) NOT NULL, \n"
    "\tkind VARCHAR(16) NOT NULL, \n"
    "\tpayload JSONB NOT NULL, \n"
    "\ttoken_enc BYTEA, \n"
    "\tapproval_token_enc BYTEA, \n"
    "\ttoken_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n"
    "\tstatus VARCHAR(16) DEFAULT 'queued' NOT NULL, \n"
    "\tlease_owner VARCHAR(128), \n"
    "\tleased_until TIMESTAMP WITH TIME ZONE, \n"
    "\tcancel_requested BOOLEAN DEFAULT 'false' NOT NULL, \n"
    "\tlast_event_sequence INTEGER DEFAULT '0' NOT NULL, \n"
    "\tagno_run_id VARCHAR(128), \n"
    "\terror_code VARCHAR(64), \n"
    "\tstarted_at TIMESTAMP WITH TIME ZONE, \n"
    "\tfinished_at TIMESTAMP WITH TIME ZONE, \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tidempotency_key VARCHAR(128) NOT NULL, \n"
    "\trequest_hash VARCHAR(64) NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_assistant_run_kind CHECK (kind IN ('chat','confirmation')), \n"
    "\tCONSTRAINT ck_assistant_run_status CHECK (status IN "
    "('queued','running','paused','completed','failed','cancelled','interrupted')), \n"
    "\tCONSTRAINT uq_assistant_run_idempotency UNIQUE (user_id, idempotency_key), \n"
    "\tFOREIGN KEY(session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "CREATE UNIQUE INDEX ix_assistant_one_active_user ON assistant_runs (user_id) WHERE status IN ('queued','running')",
    "CREATE INDEX ix_assistant_runs_claim ON assistant_runs (status, created_at)",
    "\n"
    "CREATE TABLE assistant_run_events (\n"
    "\trun_id UUID NOT NULL, \n"
    "\tsequence INTEGER NOT NULL, \n"
    "\tpayload TEXT NOT NULL, \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (run_id, sequence), \n"
    "\tFOREIGN KEY(run_id) REFERENCES assistant_runs (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    for table in ["assistant_runs", "assistant_run_events"]:
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
    op.execute("GRANT INSERT ON assistant_run_events TO devrimo_assistant")
    op.execute("""GRANT UPDATE
        (status,lease_owner,leased_until,last_event_sequence,agno_run_id,error_code,started_at,finished_at,token_enc,approval_token_enc)
        ON assistant_runs TO devrimo_assistant""")
    op.execute("REVOKE SELECT ON campus_credentials FROM devrimo_assistant")
    op.execute("DROP POLICY IF EXISTS devrimo_assistant_access ON campus_credentials")


def downgrade():
    op.drop_table("assistant_run_events")
    op.drop_table("assistant_runs")
