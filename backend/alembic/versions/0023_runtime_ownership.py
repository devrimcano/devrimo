"""Enforce domain worker boundaries with independent, non-owner identities."""

import sqlalchemy as sa

from alembic import op

revision = "0023_runtime_ownership"
down_revision = "0022_knowledge_index_generations"
branch_labels = None
depends_on = None

# NOLOGIN groups; deployment creates a separate login per process and grants
# exactly one group. No runtime receives membership in the migration owner.
WRITES = {
    "knowledge": {"campus_ingestion_jobs", "campus_knowledge_records"},
    "embedding": {"knowledge_index_vectors", "knowledge_index_jobs"},
    "researcher": {"researchers", "researcher_sections", "researcher_import_runs", "researcher_import_items"},
    "directory": {"account_directory", "organizations"},
    "catalog": {"schedule_data_cache"},
    "assistant": {"agents", "chat_sessions", "agent_tool_audit"},
    "planning": {"student_timetables", "timetable_revisions"},
    "student": {
        "student_contexts",
        "student_academic_snapshots",
        "user_preferences",
        "user_update_states",
        "user_mail_facts",
    },
}
READS = {
    "knowledge": {"organizations", "campus_sources", "campus_source_revisions", "knowledge_embedding_settings"},
    "embedding": {
        "organizations",
        "campus_sources",
        "campus_knowledge_records",
        "knowledge_index_generations",
        "knowledge_index_activations",
    },
    "researcher": set(),
    "directory": set(),
    "catalog": {"campus_credentials", "user_profiles", "agents", "account_directory", "agent_runtime_settings"},
    "assistant": {
        "account_directory",
        "admin_memberships",
        "agent_runtime_settings",
        "user_profiles",
        "campus_credentials",
    },
    "planning": {
        "student_contexts",
        "student_academic_snapshots",
        "user_preferences",
        "course_offerings",
        "course_rules",
        "planning_policies",
        "schedule_data_cache",
    },
    "student": {"account_directory", "organizations"},
}
APPLICATION_TABLES = [
    "timetable_revisions",
    "account_directory",
    "admin_audit_events",
    "admin_memberships",
    "agent_runtime_settings",
    "agent_tool_audit",
    "agents",
    "campus_credentials",
    "campus_ingestion_jobs",
    "campus_knowledge_records",
    "campus_source_revisions",
    "campus_sources",
    "chat_sessions",
    "course_group_access_audit",
    "course_group_links",
    "course_offerings",
    "course_rules",
    "knowledge_embedding_settings",
    "knowledge_index_activations",
    "knowledge_index_generations",
    "knowledge_index_jobs",
    "knowledge_index_vectors",
    "organizations",
    "planning_policies",
    "researcher_import_items",
    "researcher_import_runs",
    "researcher_sections",
    "researchers",
    "schedule_data_cache",
    "student_academic_snapshots",
    "student_contexts",
    "student_timetables",
    "user_mail_facts",
    "user_preferences",
    "user_profiles",
    "user_update_states",
]

SOURCE_PROGRESS = "etag,last_modified,last_fetched_at,last_success_at,last_error,updated_at"


def upgrade():
    db = op.get_bind()
    groups = ["api", *WRITES]
    for group in groups:
        role = f"devrimo_{group}"
        if not db.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}):
            op.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS')
        # Existing same-name roles are checked; never silently demote unrelated logins.
        unsafe = db.scalar(
            sa.text(
                "SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
                "FROM pg_roles WHERE rolname=:role"
            ),
            {"role": role},
        )
        if unsafe:
            raise RuntimeError(f"Existing {role} is not a restricted NOLOGIN group")
        for schema in ("public", "ai"):
            op.execute(f'REVOKE ALL ON SCHEMA {schema} FROM "{role}"')
            op.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM "{role}"')
        op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA ai FROM PUBLIC")
    tables = (
        db.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename = ANY(:tables)"),
            {"tables": APPLICATION_TABLES},
        )
        .scalars()
        .all()
    )
    for table in tables:
        q = f'public."{table}"'
        op.execute(f"REVOKE ALL ON TABLE {q} FROM PUBLIC")
        for exposed in ("anon", "authenticated", "service_role"):
            if db.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": exposed}):
                op.execute(f'REVOKE ALL ON TABLE {q} FROM "{exposed}"')
        op.execute(f"ALTER TABLE {q} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {q} TO devrimo_api")
        op.execute(f"CREATE POLICY devrimo_api_access ON {q} TO devrimo_api USING (true) WITH CHECK (true)")
        for group, owned in WRITES.items():
            reads = READS[group] | owned
            if group == "knowledge":
                reads |= {"campus_sources"}
            if table not in reads:
                continue
            op.execute(f"GRANT SELECT ON {q} TO devrimo_{group}")
            op.execute(f"CREATE POLICY devrimo_{group}_access ON {q} TO devrimo_{group} USING (true) WITH CHECK (true)")
            if table in owned:
                op.execute(f"GRANT INSERT,UPDATE,DELETE ON {q} TO devrimo_{group}")
            elif group == "knowledge" and table == "campus_sources":
                op.execute(f"GRANT UPDATE ({SOURCE_PROGRESS}) ON {q} TO devrimo_knowledge")
    for sequence, table in db.execute(
        sa.text("""
        SELECT s.relname,t.relname FROM pg_class s
        JOIN pg_depend d ON d.objid=s.oid AND d.deptype IN ('a','i')
        JOIN pg_class t ON t.oid=d.refobjid
        JOIN pg_namespace n ON n.oid=s.relnamespace
        WHERE s.relkind='S' AND n.nspname='public'
    """)
    ):
        owners = ["api", *[group for group, owned in WRITES.items() if table in owned]]
        op.execute(f'REVOKE ALL ON SEQUENCE public."{sequence}" FROM PUBLIC')
        for group in owners:
            op.execute(f'GRANT USAGE ON SEQUENCE public."{sequence}" TO devrimo_{group}')
    op.execute("GRANT USAGE ON SCHEMA ai TO devrimo_assistant")
    for table in db.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname='ai' AND tablename LIKE 'agno_%'")
    ).scalars():
        q = f'ai."{table}"'
        op.execute(f"REVOKE ALL ON {q} FROM PUBLIC")
        for exposed in ("anon", "authenticated", "service_role"):
            if db.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": exposed}):
                op.execute(f'REVOKE ALL ON {q} FROM "{exposed}"')
        op.execute(f"ALTER TABLE {q} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT ON {q} TO devrimo_assistant")
        if table != "agno_schema_versions":
            op.execute(f"GRANT INSERT,UPDATE,DELETE ON {q} TO devrimo_assistant")
        op.execute(f"CREATE POLICY devrimo_assistant_access ON {q} TO devrimo_assistant USING (true) WITH CHECK (true)")


def downgrade():
    # Reverting permissions would reopen private data to the Data API. Keep
    # the security boundary across application rollback; forward-fix instead.
    pass
