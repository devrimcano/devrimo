import os
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})
_PLACEHOLDER_ENCRYPTION_KEYS = frozenset(
    {
        "change-me",
        "change-me-to-a-real-generated-secret",
        "changeme",
        "replace-me",
        "test-encryption-key",
        "test-secret-not-for-production",
        "your-secret-key",
    }
)
_ENCRYPTION_KEY_ROLES = frozenset({"api", "assistant", "catalog", "embedding", "planning", "student"})
_RUNTIME_COMPONENTS = frozenset(
    {
        "",
        "api",
        "broker",
        "assistant",
        "agentos",
        "knowledge",
        "embedding",
        "researcher",
        "directory",
        "catalog",
        "retention",
    }
)


def parse_catalog_warm_hours(value: str) -> tuple[int, int] | None:
    """Parse a local-hour safety window, returning ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None
    try:
        low, high = (int(part.strip()) for part in parts)
    except (TypeError, ValueError):
        return None
    if not (0 <= low <= 24 and 0 <= high <= 24):
        return None
    return low, high


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
    # API calls may hold their request connection and a campus lease while
    # performing a short cache read. Reserve room for four such concurrent calls.
    database_pool_size: int | None = Field(default=None, ge=1, le=20)
    database_max_overflow: int = Field(default=1, ge=0, le=5)
    database_pool_timeout: float = Field(default=15, gt=0, le=120)
    database_pool_recycle: int = Field(default=300, ge=30)
    agno_database_pool_size: int = Field(default=1, ge=1, le=5)

    # Every process connects using its own restricted login. Migration credentials
    # belong only to the release job, never a worker environment.
    database_migration_url: str = ""
    database_runtime_role: Literal[
        "api", "knowledge", "embedding", "researcher", "directory", "catalog", "assistant", "planning", "student"
    ] = "api"
    assistant_database_url: str = ""
    workspace_gateway_url: str = ""
    assistant_worker_concurrency: int = Field(default=4, ge=1, le=128)
    workspace_gateway_allowed_hosts: str = ""
    # The catalog database role is shared by the catalog warmer and the
    # retention sweep. Only the former decrypts campus credentials, so the
    # process identity keeps the startup key check precise without adding a
    # new database role or granting retention a secret it never uses.
    runtime_component: str = Field(
        default="", validation_alias=AliasChoices("DEVRIMO_RUNTIME_COMPONENT", "runtime_component")
    )

    @model_validator(mode="after")
    def validate_database_configuration(self):
        from sqlalchemy.engine import make_url

        self.environment = self.environment.strip().casefold()
        base = make_url(self.database_url)
        if base.get_backend_name() != "postgresql":
            raise ValueError("Devrimo requires one PostgreSQL database")
        for configured in (self.database_migration_url, self.assistant_database_url):
            if configured:
                other = make_url(configured)
                if (other.get_backend_name(), other.host, other.port, other.database) != (
                    base.get_backend_name(),
                    base.host,
                    base.port,
                    base.database,
                ):
                    raise ValueError("All database identities must use the same PostgreSQL endpoint and database")
        if self.environment in {"production", "staging"}:
            if self.database_migration_url and make_url(self.database_migration_url).username == base.username:
                raise ValueError("Migration and runtime credentials must be separate")
            if base.username and base.username.split(".")[0] in {"postgres", "supabase_admin", "service_role"}:
                raise ValueError("Runtime requires a restricted database login")
        environment = self.environment.strip().casefold()
        runtime_component = self.runtime_component.strip().casefold()
        if runtime_component not in _RUNTIME_COMPONENTS:
            raise ValueError(f"DEVRIMO_RUNTIME_COMPONENT is not recognized: {self.runtime_component!r}")
        if (
            environment not in _DEVELOPMENT_ENVIRONMENTS
            and self.database_runtime_role in _ENCRYPTION_KEY_ROLES
            and not (self.database_runtime_role == "catalog" and runtime_component == "retention")
            and not (
                self.agentos_enabled
                and self.database_runtime_role == "assistant"
                and runtime_component == "agentos"
            )
        ):
            encryption_key = self.secret_encryption_key.strip()
            if not encryption_key or encryption_key.casefold() in _PLACEHOLDER_ENCRYPTION_KEYS:
                raise ValueError(
                    "SECRET_ENCRYPTION_KEY must be set to a non-placeholder value outside development/test"
                )
        if environment in {"production", "staging"}:
            from app.db.engine import require_postgres_tls

            for url in (self.database_url, self.assistant_database_url, self.database_migration_url):
                if url:
                    require_postgres_tls(url)
        if self.supabase_jwks_url and self.supabase_jwks_url != (
            f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        ):
            raise ValueError("SUPABASE_JWKS_URL must match the configured Supabase project's signing-key endpoint")
        return self

    @field_validator("catalog_warm_hours")
    @classmethod
    def validate_catalog_warm_hours(cls, value: str) -> str:
        if parse_catalog_warm_hours(value) is None:
            raise ValueError("CATALOG_WARM_HOURS must be two local hours in the form H-H (0 through 24)")
        return value.strip()

    avesis_proxy_url: SecretStr | None = None

    supabase_url: str = ""
    supabase_jwks_url: str = ""
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
    # Authenticated integration sessions have a bounded idle lifetime and
    # process-local capacity. Assistant runs themselves are durable jobs.
    campus_session_idle_seconds: int = Field(default=300, ge=1)
    campus_session_max_size: int = Field(default=256, ge=1)
    # How often expired schedule_data_cache rows are reclaimed. Nothing else
    # reclaims them: the read path only drops rows somebody asks for twice.
    schedule_cache_sweep_seconds: int = 3600

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
    # The vendored SAIS client may spend up to 30 seconds producing its own
    # explicit timeout. The MCP envelope needs a separate margin to transport
    # that failure; sharing the same deadline replaced the useful cause with a
    # generic "Error executing tool" race.
    campus_mcp_timeout_seconds: int = 45
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
    # Whether the Course Info server holds its SAIS app-proxy session between
    # calls. On by default: without it two thirds of every catalog read is
    # re-establishing a session the process already has. Here rather than only
    # in the image so it can be turned off without a rebuild.
    course_info_session_cache: bool = True
    # Whether catalog actions reuse the held session's navigation position
    # instead of re-selecting the department before every read. Live-verified
    # against SAIS; each reuse is identity-checked with a fallback to the full
    # navigation. Here rather than only in the image so a rollback is a config
    # change, not a rebuild.
    course_info_nav_elision: bool = True

    # Enable published-only reads after administrators publish the initial catalog.
    # Ingestion can be enabled first so migration never exposes an unreviewed draft.
    academic_catalog_reads_enabled: bool = False
    academic_catalog_ingestion_enabled: bool = False
    academic_catalog_registration_start: str = ""
    academic_catalog_registration_end: str = ""
    academic_catalog_job_lease_seconds: int = 900
    academic_catalog_max_attempts: int = 3
    # --- Development campus fixtures ---------------------------------------
    # Swaps the four campus MCP servers for synthetic ones
    # (app/campus/fixtures/server.py) that answer the same tool names from
    # seeded data. Everything between the model and the campus stays real: the
    # allowlist, the confirmation policy for writes, the queue, the gateway,
    # and the consent copy. Only what is on the other end of the socket changes.
    #
    # Rejected outside development and test by the validator below. A staging
    # or production deployment that set this would answer students with invented
    # course data while looking entirely healthy, which is the worst shape a
    # failure can take here.
    campus_fixture_mode: bool = False

    # --- Catalog pre-warming ----------------------------------------------
    # Automatic refresh scheduling; manual catalog imports ignore the window.
    catalog_warm_enabled: bool = True
    # Pacing/budget apply only to the old raw-cache warmer, not catalog imports.
    catalog_warm_interval_seconds: float = 20.0
    catalog_warm_jitter_seconds: float = 10.0
    # Legacy admitted Course Info calls, not courses or internal HTTP requests.
    catalog_warm_daily_limit: int = 200
    # Bound scheduled course refresh jobs per pass across all active terms.
    catalog_warm_courses_per_pass: int = 3
    # Directory discovery is a separate cheap job allowance. Keeping it
    # bounded prevents a large set of terms from filling every pass while the
    # course allowance is reserved for detail freshness.
    catalog_warm_discoveries_per_pass: int = Field(default=1, ge=0, le=100)
    catalog_warm_batch: int = 15
    # How many plan steps one import pass walks before checkpointing and
    # handing control back. This is the import job's own batch, deliberately
    # separate from catalog_warm_batch above, which paces the legacy warmer
    # against a daily HTTP budget and must stay small.
    #
    # At fifteen, a 10,048-step term needed 670 passes and the five seconds
    # between them added an hour of doing nothing. A pass is also bounded by
    # the wall clock below, so a larger batch cannot outlive its lease.
    # Whose SAIS session the catalog is read through, by METU username. A
    # whole-term import is about eleven thousand page loads behind one
    # student's login, and until this existed nobody chose whose: every
    # connected account was eligible and the rotation picked by day of the
    # year. Set this and only that account is used; leave it empty and the old
    # rotation applies.
    catalog_source_metu_username: str = ""
    catalog_import_batch: int = Field(default=200, ge=1, le=5000)
    # A pass stops at this fraction of the job lease regardless of the batch,
    # so a slow source can never let a pass write past its own lease.
    catalog_import_pass_lease_fraction: float = Field(default=0.5, gt=0.0, le=0.9)
    # How often a running pass moves `lease_until`. The lease is fifteen
    # minutes; touching it every three is ample. It used to be touched once per
    # HTTP request, which is four round trips for a timestamp nothing was
    # waiting on.
    catalog_import_lease_touch_seconds: float = Field(default=180.0, ge=1.0)
    # Steps between durable checkpoints. Between them only observations are
    # written; the cursor and the operation plan move together, so an
    # interrupted pass re-reads at most this many pages and never loses the
    # courses those pages discovered.
    catalog_import_checkpoint_steps: int = Field(default=25, ge=1, le=1000)
    # How long a successful source observation may satisfy an import step
    # before the worker reads that page again.  Detail and section pages go
    # stale during add-drop; rule pages change by publication.  Forced imports
    # ignore both windows, and a window above the component's read-time max age
    # is clamped down so a stale component can always be refreshed.
    catalog_reuse_info_seconds: int = Field(default=24 * 3600, ge=0)
    catalog_reuse_rules_seconds: int = Field(default=7 * 24 * 3600, ge=0)
    # How many source pages may be in flight at once, on one client each.
    # Leave this at one for SAIS: measured on production, three clients on one
    # account cost 10s per step against 1.02s with one, and the source began
    # closing connections. SAIS holds its proxy session against the account,
    # not the cookie jar, so extra clients only fight over it. Kept
    # configurable because a second *account* is the real way to parallelise.
    catalog_import_concurrency: int = Field(default=1, ge=1, le=8)
    # Local hours for automatic refreshes, as "start-end".
    catalog_warm_hours: str = "1-7"
    catalog_warm_poll_seconds: int = 900  # Enabled catalog imports cap polling at 5s.
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

    @model_validator(mode="after")
    def _fixtures_are_development_only(self) -> "Settings":
        """Fixture mode outside development or test is a configuration error.

        Fails at construction — which is import time for the app — so a
        misconfigured deployment refuses to start instead of serving invented
        course data under a real student's name.
        """
        if self.campus_fixture_mode and self.environment not in ("development", "test"):
            raise ValueError(
                "CAMPUS_FIXTURE_MODE is only valid when ENVIRONMENT is 'development' or 'test'; "
                f"this process has ENVIRONMENT={self.environment!r}. Synthetic campus data must "
                "never be served to real students."
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def jwks_url(self) -> str:
        return self.supabase_jwks_url or f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

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
    return Settings(_env_file=os.environ.get("DEVRIMO_ENV_FILE", ".env") or None)
