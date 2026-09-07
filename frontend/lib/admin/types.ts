import type { components } from "@/lib/api/schema";

export type AdminRole = components["schemas"]["AdminRole"];

export type AdminPrincipal = {
  user_id: string;
  email: string | null;
  role: AdminRole;
  organization_id: string | null;
  permissions: string[];
  bootstrap: boolean;
};

export type Overview = {
  users: number;
  active_users: number;
  onboarding_completed: number;
  campus_connected: number;
  agents: Record<string, number>;
  resident_agents: number;
  usage: {
    runs: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    last_24h_tokens: number;
    last_7d_tokens: number;
    estimated_cost_usd: number;
    primary_model_tokens: number;
    compression_tokens: number;
    learning_tokens: number;
  };
  attention: Array<{
    user_id: string;
    email: string | null;
    account_status: string;
    agent_status: string | null;
  }>;
  fresh_at: string;
};

export type AdminUser = {
  user_id: string;
  email: string | null;
  display_name: string | null;
  status: string;
  onboarding_completed: boolean;
  agent_status: string | null;
  last_seen_at: string | null;
  created_at: string;
};

export type AgentRow = {
  user_id: string;
  email: string | null;
  display_name: string | null;
  status: string;
  resident: boolean;
  last_active_at: string | null;
  has_error: boolean;
};

export type AdminUserDetail = {
  status: string;
  last_seen_at: string | null;
  agent: { status: string } | null;
  sessions: { count: number };
  campus: { connected: boolean; enabled_tools: string[] };
};

export type IntegrationOverview = {
  connected_accounts: number;
  items: Array<{
    id: string;
    name_en: string;
    name_tr: string;
    adopted: number;
    verification_failures: number;
  }>;
  commits: Record<string, string>;
};

export type AuditEvent = {
  id: string;
  actor_user_id: string;
  target_user_id: string | null;
  action: string;
  result: string;
  reason: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
};

export type AdminMembership = {
  user_id: string;
  email: string | null;
  role: AdminRole;
  organization_id: string | null;
  bootstrap: boolean;
};

export type SystemHealth = {
  broker: string;
  database: string;
  posthog: string;
  posthog_dashboard_url: string | null;
  supabase_admin: string;
  agent_runtime: string;
  resident_agents: number;
  pool_capacity: number;
  checked_at: string;
};

export type RuntimeSettings = {
  model_id: string;
  profile: "scholar" | "legacy";
  max_tokens: number;
  legacy_history_runs: number;
  scholar_history_runs: number;
  tool_call_limit: number;
  learning_enabled: boolean;
  input_token_price: number;
  output_token_price: number;
  revision: number;
  has_database_override: boolean;
  updated_at: string | null;
  editable: boolean;
};

export type KnowledgeSource = {
  id: string;
  name: string;
  kind: string;
  url: string | null;
  language: string;
  authority: number;
  audience: Record<string, unknown>;
  schedule_seconds: number;
  enabled: boolean;
  status: string;
  active_revision_id: string | null;
  last_fetched_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  revisions: number | null;
  draft_revisions?: number | null;
  records: number | null;
};

export type SourceRevision = {
  id: string;
  revision: number;
  status: string;
  config: Record<string, unknown>;
  validation: { ok: boolean; errors: string[]; warnings: string[] };
  created_at: string;
  published_at: string | null;
};

export type KnowledgeSourceDetail = KnowledgeSource & { revision_history: SourceRevision[] };

export type BatchSourceInput = components["schemas"]["SourceCreateIn"];

export type EmbeddingSettings = {
  provider: "disabled" | "local" | "remote";
  model: string;
  base_url: string | null;
  dimensions: number;
  batch_size: number;
  query_prefix: string;
  document_prefix: string;
  has_api_key: boolean;
  has_database_override: boolean;
  model_label: string | null;
  total_records: number;
  embedded_records: number;
  current_model_records: number;
  active_jobs: number;
};

export type IngestionJob = {
  id: string;
  source_id: string;
  source_name: string;
  kind: "ingest" | "reembed";
  status: string;
  phase: string;
  attempt: number;
  total_records: number;
  processed_records: number;
  embedded_records: number;
  embedding_provider: string | null;
  embedding_model: string | null;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
  progress_updated_at: string;
};

export type KnowledgeSearchResult = {
  id: string;
  document_id: string;
  type: string;
  title: string;
  summary: string | null;
  content: string;
  section: string | null;
  chunk_index: number;
  chunk_count: number;
  page_number: number | null;
  url: string | null;
  language: string | null;
  starts_at: string | null;
  ends_at: string | null;
  published_at: string | null;
  audience: Record<string, unknown>;
  source: string;
  source_id: string;
  retrieved_at: string;
  source_last_success_at: string | null;
  score: number;
};

export type KnowledgeSearchResponse = {
  query: string;
  count: number;
  embedding_model: string | null;
  items: KnowledgeSearchResult[];
};

export type CourseGroup = {
  id: string;
  course_code: string;
  section: string | null;
  eligibility: Record<string, unknown>;
  active: boolean;
  valid_until: string | null;
  has_invite_url: boolean;
};
