"""Runtime write boundaries; mirrored by the immutable ownership migration."""

WRITE_TABLES = {
    "knowledge": {"campus_ingestion_jobs", "campus_knowledge_records"},
    "embedding": {"knowledge_index_vectors", "knowledge_index_jobs"},
    "researcher": {"researchers", "researcher_sections", "researcher_import_runs", "researcher_import_items"},
    "directory": {"account_directory", "organizations"},
    "catalog": {"schedule_data_cache"},
    "assistant": {"assistant_run_events", "workspace_memory_mutations", "agents", "chat_sessions", "agent_tool_audit"},
    "planning": {"student_timetables", "timetable_revisions"},
    "student": {
        "student_resource_revisions",
        "student_contexts",
        "student_academic_snapshots",
        "user_preferences",
        "user_update_states",
        "user_mail_facts",
    },
}
SOURCE_PROGRESS_COLUMNS = {"etag", "last_modified", "last_fetched_at", "last_success_at", "last_error", "updated_at"}


def permitted_write(role: str, schema: str, table: str, column: str | None = None) -> bool:
    if role == "api":
        return schema == "public" and table != "alembic_version"
    if role == "assistant" and schema == "public" and table == "assistant_runs":
        return column in {
            "status", "lease_owner", "leased_until", "last_event_sequence", "agno_run_id",
            "error_code", "started_at", "finished_at", "token_enc", "approval_token_enc",
        }
    if role == "assistant" and schema == "ai":
        return table.startswith("agno_") and table != "agno_schema_versions"
    if schema != "public":
        return False
    if role == "knowledge" and table == "campus_sources":
        return column in SOURCE_PROGRESS_COLUMNS
    return table in WRITE_TABLES.get(role, set())
