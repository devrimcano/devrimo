"""Agent build dispatch: Scholar is the only profile left."""

from uuid import UUID

from agno.agent import Agent

from app.agents.runtime import AgentRuntimeConfig


def build_agent(user_id: UUID, runtime: AgentRuntimeConfig, *, session_id: str | None = None) -> Agent:
    from app.agents.scholar.build import build_scholar_agent

    return build_scholar_agent(runtime, user_id=user_id, session_id=session_id)
