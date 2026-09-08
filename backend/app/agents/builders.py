"""Agent profile dispatch."""

from uuid import UUID

from agno.agent import Agent
from agno.tools.mcp import MCPTools

from app.agents.legacy import build_legacy_agent
from app.agents.runtime import AgentRuntimeConfig
from app.observability.flags import FLAG_AGENT_PROFILE, flag_variant


def build_agent(user_id: UUID, connected: list[MCPTools], runtime: AgentRuntimeConfig) -> Agent:
    # AGENT_PROFILE is the default; the flag makes the documented rollback to
    # `legacy` a runtime decision rather than a redeploy, and lets it be rolled
    # back for one affected student rather than everybody.
    profile = flag_variant(
        FLAG_AGENT_PROFILE,
        default=runtime.profile,
        distinct_id=str(user_id),
    ).lower()
    if profile == "legacy":
        return build_legacy_agent(user_id, connected, runtime)
    if profile == "scholar":
        from app.agents.scholar.build import build_scholar_agent

        return build_scholar_agent(connected, runtime, user_id=user_id)
    raise ValueError(f"Unknown AGENT_PROFILE: {profile!r}")
