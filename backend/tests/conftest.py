import asyncio
import atexit
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ["AGENT_RUNTIME"] = "fake"
# Belt and braces: the Agent is constructed with telemetry=False, but a test
# must never be able to reach os-api.agno.com even if that regresses.
os.environ["AGNO_TELEMETRY"] = "false"

# No test run may reach PostHog. ``Settings`` reads the developer's real
# ``backend/.env``, so a machine with a working key turned every assertion that
# exercised a capture path into a production event — which is how a literal
# ``some_event`` and a pytest ``ValueError: boom`` ended up in the live
# project's error tracking. Blanked here, before ``app`` is imported anywhere,
# because the client is built once and memoised.
#
# ``POSTHOG_ENABLED`` is deliberately left at its default: the middleware is
# gated on it, and the tests that assert identity binding turn telemetry on by
# monkeypatching the key onto the settings object.
os.environ["POSTHOG_API_KEY"] = ""
os.environ["POSTHOG_PERSONAL_API_KEY"] = ""
os.environ["ENVIRONMENT"] = "test"
os.environ["RELEASE"] = "test"
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-encryption-key")
os.environ.setdefault("AGENT_IDLE_TIMEOUT_SECONDS", "3600")

_tmp_root = Path(tempfile.gettempdir()) / f"devrimo-test-{uuid.uuid4().hex}"
_tmp_root.mkdir(parents=True, exist_ok=True)

# Retrieval is implemented in PostgreSQL — stemmed full-text search, trigram
# similarity and pgvector ANN — so the suite runs against a real server rather
# than a substitute engine that cannot express any of it. TEST_DATABASE_URL
# points at the maintenance database; each run gets its own throwaway database.
_ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://devrimo:devrimo@localhost:5432/postgres")
_TEST_DB = f"devrimo_test_{uuid.uuid4().hex[:12]}"


def _admin_connection():
    import psycopg

    return psycopg.connect(_ADMIN_URL, autocommit=True)


with _admin_connection() as _conn:
    _conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
    _conn.execute(f'CREATE DATABASE "{_TEST_DB}"')

_base, _, _ = _ADMIN_URL.rpartition("/")
os.environ["DATABASE_URL"] = f"{_base}/{_TEST_DB}".replace("postgresql://", "postgresql+asyncpg://", 1)

# The schema comes from the migrations, not from ``create_all``: the search
# columns are Postgres generated columns and the indexes are GIN/HNSW, none of
# which the model metadata carries. Running them here also means every test
# exercises the exact DDL production receives.
_migration = subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    cwd=Path(__file__).resolve().parent.parent,
    env={**os.environ},
    capture_output=True,
    text=True,
)
if _migration.returncode:
    raise RuntimeError(f"Test database migration failed:\n{_migration.stderr}")


def _drop_test_database() -> None:
    from app.db.session import engine as _engine

    asyncio.run(_engine.dispose())
    with _admin_connection() as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (_TEST_DB,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')


atexit.register(_drop_test_database)
# Campus servers are never actually launched under AGENT_RUNTIME=fake, but the
# spec renderer still builds paths from these and the toolset builder still
# creates working directories — both of which must stay inside the sandbox.
os.environ["CAMPUS_MCP_ROOT"] = str(_tmp_root / "mcp")
os.environ["CAMPUS_STATE_ROOT"] = str(_tmp_root / "state")

import httpx
import jwt as pyjwt
import pytest
from sqlalchemy import text

from app.agents.pool import reset_pool
from app.agents.store import get_agno_db
from app.db.base import Base
from app.db.session import engine
from app.main import app

CAMPUS_MCP_ROOT = os.environ["CAMPUS_MCP_ROOT"]
CAMPUS_STATE_ROOT = os.environ["CAMPUS_STATE_ROOT"]


async def _drop_agno_tables(conn) -> None:
    """Agno creates its own tables, so ``Base.metadata`` cannot drop them.

    Without this they outlive the fixture and a session id reused by a later
    test resolves to the previous test's user — which looks exactly like a
    cross-user history leak, and would mask a real one. On Postgres they live in
    Agno's own ``ai`` schema rather than beside the application's tables.
    """
    result = await conn.execute(
        text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE tablename LIKE 'agno_%' AND schemaname NOT IN ('pg_catalog', 'information_schema')"
        )
    )
    for schema, table in result.fetchall():
        await conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE'))


@pytest.fixture(autouse=True)
async def _fresh_schema():
    # The migrations built the schema once for the whole session, so each test
    # only needs the rows cleared. Truncating keeps the generated search columns
    # and their GIN/HNSW indexes in place, which a drop/create cycle would lose.
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await _drop_agno_tables(conn)
    await reset_pool()
    # The Agno db caches which of its tables it has already created, so it must
    # be rebuilt alongside the schema or it will write to tables the previous
    # test's teardown dropped.
    get_agno_db.cache_clear()
    yield
    # Resident agents hold MCP subprocesses; a test that leaves one behind
    # leaks it into the next test's pool.
    await reset_pool()
    # Each test runs in its own event loop, and asyncpg connections are bound to
    # the loop that opened them. Returning one to the pool would hand the next
    # test a connection whose futures belong to a closed loop.
    await engine.dispose()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def new_user_id() -> uuid.UUID:
    return uuid.uuid4()


def auth_header(user_id: uuid.UUID) -> dict[str, str]:
    token = pyjwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "email": f"{user_id}@example.edu",
        },
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}
