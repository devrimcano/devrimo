"""Scholar learning and context-compression configuration."""

from agno.compression.manager import CompressionManager
from agno.learn import LearningMachine, LearningMode, SessionContextConfig
from agno.models.base import Model

from app.agents.store import get_agno_db
from app.config import get_settings

MEMORY_POLICY = (
    "Save only stable, non-sensitive preferences the student explicitly asked you to remember. "
    "Never infer or save grades, transcript data, email contents, credentials, medical or disciplinary data, "
    "financial information, or protected/sensitive traits. Keep each memory short and include no tool output."
)


def build_learning(model: Model, *, enabled: bool | None = None) -> LearningMachine | None:
    settings = get_settings()
    # The echo model intentionally has no structured learning behavior; the
    # deterministic harness tests configuration without making background calls.
    if not (settings.agent_learning_enabled if enabled is None else enabled) or settings.agent_runtime == "fake":
        return None
    db = get_agno_db()
    return LearningMachine(
        db=db,
        model=model,
        max_updates_per_run=2,
        # Canonical explicit memories are injected by build_run_dependencies.
        # No extraction or hidden memory tool can bypass the seven operations.
        user_memory=False,
        session_context=SessionContextConfig(
            mode=LearningMode.ALWAYS,
            db=db,
            model=model,
            enable_planning=True,
            enable_add_context=True,
            enable_update_context=True,
            enable_delete_context=False,
            enable_clear_context=False,
            max_updates_per_run=1,
            instructions="Track only this session's goal, agreed plan, constraints, and completed progress.",
        ),
    )


def build_compression(model: Model) -> CompressionManager | None:
    settings = get_settings()
    if not settings.agent_compress_tool_results or settings.agent_runtime == "fake":
        return None
    return CompressionManager(
        model=model,
        compress_tool_results_limit=settings.agent_compress_tool_results_limit,
    )
