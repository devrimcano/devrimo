"""Freeze Agno 3.0.1 persistence; runtime identities never provision tables.

Existing tables are preserved. Upgrading older Agno layouts requires the
upstream data migration before this release; this additive DDL never replaces
conversation history.
"""

from alembic import op

revision = "0020_agno_schema"
down_revision = "0019_avesis_researchers"
branch_labels = None
depends_on = None

DDL = [
    "CREATE SCHEMA IF NOT EXISTS ai",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_approvals (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\trun_id VARCHAR NOT NULL, \n"
    "\tsession_id VARCHAR NOT NULL, \n"
    "\tstatus VARCHAR NOT NULL, \n"
    "\tsource_type VARCHAR NOT NULL, \n"
    "\tapproval_type VARCHAR, \n"
    "\tpause_type VARCHAR NOT NULL, \n"
    "\ttool_name VARCHAR, \n"
    "\ttool_args JSONB, \n"
    "\texpires_at BIGINT, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tschedule_id VARCHAR, \n"
    "\tschedule_run_id VARCHAR, \n"
    "\tsource_name VARCHAR, \n"
    "\trequirements JSONB, \n"
    "\tcontext JSONB, \n"
    "\tresolution_data JSONB, \n"
    "\tresolved_by VARCHAR, \n"
    "\tresolved_at BIGINT, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\trun_status VARCHAR, \n"
    "\tPRIMARY KEY (id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_agent_id ON ai.agno_approvals (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_approval_type ON ai.agno_approvals (approval_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_created_at ON ai.agno_approvals (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_pause_type ON ai.agno_approvals (pause_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_run_id ON ai.agno_approvals (run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_run_status ON ai.agno_approvals (run_status)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_schedule_id ON ai.agno_approvals (schedule_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_schedule_run_id ON ai.agno_approvals (schedule_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_session_id ON ai.agno_approvals (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_source_type ON ai.agno_approvals (source_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_status ON ai.agno_approvals (status)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_team_id ON ai.agno_approvals (team_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_user_id ON ai.agno_approvals (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_approvals_workflow_id ON ai.agno_approvals (workflow_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_components (\n"
    "\tcomponent_id VARCHAR NOT NULL, \n"
    "\tcomponent_type VARCHAR NOT NULL, \n"
    "\tname VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tdescription TEXT, \n"
    "\tcurrent_version INTEGER, \n"
    "\tmetadata JSONB, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tdeleted_at BIGINT, \n"
    "\tPRIMARY KEY (component_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_components_component_type ON ai.agno_components (component_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_components_created_at ON ai.agno_components (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_components_current_version ON ai.agno_components (current_version)",
    "CREATE INDEX IF NOT EXISTS idx_agno_components_name ON ai.agno_components (name)",
    "CREATE INDEX IF NOT EXISTS idx_agno_components_user_id ON ai.agno_components (user_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_eval_runs (\n"
    "\trun_id VARCHAR NOT NULL, \n"
    "\teval_type VARCHAR NOT NULL, \n"
    "\teval_data JSONB NOT NULL, \n"
    "\teval_input JSONB NOT NULL, \n"
    "\tname VARCHAR, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tmodel_id VARCHAR, \n"
    "\tmodel_provider VARCHAR, \n"
    "\tevaluated_component_name VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (run_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_eval_runs_created_at ON ai.agno_eval_runs (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_eval_runs_user_id ON ai.agno_eval_runs (user_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_knowledge (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\tname VARCHAR NOT NULL, \n"
    "\tdescription TEXT NOT NULL, \n"
    "\tmetadata JSONB, \n"
    "\ttype VARCHAR, \n"
    "\tsize BIGINT, \n"
    "\tlinked_to VARCHAR, \n"
    "\taccess_count BIGINT, \n"
    "\tstatus VARCHAR, \n"
    "\tstatus_message TEXT, \n"
    "\tcreated_at BIGINT, \n"
    "\tupdated_at BIGINT, \n"
    "\texternal_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tPRIMARY KEY (id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_knowledge_user_id ON ai.agno_knowledge (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_knowledge_user_id_linked_to ON ai.agno_knowledge (user_id, linked_to)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_learnings (\n"
    "\tlearning_id VARCHAR NOT NULL, \n"
    "\tlearning_type VARCHAR NOT NULL, \n"
    "\tnamespace VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tsession_id VARCHAR, \n"
    "\tentity_id VARCHAR, \n"
    "\tentity_type VARCHAR, \n"
    "\tcontent JSONB NOT NULL, \n"
    "\tmetadata JSONB, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (learning_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_agent_id ON ai.agno_learnings (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_created_at ON ai.agno_learnings (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_entity_id ON ai.agno_learnings (entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_entity_type ON ai.agno_learnings (entity_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_learning_type ON ai.agno_learnings (learning_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_namespace ON ai.agno_learnings (namespace)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_session_id ON ai.agno_learnings (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_team_id ON ai.agno_learnings (team_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_user_id ON ai.agno_learnings (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_learnings_workflow_id ON ai.agno_learnings (workflow_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_memories (\n"
    "\tmemory_id VARCHAR NOT NULL, \n"
    "\tmemory JSONB NOT NULL, \n"
    "\tfeedback TEXT, \n"
    "\tinput TEXT, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\ttopics JSONB, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (memory_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_memories_created_at ON ai.agno_memories (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_memories_updated_at ON ai.agno_memories (updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_memories_user_id ON ai.agno_memories (user_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_metrics (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\tagent_runs_count BIGINT NOT NULL, \n"
    "\tteam_runs_count BIGINT NOT NULL, \n"
    "\tworkflow_runs_count BIGINT NOT NULL, \n"
    "\tagent_sessions_count BIGINT NOT NULL, \n"
    "\tteam_sessions_count BIGINT NOT NULL, \n"
    "\tworkflow_sessions_count BIGINT NOT NULL, \n"
    "\tusers_count BIGINT NOT NULL, \n"
    "\ttoken_metrics JSONB NOT NULL, \n"
    "\tmodel_metrics JSONB NOT NULL, \n"
    "\tdate DATE NOT NULL, \n"
    "\taggregation_period VARCHAR NOT NULL, \n"
    "\tuser_id VARCHAR NOT NULL, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tcompleted BOOLEAN NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT agno_metrics_uq_metrics_user_date_period UNIQUE (user_id, date, "
    "aggregation_period)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_metrics_date ON ai.agno_metrics (date)",
    "CREATE INDEX IF NOT EXISTS idx_agno_metrics_user_id ON ai.agno_metrics (user_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_schedules (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\tname VARCHAR NOT NULL, \n"
    "\tdescription TEXT, \n"
    "\tmethod VARCHAR NOT NULL, \n"
    "\tendpoint VARCHAR NOT NULL, \n"
    "\tpayload JSONB, \n"
    "\tcron_expr VARCHAR NOT NULL, \n"
    "\ttimezone VARCHAR NOT NULL, \n"
    "\ttimeout_seconds BIGINT NOT NULL, \n"
    "\tmax_retries BIGINT NOT NULL, \n"
    "\tretry_delay_seconds BIGINT NOT NULL, \n"
    "\tenabled BOOLEAN NOT NULL, \n"
    "\tnext_run_at BIGINT, \n"
    "\tlocked_by VARCHAR, \n"
    "\tlocked_at BIGINT, \n"
    "\tuser_id VARCHAR, \n"
    "\tmanaged_by VARCHAR, \n"
    "\ttarget_type VARCHAR, \n"
    "\ttarget_id VARCHAR, \n"
    "\tcreated_by_run_id VARCHAR, \n"
    "\tcreated_by_session_id VARCHAR, \n"
    "\tupdated_by_run_id VARCHAR, \n"
    "\tupdated_by_session_id VARCHAR, \n"
    "\tdisabled_reason VARCHAR, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (id)\n"
    ")\n"
    "\n",
    "CREATE UNIQUE INDEX IF NOT EXISTS agno_schedules_uq_unowned_name ON ai.agno_schedules (name) "
    "WHERE user_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS agno_schedules_uq_user_name ON ai.agno_schedules (user_id, "
    "name) WHERE user_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_created_at ON ai.agno_schedules (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_enabled_next_run_at ON ai.agno_schedules (enabled, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_managed_by ON ai.agno_schedules (managed_by)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_name ON ai.agno_schedules (name)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_next_run_at ON ai.agno_schedules (next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_target_id ON ai.agno_schedules (target_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_user_id ON ai.agno_schedules (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedules_user_id_enabled_next_run_at ON ai.agno_schedules "
    "(user_id, enabled, next_run_at)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_schema_versions (\n"
    "\ttable_name VARCHAR NOT NULL, \n"
    "\tversion VARCHAR NOT NULL, \n"
    "\tcreated_at VARCHAR NOT NULL, \n"
    "\tupdated_at VARCHAR, \n"
    "\tPRIMARY KEY (table_name)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_schema_versions_created_at ON ai.agno_schema_versions (created_at)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_service_accounts (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\tname VARCHAR NOT NULL, \n"
    "\tuser_id VARCHAR, \n"
    "\ttoken_hash VARCHAR NOT NULL, \n"
    "\ttoken_prefix VARCHAR NOT NULL, \n"
    "\tscopes JSONB NOT NULL, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\texpires_at BIGINT, \n"
    "\tlast_used_at BIGINT, \n"
    "\trevoked_at BIGINT, \n"
    "\tcreated_by VARCHAR, \n"
    "\tPRIMARY KEY (id), \n"
    "\tUNIQUE (token_hash)\n"
    ")\n"
    "\n",
    "CREATE UNIQUE INDEX IF NOT EXISTS agno_service_accounts_uq_active_name ON "
    "ai.agno_service_accounts (name) WHERE revoked_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_agno_service_accounts_created_at ON ai.agno_service_accounts (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_service_accounts_token_hash ON ai.agno_service_accounts (token_hash)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_sessions (\n"
    "\tsession_id VARCHAR NOT NULL, \n"
    "\tsession_type VARCHAR NOT NULL, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tsession_data JSONB, \n"
    "\tagent_data JSONB, \n"
    "\tteam_data JSONB, \n"
    "\tworkflow_data JSONB, \n"
    "\tmetadata JSONB, \n"
    "\tsummary JSONB, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (session_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_sessions_created_at ON ai.agno_sessions (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_sessions_session_type ON ai.agno_sessions (session_type)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_tool_results (\n"
    "\tresult_id VARCHAR NOT NULL, \n"
    "\tnamespace VARCHAR NOT NULL, \n"
    "\tpath VARCHAR NOT NULL, \n"
    "\tsession_id VARCHAR NOT NULL, \n"
    "\trun_id VARCHAR NOT NULL, \n"
    "\ttool_call_id VARCHAR NOT NULL, \n"
    "\ttool_name VARCHAR NOT NULL, \n"
    "\targs_hash VARCHAR NOT NULL, \n"
    "\tcontent_type VARCHAR NOT NULL, \n"
    "\tsize_bytes BIGINT NOT NULL, \n"
    "\tline_count BIGINT NOT NULL, \n"
    "\tpreview TEXT NOT NULL, \n"
    "\tuser_id VARCHAR, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\texpires_at BIGINT, \n"
    "\tPRIMARY KEY (result_id), \n"
    "\tCONSTRAINT agno_tool_results_uq_tool_results_namespace_path UNIQUE (namespace, path)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_tool_results_expires_at ON ai.agno_tool_results (expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_tool_results_session_id_created_at ON ai.agno_tool_results "
    "(session_id, created_at)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_traces (\n"
    "\ttrace_id VARCHAR NOT NULL, \n"
    "\tname VARCHAR NOT NULL, \n"
    "\tstatus VARCHAR NOT NULL, \n"
    "\tstart_time VARCHAR NOT NULL, \n"
    "\tend_time VARCHAR NOT NULL, \n"
    "\tduration_ms BIGINT NOT NULL, \n"
    "\trun_id VARCHAR, \n"
    "\tsession_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tcreated_at VARCHAR NOT NULL, \n"
    "\tPRIMARY KEY (trace_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_agent_id ON ai.agno_traces (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_created_at ON ai.agno_traces (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_run_id ON ai.agno_traces (run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_session_id ON ai.agno_traces (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_start_time ON ai.agno_traces (start_time)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_status ON ai.agno_traces (status)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_team_id ON ai.agno_traces (team_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_user_id ON ai.agno_traces (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_traces_workflow_id ON ai.agno_traces (workflow_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_component_configs (\n"
    "\tcomponent_id VARCHAR NOT NULL, \n"
    "\tversion INTEGER NOT NULL, \n"
    "\tlabel VARCHAR, \n"
    "\tstage VARCHAR NOT NULL, \n"
    "\tconfig JSONB NOT NULL, \n"
    "\tnotes TEXT, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tdeleted_at BIGINT, \n"
    "\tPRIMARY KEY (component_id, version), \n"
    "\tFOREIGN KEY(component_id) REFERENCES ai.agno_components (component_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_component_configs_created_at ON ai.agno_component_configs (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_component_configs_stage ON ai.agno_component_configs (stage)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_runs (\n"
    "\trun_id VARCHAR NOT NULL, \n"
    "\tsession_id VARCHAR NOT NULL, \n"
    "\trun_type VARCHAR NOT NULL, \n"
    "\tagent_id VARCHAR, \n"
    "\tteam_id VARCHAR, \n"
    "\tworkflow_id VARCHAR, \n"
    "\tuser_id VARCHAR, \n"
    "\tparent_run_id VARCHAR, \n"
    "\tstatus VARCHAR, \n"
    "\trun_index BIGINT, \n"
    "\trun_data JSONB NOT NULL, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tupdated_at BIGINT, \n"
    "\tPRIMARY KEY (run_id), \n"
    "\tFOREIGN KEY(session_id) REFERENCES ai.agno_sessions (session_id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_agent_id ON ai.agno_runs (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_created_at ON ai.agno_runs (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_run_type ON ai.agno_runs (run_type)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_session_id ON ai.agno_runs (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_session_id_run_index ON ai.agno_runs (session_id, run_index)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_status ON ai.agno_runs (status)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_team_id ON ai.agno_runs (team_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_user_id ON ai.agno_runs (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_runs_workflow_id ON ai.agno_runs (workflow_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_schedule_runs (\n"
    "\tid VARCHAR NOT NULL, \n"
    "\tschedule_id VARCHAR NOT NULL, \n"
    "\tattempt BIGINT NOT NULL, \n"
    "\ttriggered_at BIGINT, \n"
    "\tcompleted_at BIGINT, \n"
    "\tstatus VARCHAR NOT NULL, \n"
    "\tstatus_code BIGINT, \n"
    "\trun_id VARCHAR, \n"
    "\tsession_id VARCHAR, \n"
    "\terror TEXT, \n"
    "\tinput JSONB, \n"
    "\toutput JSONB, \n"
    "\trequirements JSONB, \n"
    "\tuser_id VARCHAR, \n"
    "\tcreated_at BIGINT NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tFOREIGN KEY(schedule_id) REFERENCES ai.agno_schedules (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedule_runs_created_at ON ai.agno_schedule_runs (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedule_runs_schedule_id ON ai.agno_schedule_runs (schedule_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedule_runs_status ON ai.agno_schedule_runs (status)",
    "CREATE INDEX IF NOT EXISTS idx_agno_schedule_runs_user_id ON ai.agno_schedule_runs (user_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_spans (\n"
    "\tspan_id VARCHAR NOT NULL, \n"
    "\ttrace_id VARCHAR NOT NULL, \n"
    "\tparent_span_id VARCHAR, \n"
    "\tname VARCHAR NOT NULL, \n"
    "\tspan_kind VARCHAR NOT NULL, \n"
    "\tstatus_code VARCHAR NOT NULL, \n"
    "\tstatus_message TEXT, \n"
    "\tstart_time VARCHAR NOT NULL, \n"
    "\tend_time VARCHAR NOT NULL, \n"
    "\tduration_ms BIGINT NOT NULL, \n"
    "\tattributes JSONB, \n"
    "\tcreated_at VARCHAR NOT NULL, \n"
    "\tPRIMARY KEY (span_id), \n"
    "\tFOREIGN KEY(trace_id) REFERENCES ai.agno_traces (trace_id)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_spans_created_at ON ai.agno_spans (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_spans_parent_span_id ON ai.agno_spans (parent_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_agno_spans_start_time ON ai.agno_spans (start_time)",
    "CREATE INDEX IF NOT EXISTS idx_agno_spans_trace_id ON ai.agno_spans (trace_id)",
    "\n"
    "CREATE TABLE IF NOT EXISTS ai.agno_component_links (\n"
    "\tparent_component_id VARCHAR NOT NULL, \n"
    "\tparent_version INTEGER NOT NULL, \n"
    "\tlink_kind VARCHAR NOT NULL, \n"
    "\tlink_key VARCHAR NOT NULL, \n"
    "\tchild_component_id VARCHAR NOT NULL, \n"
    "\tchild_version INTEGER, \n"
    "\tposition INTEGER NOT NULL, \n"
    "\tmeta JSONB, \n"
    "\tcreated_at BIGINT, \n"
    "\tupdated_at BIGINT, \n"
    "\tCONSTRAINT agno_component_links_pkey PRIMARY KEY (parent_component_id, parent_version, "
    "link_kind, link_key), \n"
    "\tFOREIGN KEY(child_component_id) REFERENCES ai.agno_components (component_id), \n"
    "\tCONSTRAINT agno_component_links_parent_component_id_parent_version_fkey FOREIGN "
    "KEY(parent_component_id, parent_version) REFERENCES ai.agno_component_configs (component_id, "
    "version)\n"
    ")\n"
    "\n",
    "CREATE INDEX IF NOT EXISTS idx_agno_component_links_created_at ON ai.agno_component_links (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agno_component_links_link_kind ON ai.agno_component_links (link_kind)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    # Conversations outlive an application rollback. Never drop student data.
    pass
