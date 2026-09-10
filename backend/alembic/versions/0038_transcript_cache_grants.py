"""Let the runtime roles reach the transcript cache 0037 created.

0037 created `chat_transcript_cache` and stopped there. Every other table in
this database is locked down and then granted to the roles that use it; this
one was only created, so the API role hit

    permission denied for table chat_transcript_cache

on every chat it opened. The read was written to fall back to Agno when the
cache is unreachable, and it did - but the failed statement had already left
the request's transaction unusable, so the next query on it raised and the
student got a 500 opening any conversation. The rollback is fixed alongside
this; this migration is the reason there was anything to fall back from.

Also brings the table into the same lockdown as its neighbours, which 0037
skipped as well: the Supabase-provided roles must not reach it, and the rows
are transcripts.
"""

from alembic import op

revision = "0038_transcript_cache_grants"
down_revision = "0037_chat_transcript_cache"
branch_labels = None
depends_on = None

TABLE = "chat_transcript_cache"


def upgrade() -> None:
    op.execute(f"REVOKE ALL ON public.{TABLE} FROM PUBLIC")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON public.{TABLE} FROM anon; END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        REVOKE ALL ON public.{TABLE} FROM authenticated;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.{TABLE} FROM service_role;
      END IF;
    END $$""")
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")

    # The API both reads and fills the cache, so it needs all four. Written as
    # DO blocks because a deployment may not have every role, and a migration
    # that fails on a missing role leaves the table unreachable - which is the
    # failure this migration exists to repair.
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='devrimo_api') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON public.{TABLE} TO devrimo_api;
        IF NOT EXISTS (
          SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='{TABLE}'
            AND policyname='devrimo_api_access'
        ) THEN
          CREATE POLICY devrimo_api_access ON public.{TABLE} TO devrimo_api USING (true) WITH CHECK (true);
        END IF;
      END IF;
    END $$""")

    # The assistant worker deletes a conversation's cached copy when the
    # conversation changes underneath it, so it needs more than SELECT.
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='devrimo_assistant') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON public.{TABLE} TO devrimo_assistant;
        IF NOT EXISTS (
          SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='{TABLE}'
            AND policyname='devrimo_assistant_access'
        ) THEN
          CREATE POLICY devrimo_assistant_access ON public.{TABLE}
            TO devrimo_assistant USING (true) WITH CHECK (true);
        END IF;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='devrimo_api') THEN
        DROP POLICY IF EXISTS devrimo_api_access ON public.{TABLE};
        REVOKE ALL ON public.{TABLE} FROM devrimo_api;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='devrimo_assistant') THEN
        DROP POLICY IF EXISTS devrimo_assistant_access ON public.{TABLE};
        REVOKE ALL ON public.{TABLE} FROM devrimo_assistant;
      END IF;
    END $$""")
    op.execute(f"ALTER TABLE public.{TABLE} DISABLE ROW LEVEL SECURITY")
