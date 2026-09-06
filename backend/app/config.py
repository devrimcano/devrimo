from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "http://localhost:3000"

    # --- Deployment identity ----------------------------------------------
    # Stamped onto every event, log record and span so a regression can be
    # attributed to a deploy rather than to a date. ``release`` is normally
    # left unset here: ``scripts/deploy-vps.sh`` writes the commit to a
    # ``.release`` marker beside the deployed tree, which
    # ``app.observability.runtime`` reads when this is empty.
    environment: str = "development"  # "development" | "staging" | "production"
    release: str = ""

    # Retrieval depends on pgvector, pg_trgm and Postgres full-text search, so
    # there is no second supported engine to fall back to.
    database_url: str = "postgresql+asyncpg://devrimo:devrimo@localhost:5432/devrimo"

    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    # Supabase secret key (or legacy service-role key). Backend only.
    supabase_secret_key: str = ""
    admin_bootstrap_user_ids: str = ""
    admin_directory_sync_seconds: int = 300
    posthog_dashboard_url: str = ""

    # --- Agent runtime -----------------------------------------------------
    # "agno" runs a real Agno agent with real MCP subprocesses. "fake" swaps in
    # a scripted agent so the whole API can be exercised in tests and local dev
    # without a model provider or the four campus servers installed.
    agent_runtime: str = "agno"  # "agno" | "fake"
    # Scholar is the production-hardened profile. Legacy remains available as
    # an explicit rollback target while a deployment completes its eval gates.
    agent_profile: str = "scholar"  # "scholar" | "legacy"

    agent_model: str = "muse-spark-1.2-contributor"
    agent_openai_base_url: str = "https://opencode.ai/zen/go/v1"
    agent_openai_api_key: str = ""
    agent_max_tokens: int = 32768

    # How many prior runs of a session are replayed into the model's context.
    agent_history_runs: int = 10
    scholar_history_runs: int = 3
    agent_tool_call_limit: int = 10
    agent_compress_tool_results: bool = False
    agent_compress_tool_results_limit: int = 3
    agent_learning_enabled: bool = True
    agent_retries: int = 2
    agent_store_events: bool = False
    agent_tracing_enabled: bool = False
    # A user's agent (and its MCP subprocesses) is torn down after this long
    # with no turns. Nothing is lost — history lives in the database.
    agent_idle_timeout_seconds: int = 900
    # Ceiling on simultaneously-resident users. The least recently used agent
    # is evicted past this, so a busy hour can't spawn unbounded subprocesses.
    agent_pool_max_size: int = 64
    reconcile_interval_seconds: int = 60
    # How often expired schedule_data_cache rows are reclaimed. Nothing else
    # reclaims them: the read path only drops rows somebody asks for twice.
    schedule_cache_sweep_seconds: int = 3600
    turn_lock_lease_seconds: int = 180
    turn_lock_heartbeat_seconds: int = 60

    # --- Observability (PostHog) -------------------------------------------
    # Everything below is optional: with no key the whole integration is a
    # no-op, so a developer without a PostHog project still gets a working
    # broker. ``posthog_debug`` makes that silence loud during development.
    posthog_api_key: str = ""  # phc_...
    posthog_host: str = "https://eu.i.posthog.com"
    # Only needed for local feature-flag evaluation, which avoids a network
    # round trip per flag check on the chat hot path.
    posthog_personal_api_key: str = ""  # phx_...
    posthog_enabled: bool = True
    posthog_debug: bool = False
    # The model is served from an OpenAI-compatible endpoint PostHog has no
    # price table for, so cost is reported from these instead of inferred.
    # Prices are per single token, not per million.
    posthog_input_token_price: float = 0.0
    posthog_output_token_price: float = 0.0

    # --- Campus MCP servers ------------------------------------------------
    # Where the four per-server virtualenvs live on the broker host. The image
    # build installs each one at ``{campus_mcp_root}/{slug}/.venv``.
    campus_mcp_root: str = "/opt/mcp"
    # Per-user scratch root. Servers that cache a session token relative to
    # their CWD (odtuclass) get a private directory beneath this.
    campus_state_root: str = "/var/lib/devrimo/campus"
    campus_mcp_timeout_seconds: int = 30
    # SAIS transcript and student-card reads are full scrapes behind a login and
    # routinely take longer than a chat tool call is allowed to. Sharing one
    # budget meant every academic refresh died at exactly 30 seconds with
    # "Timed out while waiting for response", which surfaced to the student as
    # "SAIS did not answer" — a sentence about SAIS being down for a request we
    # gave up on ourselves. The batch sync gets its own, longer budget; the
    # agent's per-turn calls keep the short one.
    campus_sync_timeout_seconds: int = 120
    # How long an *optional* SAIS read may take before the sync gives up on it
    # and returns what it already has. The weekly schedule is the one this
    # exists for: it is the least useful field and the one METU is slowest to
    # render, and waiting out the campus server's own thirty-second ceiling on
    # it made a refresh that was finished in four seconds look like a failure.
    sais_optional_read_seconds: int = 12
    # Catalog reads are serialised per student, because the Course Info server
    # keeps one stateful SAIS session and two concurrent calls land on each
    # other's page. That queue did not exist before, and the agent's per-turn
    # ceiling would fall on whoever is *waiting* rather than on the slow call —
    # cutting a read short and reporting a timeout for a request that was going
    # to succeed. Deliberately generous: this is a budget for a queue, and the
    # point of the queue is that everyone in it eventually gets served.
    campus_catalog_timeout_seconds: int = 120

    # --- Catalog pre-warming ----------------------------------------------
    # Course offerings are published per term and then barely move, so the
    # cache can be filled before students arrive instead of by whoever opens a
    # department first. These requests hit METU with a real student's
    # credentials, so the defaults are deliberately timid: roughly three
    # requests a minute, only in the small hours, with a daily ceiling well
    # under the 153 Ankara departments so a full term's warm-up is spread over
    # several nights rather than done in one visible burst.
    catalog_warm_enabled: bool = True
    catalog_warm_interval_seconds: float = 20.0
    catalog_warm_jitter_seconds: float = 10.0
    # Enough for the whole Ankara catalog in one night. At one request every
    # twenty seconds that is under an hour of traffic inside a six-hour
    # window, once per term — gentler than a student clicking through the
    # catalog by hand, and the interval above is what actually paces it. The
    # ceiling exists to bound a runaway, not to spread the work over days:
    # until a department is cached, searching course titles cannot see it.
    catalog_warm_daily_limit: int = 200
    # Per pass, so the worker returns to its other duties between batches.
    catalog_warm_batch: int = 15
    # Local hours, as "start-end". Overnight, when METU is quiet.
    catalog_warm_hours: str = "1-7"
    catalog_warm_poll_seconds: int = 900
    # Read the student's academic context from SAIS as part of saving a verified
    # campus connection, so their profile is populated before their first turn.
    # Costs one campus server spawn inside that request; turn it off to keep the
    # save fast and leave the context to the planner's in-turn refresh.
    campus_context_sync_on_connect: bool = True

    # --- Shared campus knowledge ------------------------------------------
    knowledge_worker_poll_seconds: int = 15
    knowledge_worker_lease_seconds: int = 120
    knowledge_fetch_timeout_seconds: int = 20
    knowledge_fetch_max_bytes: int = 5_000_000
    knowledge_fetch_user_agent: str = "DevrimoCampusIndexer/1.0 (+https://devrimo.app)"
    knowledge_embedding_enabled: bool = False
    knowledge_embedding_model: str = "text-embedding-3-small"
    knowledge_embedding_dimensions: Literal[384, 768, 1536] = 1536
    knowledge_embedding_base_url: str = "https://api.openai.com/v1"
    knowledge_embedding_api_key: str = ""

    secret_encryption_key: str = "change-me-to-a-real-generated-secret"

    # --- AgentOS -----------------------------------------------------------
    # AgentOS runs as a separate, internal process. Production authorization
    # uses JWT/RBAC; the old shared OS security key is intentionally not used.
    agentos_enabled: bool = False
    agentos_host: str = "127.0.0.1"
    agentos_port: int = 7777
    agentos_jwt_verification_key: str = ""
    agentos_jwks_file: str = ""
    agentos_jwt_algorithm: str = "RS256"
    agentos_jwt_audience: str = "devrimo"
    agentos_admin_scope: str = "agentos:admin"
    agentos_cors_origins: str = "https://os.agno.com"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def posthog_configured(self) -> bool:
        return bool(self.posthog_enabled and self.posthog_api_key.strip())

    @property
    def agentos_cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.agentos_cors_origins.split(",") if origin.strip()]

    @property
    def admin_bootstrap_ids(self) -> set[str]:
        return {value.strip() for value in self.admin_bootstrap_user_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
