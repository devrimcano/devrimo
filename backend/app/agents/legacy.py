"""The pre-Scholar agent profile, kept as a reversible rollout target."""

from pathlib import Path
from uuid import UUID

from agno.agent import Agent
from agno.tools.mcp import MCPTools

from app.agents.models import build_model
from app.agents.runtime import AgentRuntimeConfig
from app.agents.store import get_agno_db

_PERSONA_PATH = Path(__file__).with_name("persona.md")


def build_legacy_agent(user_id: UUID, connected: list[MCPTools], runtime: AgentRuntimeConfig) -> Agent:
    return Agent(
        id=f"devrimo-campus-{user_id}",
        name="Devrimo Campus Agent",
        model=build_model(runtime),
        db=get_agno_db(),
        tools=list(connected),
        instructions=_PERSONA_PATH.read_text(encoding="utf-8"),
        add_history_to_context=True,
        num_history_runs=runtime.legacy_history_runs,
        add_datetime_to_context=True,
        markdown=True,
        telemetry=False,
    )
