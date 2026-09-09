import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import jwt
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth.jwt import verify_access_token
from app.config import Settings, get_settings
from app.db.engine import database_pool_options, postgres_connect_args
from app.db.release_roles import provision_release_roles

ROOT = Path(__file__).resolve().parents[1]


def test_public_migration_metadata_is_denied_even_with_supabase_default_grants():
    url = make_url(get_settings().database_url).set(drivername="postgresql+psycopg")
    engine = create_engine(url, connect_args=postgres_connect_args(url))
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            for role in ("anon", "authenticated", "service_role"):
                if not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"), {"r": role}):
                    conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
                conn.execute(text(f'GRANT ALL ON public.alembic_version TO "{role}"'))
            migration = runpy.run_path(str(ROOT / "alembic/versions/0030_database_hardening.py"))
            with Operations.context(MigrationContext.configure(conn)):
                migration["upgrade"]()
            for role in ("anon", "authenticated", "service_role"):
                assert not conn.scalar(text(
                    "SELECT has_table_privilege(:r,'public.alembic_version','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')"
                ), {"r": role})
            assert conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid='public.alembic_version'::regclass"))
            conn.execute(text("SET LOCAL ROLE anon"))
            with conn.begin_nested():
                with pytest.raises(Exception, match="permission denied"):
                    # A zero-row write still checks SQL permissions.
                    with conn.begin_nested():
                        conn.execute(text("DELETE FROM public.alembic_version WHERE false"))
            conn.execute(text("RESET ROLE"))
            transaction.rollback()
    finally:
        engine.dispose()


def test_schema_drift_gate_is_clean_and_detects_unexpected_column():
    env = {**os.environ, "DATABASE_MIGRATION_URL": get_settings().database_url}
    def check():
        return subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=ROOT, env=env,
                              text=True, capture_output=True)
    clean = check()
    assert clean.returncode == 0, clean.stdout + clean.stderr
    engine = create_engine(make_url(get_settings().database_url).set(drivername="postgresql+psycopg"))
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE campus_knowledge_records ADD COLUMN audit_unexpected text"))
        drift = check()
        assert drift.returncode != 0
        assert "audit_unexpected" in drift.stdout + drift.stderr
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE campus_knowledge_records DROP COLUMN IF EXISTS audit_unexpected"))
        engine.dispose()


@pytest.mark.parametrize("mode", ["", "?ssl=disable", "?sslmode=allow", "?ssl=prefer"])
def test_production_rejects_unencrypted_or_opportunistic_connections(mode):
    with pytest.raises(ValueError, match="Production PostgreSQL"):
        Settings(_env_file=None, environment="production", database_runtime_role="knowledge",
                 database_url=f"postgresql+asyncpg://knowledge:x@db/app{mode}")


