"""The Agno database: where conversation history actually lives now.

Under Hermes each turn was stored inside the student's container, on its
volume, reachable only while that container was running — so reading last
week's conversation meant booting a 2 GB container, and losing the volume lost
the history. Agno stores runs in the same database the broker already uses, so
history is queryable, backed up with everything else, and readable whether or
not the student's agent is currently resident.

Agno's database layer is synchronous and manages its own engine, so it gets its
own URL derived from ``DATABASE_URL`` rather than sharing the app's async
engine. Same database, same tables namespace, separate pool.
"""

from functools import lru_cache

from agno.db.base import BaseDb

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

# Prefixed so Agno's tables are obviously not hand-written app tables when
# someone opens the schema, and so they can't collide with `chat_sessions`.
#
# Note that on Postgres these do not land beside the app's tables: `PostgresDb`
# defaults to its own `ai` schema, while Alembic owns `public`. Same database,
# two schemas — retention and backup policy has to name `ai` explicitly or it
# will miss every conversation.
_SESSION_TABLE = "agno_sessions"
_RUNS_TABLE = "agno_runs"
_MEMORY_TABLE = "agno_memories"
_METRICS_TABLE = "agno_metrics"
_EVAL_TABLE = "agno_eval_runs"
_KNOWLEDGE_TABLE = "agno_knowledge"
_TRACES_TABLE = "agno_traces"
_SPANS_TABLE = "agno_spans"
_VERSIONS_TABLE = "agno_schema_versions"
_COMPONENTS_TABLE = "agno_components"
_COMPONENT_CONFIGS_TABLE = "agno_component_configs"
_COMPONENT_LINKS_TABLE = "agno_component_links"
_LEARNINGS_TABLE = "agno_learnings"
_SCHEDULES_TABLE = "agno_schedules"
_SCHEDULE_RUNS_TABLE = "agno_schedule_runs"
_APPROVALS_TABLE = "agno_approvals"


def _sync_postgres_url(url: str) -> str:
    """Swap an async driver for the sync one Agno's engine needs."""
    for async_driver in ("+asyncpg", "+psycopg_async"):
        url = url.replace(async_driver, "+psycopg")
    if "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_agno_db() -> BaseDb:
    url = get_settings().database_url
    tables = {
        "id": "devrimo-agno",
        "session_table": _SESSION_TABLE,
        "runs_table": _RUNS_TABLE,
        "memory_table": _MEMORY_TABLE,
        "metrics_table": _METRICS_TABLE,
        "eval_table": _EVAL_TABLE,
        "knowledge_table": _KNOWLEDGE_TABLE,
        "traces_table": _TRACES_TABLE,
        "spans_table": _SPANS_TABLE,
        "versions_table": _VERSIONS_TABLE,
        "components_table": _COMPONENTS_TABLE,
        "component_configs_table": _COMPONENT_CONFIGS_TABLE,
        "component_links_table": _COMPONENT_LINKS_TABLE,
        "learnings_table": _LEARNINGS_TABLE,
        "schedules_table": _SCHEDULES_TABLE,
        "schedule_runs_table": _SCHEDULE_RUNS_TABLE,
        "approvals_table": _APPROVALS_TABLE,
    }

    from agno.db.postgres import PostgresDb

    return PostgresDb(db_url=_sync_postgres_url(url), **tables)
