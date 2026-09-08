"""JWT/RBAC-protected AgentOS over Devrimo's self-hosted Agno database."""

from agno.os import AgentOS
from agno.os.config import AuthorizationConfig
from agno.os.settings import AgnoAPISettings

from app.agents.store import get_agno_db
from app.config import get_settings


def build_agentos_app():
    settings = get_settings()
    if not settings.agentos_enabled:
        raise RuntimeError("AgentOS is disabled; set AGENTOS_ENABLED=true in the dedicated service")
    verification_key = settings.agentos_jwt_verification_key.strip()
    jwks_file = settings.agentos_jwks_file.strip()
    if not verification_key and not jwks_file:
        raise RuntimeError(
            "AGENTOS_JWT_VERIFICATION_KEY or AGENTOS_JWKS_FILE is required; shared security-key auth is not allowed"
        )

    agent_os = AgentOS(
        id="devrimo",
        name="Devrimo AgentOS",
        description="Internal, read-oriented operations surface for Scholar runs, evals, metrics, and traces.",
        db=get_agno_db(),
        # Deliberately register no runnable credential-bearing agent. Production
        # Scholar runs in the broker and writes sessions/traces to the shared DB.
        agents=[],
        authorization=True,
        authorization_config=AuthorizationConfig(
            verification_keys=[verification_key] if verification_key else None,
            jwks_file=jwks_file or None,
            algorithm=settings.agentos_jwt_algorithm,
            verify_audience=True,
            audience=settings.agentos_jwt_audience,
            admin_scope=settings.agentos_admin_scope,
            user_isolation=True,
        ),
        settings=AgnoAPISettings(
            env="production",
            docs_enabled=False,
            authorization_enabled=True,
            cors_origin_list=settings.agentos_cors_origin_list,
        ),
        tracing=False,
        telemetry=False,
        auto_provision_dbs=True,
    )
    return agent_os.get_app()