def test_the_api_login_can_carry_two_generations_of_its_pool():
    """A restart overlaps, and the login limit has to survive the overlap.

    The pooler in front of PostgreSQL keeps the outgoing connections of the
    process that just exited while the replacement is opening its own, so for
    that window both pools count against one login. When the limit was exactly
    one pool, the replacement could not obtain a single connection: it failed
    startup, systemd restarted it into the same refusal every five seconds, and
    the site stayed down until the old connections aged out.
    """
    url = make_url(get_settings().database_url).set(drivername="postgresql+psycopg")
    engine = create_engine(url, connect_args=postgres_connect_args(url))
    login = "devrimo_api_restart_probe"
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            conn.execute(text(
                f"CREATE ROLE {login} LOGIN PASSWORD 'probe' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ))
            conn.execute(text(f"GRANT devrimo_api TO {login}"))
            migration = runpy.run_path(str(ROOT / "alembic/versions/0035_api_restart_connection_headroom.py"))
            with Operations.context(MigrationContext.configure(conn)):
                migration["upgrade"]()
            granted = conn.scalar(text("SELECT rolconnlimit FROM pg_roles WHERE rolname=:name"), {"name": login})
            pool = database_pool_options(get_settings())
            assert granted >= 2 * (pool["pool_size"] + pool["max_overflow"])
            transaction.rollback()
    finally:
        engine.dispose()


def test_pool_budget_bounds_both_engines():
    settings = Settings(_env_file=None)
    app = database_pool_options(settings)
    agno = database_pool_options(settings, agno=True)
    worker = database_pool_options(Settings(_env_file=None, database_runtime_role="knowledge"))
    assert (app["pool_size"] + app["max_overflow"]) + 7 * (
        worker["pool_size"] + worker["max_overflow"]
    ) + 2 * agno["pool_size"] == 32
    assert app["pool_timeout"] == 15
    assert agno["max_overflow"] == 0


async def test_api_pool_has_room_for_four_campus_calls_and_their_nested_cache_reads():
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.database_url, **database_pool_options(settings))
    barrier = asyncio.Barrier(4)

    async def campus_call():
        # Match request + integration advisory-lock + transient cache connection
        # nesting. All four reach cache access with their two leases held.
        async with engine.connect(), engine.connect():
            await barrier.wait()
            async with engine.connect() as cached:
                assert await cached.scalar(text("SELECT 1")) == 1

    try:
        await asyncio.wait_for(asyncio.gather(*(campus_call() for _ in range(4))), timeout=5)
    finally:
        await engine.dispose()


def test_equivalent_url_spellings_cannot_share_the_runtime_migration_login():
    with pytest.raises(ValueError, match="credentials must be separate"):
        Settings(_env_file=None, environment=" Production ",
                 database_url="postgresql+asyncpg://api:x@db/app?ssl=require",
                 database_migration_url="postgresql+asyncpg://api:x@db/app?sslmode=require")


def test_scoped_release_owner_and_backup_reader_have_only_intended_access():
    engine = create_engine(make_url(get_settings().database_url).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            provision_release_roles(conn)
            conn.execute(text("SET LOCAL ROLE devrimo_release"))
            conn.execute(text("ALTER TABLE public.alembic_version ADD COLUMN scope_test int"))
            conn.execute(text("ALTER TABLE public.alembic_version DROP COLUMN scope_test"))
            conn.execute(text("CREATE SEQUENCE public.backup_future_sequence"))
            assert not conn.scalar(text("SELECT rolbypassrls OR rolcreatedb OR rolcreaterole OR rolsuper "
                                        "FROM pg_roles WHERE rolname=current_user"))
            conn.execute(text("RESET ROLE"))
            conn.execute(text("SET LOCAL ROLE devrimo_backup"))
            assert conn.scalar(text("SELECT version_num FROM public.alembic_version"))
            assert conn.scalar(text("SELECT last_value FROM public.backup_future_sequence")) == 1
            conn.execute(text("SELECT * FROM ai.agno_sessions LIMIT 0"))
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'public.alembic_version','UPDATE')"))
            assert not conn.scalar(text("SELECT has_schema_privilege(current_user,'public','CREATE')"))
            conn.execute(text("RESET ROLE"))
            transaction.rollback()
    finally:
        engine.dispose()


def test_explicit_jwks_endpoint_cannot_replace_project_trust():
    with pytest.raises(ValueError, match="SUPABASE_JWKS_URL"):
        Settings(_env_file=None, supabase_url="https://example.supabase.co",
                 supabase_jwks_url="https://different.example/jwks")
    settings = Settings(_env_file=None, supabase_url="https://example.supabase.co",
                        supabase_jwks_url="https://example.supabase.co/auth/v1/.well-known/jwks.json")
    assert settings.jwks_url == settings.supabase_jwks_url


@pytest.mark.parametrize("issuer", [None, "https://different.supabase.co/auth/v1"])
def test_valid_signature_with_wrong_or_missing_issuer_is_rejected(issuer):
    from fastapi import HTTPException

    settings = get_settings()
    claims = {"sub": "00000000-0000-0000-0000-000000000001", "aud": "authenticated", "exp": time.time() + 300}
    if issuer is not None:
        claims["iss"] = issuer
    token = jwt.encode(claims, settings.supabase_jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as caught:
        verify_access_token(token)
    assert caught.value.status_code == 401
