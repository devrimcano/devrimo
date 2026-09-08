"""Exercise actual PostgreSQL permission denials under separate login roles."""

import re
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.db.engine import postgres_connect_args
from app.db.session import validate_sync_database_identity


def create_engine(url):
    return sqlalchemy_create_engine(url, connect_args=postgres_connect_args(url))


@pytest.mark.parametrize(
    "owner,owned,foreign",
    [
        ("knowledge", "campus_ingestion_jobs", "student_timetables"),
        ("embedding", "knowledge_index_vectors", "campus_knowledge_records"),
        ("researcher", "researcher_import_runs", "campus_sources"),
        ("directory", "account_directory", "student_academic_snapshots"),
        ("catalog", "schedule_data_cache", "campus_credentials"),
        ("planning", "student_timetables", "student_academic_snapshots"),
        ("student", "student_academic_snapshots", "student_timetables"),
    ],
)
def test_worker_login_cannot_write_foreign_tables(owner, owned, foreign):
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    name = "boundary_" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
        admin.execute(f'GRANT devrimo_{owner} TO "{name}"')
        try:
            login = url.set(username=name, password=password)
            with psycopg.connect(login.render_as_string(hide_password=False), autocommit=True) as worker:
                # Even a zero-row update/delete must pass PostgreSQL permission checks.
                worker.execute(f'DELETE FROM "{owned}" WHERE false')
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute(f'DELETE FROM "{foreign}" WHERE false')
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("CREATE TABLE public.unowned_table (id int)")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("SET ROLE devrimo_api")
            runtime = create_engine(login.set(drivername="postgresql+psycopg"))
            try:
                validate_sync_database_identity(runtime, owner)
                with runtime.connect() as conn:
                    assert conn.scalar(text("SELECT vector_dims('[1,2,3]'::vector)")) == 3
                    assert conn.scalar(text("SELECT similarity('campus','campus')")) == 1
                    assert conn.scalar(text("SELECT '[1,0]'::vector <=> '[1,0]'::vector")) == 0
                    if conn.scalar(text("SELECT to_regnamespace('extensions') IS NOT NULL")):
                        assert not conn.scalar(text("SELECT has_schema_privilege('extensions','CREATE')"))
            finally:
                runtime.dispose()
        finally:
            admin.execute(f'DROP ROLE "{name}"')


def test_knowledge_can_only_change_source_progress():
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn:
        conn.execute("SET ROLE devrimo_knowledge")
        conn.execute("UPDATE campus_sources SET last_error=NULL WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE campus_sources SET enabled=false WHERE false")


def test_production_rejects_owner_url_and_multiple_databases():
    with pytest.raises(ValueError, match="restricted"):
        Settings(_env_file=None, environment="production", database_url="postgresql+asyncpg://postgres:x@db/app")
    with pytest.raises(ValueError, match="same PostgreSQL"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://api:x@db/app",
            assistant_database_url="postgresql+asyncpg://agent:x@db/other",
        )


@pytest.mark.parametrize("environment", ["development", "test"])
def test_development_and_test_allow_the_documented_placeholder_key(environment):
    settings = Settings(
        _env_file=None,
        environment=environment,
        database_url="postgresql+asyncpg://api:x@db/app",
        secret_encryption_key="change-me-to-a-real-generated-secret",
    )
    assert settings.environment == environment


@pytest.mark.parametrize("key", ["", "change-me-to-a-real-generated-secret", "test-encryption-key"])
def test_non_development_crypto_processes_reject_missing_or_placeholder_key(key):
    with pytest.raises(ValueError, match="SECRET_ENCRYPTION_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://api:x@db/app",
            secret_encryption_key=key,
        )


def test_non_crypto_workers_and_agentos_do_not_receive_the_encryption_key():
    knowledge = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://knowledge:x@db/app?ssl=require",
        database_runtime_role="knowledge",
        secret_encryption_key="",
        runtime_component="knowledge",
    )
    retention = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://catalog:x@db/app?ssl=require",
        database_runtime_role="catalog",
        secret_encryption_key="",
        runtime_component="retention",
    )
    agentos = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://assistant:x@db/app?ssl=require",
        database_runtime_role="assistant",
        secret_encryption_key="",
        runtime_component="agentos",
        agentos_enabled=True,
    )
    assert knowledge.database_runtime_role == "knowledge"
    assert retention.database_runtime_role == "catalog"
    assert agentos.agentos_enabled


def test_unrecognized_runtime_component_is_rejected():
    with pytest.raises(ValueError, match="DEVRIMO_RUNTIME_COMPONENT"):
        Settings(
            _env_file=None,
            environment="development",
            database_url="postgresql+asyncpg://api:x@db/app",
            runtime_component="unknown",
        )


def test_runtime_component_reads_the_devrimo_environment_variable(monkeypatch):
    monkeypatch.setenv("DEVRIMO_RUNTIME_COMPONENT", "retention")
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://catalog:x@db/app?ssl=require",
        database_runtime_role="catalog",
        secret_encryption_key="",
    )
    assert settings.runtime_component == "retention"


def test_crypto_rejects_placeholder_even_for_retention(monkeypatch):
    from app.core import crypto

    retention = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://catalog:x@db/app?ssl=require",
        database_runtime_role="catalog",
        runtime_component="retention",
        secret_encryption_key="",
    )
    monkeypatch.setattr(crypto, "get_settings", lambda: retention)
    crypto._fernet.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SECRET_ENCRYPTION_KEY"):
            crypto._fernet()
    finally:
        crypto._fernet.cache_clear()


def test_compose_worker_environment_is_explicit_and_does_not_share_flag_keys():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    services = (
        "broker",
        "knowledge-worker",
        "agentos",
        "embedding-worker",
        "researcher-worker",
        "directory-worker",
        "catalog-worker",
        "retention-worker",
        "assistant-worker",
    )

    def service_block(name):
        start = compose.index(f"  {name}:\n")
        match = re.search(r"^  [a-z0-9][a-z0-9-]*:\n", compose[start + 1 :], re.MULTILINE)
        end = start + 1 + match.start() if match else len(compose)
        return compose[start:end]

    for name in services:
        block = service_block(name)
        assert "env_file:" not in block
        assert 'DEVRIMO_ENV_FILE: ""' in block
        assert "NEXT_PUBLIC_" not in block
        if name not in {"broker", "assistant-worker"}:
            assert "POSTHOG_PERSONAL_API_KEY:" not in block

    assert "POSTHOG_PERSONAL_API_KEY:" in service_block("broker")
    assert "POSTHOG_PERSONAL_API_KEY:" in service_block("assistant-worker")
    assert "devrimo-campus-catalog-state:/var/lib/devrimo/campus" in service_block("catalog-worker")
    assert "devrimo-campus-state:/var/lib/devrimo/campus" not in service_block("catalog-worker")
    for name in ("broker", "embedding-worker", "catalog-worker", "assistant-worker"):
        assert "SECRET_ENCRYPTION_KEY:" in service_block(name)
    for name in ("knowledge-worker", "agentos", "researcher-worker", "directory-worker", "retention-worker"):
        assert "SECRET_ENCRYPTION_KEY:" not in service_block(name)


def test_validator_rejects_migration_owner():
    url = make_url(get_settings().database_url).set(drivername="postgresql+psycopg")
    runtime = create_engine(url)
    try:
        with pytest.raises(RuntimeError, match="privileged|owns|CREATE"):
            validate_sync_database_identity(runtime, "embedding")
    finally:
        runtime.dispose()


@pytest.mark.asyncio
async def test_asyncpg_validates_numeric_schema_oids():
    """Startup must bind the oid overload, not the schema-name text overload."""
    from app.db.session import _validate_connection, engine

    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE devrimo_embedding"))
        await connection.run_sync(lambda conn: _validate_connection(conn, "embedding"))


@pytest.mark.asyncio
async def test_runtime_and_agno_resolve_installed_extensions():
    import os

    from app.agents.store import get_agno_db
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        assert await db.scalar(text("SHOW search_path")) == "public,extensions"
        schemas = dict(
            (
                await db.execute(
                    text(
                        "SELECT e.extname,n.nspname FROM pg_extension e "
                        "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname IN ('vector','pg_trgm')"
                    )
                )
            ).all()
        )
        assert set(schemas.values()) == {os.environ.get("TEST_EXTENSION_SCHEMA", "public")}
        assert await db.scalar(text("SELECT vector_dims('[1,2,3]'::vector)")) == 3
    with get_agno_db().db_engine.connect() as conn:
        assert conn.scalar(text("SHOW search_path")) == "public,extensions"
        assert conn.scalar(text("SELECT similarity('campus','campus')")) == 1


def test_assistant_login_owns_history_but_not_student_records():
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    name = "assistant_boundary_" + uuid.uuid4().hex[:10]
    password = uuid.uuid4().hex
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
        admin.execute(f'GRANT devrimo_assistant TO "{name}"')
        try:
            login = url.set(username=name, password=password)
            with psycopg.connect(login.render_as_string(hide_password=False), autocommit=True) as worker:
                worker.execute("DELETE FROM ai.agno_sessions WHERE false")
                worker.execute("DELETE FROM workspace_memory_mutations WHERE false")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("DELETE FROM student_contexts WHERE false")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("DELETE FROM ai.agno_schema_versions WHERE false")
            runtime = create_engine(login.set(drivername="postgresql+psycopg"))
            try:
                validate_sync_database_identity(runtime, "assistant")
            finally:
                runtime.dispose()
        finally:
            admin.execute(f'DROP ROLE "{name}"')


def test_migration_backfills_embedding_config_without_worker_config_writes():
    """Exercise the additive data migration with a pre-existing encrypted config."""
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/0022_knowledge_index_generations.py"
    spec = importlib.util.spec_from_file_location("index_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    runtime = create_engine(make_url(get_settings().database_url).set(drivername="postgresql+psycopg"))
    try:
        with runtime.begin() as connection:
            for width in (384, 768, 1536):
                connection.execute(
                    text(f"ALTER TABLE campus_knowledge_records ADD COLUMN embedding_{width} vector({width})")
                )
            connection.execute(text("ALTER TABLE campus_knowledge_records ADD COLUMN embedding_model text"))
            organization = uuid.uuid4()
            connection.execute(
                text("INSERT INTO organizations(id,slug,name) VALUES (:id,:slug,'Test')"),
                {
                    "id": organization,
                    "slug": str(organization),
                },
            )
            connection.execute(
                text("""
                INSERT INTO knowledge_embedding_settings
                (organization_id,provider,model,base_url,dimensions,batch_size,query_prefix,document_prefix,api_key_enc)
                VALUES (:id,'remote','test-model','https://example.invalid',384,8,'query: ','passage: ',:key)
            """),
                {"id": organization, "key": b"ciphertext-is-preserved"},
            )
            # Schema already migrated by the fixture. Execute only the frozen
            # backfill, under the same Alembic Operations context as deployment.
            operations = Operations(MigrationContext.configure(connection))
            with patch.object(migration, "op", operations), patch.object(migration, "DDL", []):
                migration.upgrade()
            row = connection.execute(
                text("""
                SELECT g.provider,g.model_label,g.api_key_enc,a.generation_id=g.id AS active
                FROM knowledge_index_generations g JOIN knowledge_index_activations a
                ON a.organization_id=g.organization_id WHERE g.organization_id=:id
            """),
                {"id": organization},
            ).one()
            assert row.provider == "remote"
            assert row.model_label.startswith("remote:test-model:384:")
            assert row.api_key_enc == b"ciphertext-is-preserved"
            assert row.active
            connection.rollback()
    finally:
        runtime.dispose()


async def test_embedding_job_runs_with_only_embedding_login(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import (
        CampusKnowledgeRecord,
        CampusSource,
        CampusSourceRevision,
        KnowledgeEmbeddingSettings,
        Organization,
    )
    from app.db.session import SessionLocal
    from app.knowledge.index_models import KnowledgeIndexVector
    from app.knowledge.indexes import claim_index_job, create_generation, process_index_batch

    async def embeddings(config, texts):
        return [[1.0] + [0.0] * 383 for _ in texts]

    monkeypatch.setattr("app.knowledge.embeddings._request_embeddings", embeddings)
    async with SessionLocal() as db:
        organization = Organization(slug="scoped-worker", name="Scoped worker")
        db.add(organization)
        await db.flush()
        source = CampusSource(
            organization_id=organization.id, name="Library", kind="curated", enabled=True, status="published"
        )
        config = KnowledgeEmbeddingSettings(
            organization_id=organization.id, provider="local", model="test", dimensions=384, batch_size=8
        )
        db.add_all([source, config])
        await db.flush()
        revision = CampusSourceRevision(source_id=source.id, revision=1, status="published", config={})
        db.add(revision)
        await db.flush()
        source.active_revision_id = revision.id
        record = CampusKnowledgeRecord(
            source_id=source.id,
            source_revision_id=revision.id,
            external_id="library",
            record_type="guide",
            title="Library",
            content="Library hours",
            content_hash="f" * 64,
        )
        db.add(record)
        await db.flush()
        generation = await create_generation(db, organization.id)
        generation_id = generation.id
        await db.commit()
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    name = "embedding_job_" + uuid.uuid4().hex[:10]
    password = uuid.uuid4().hex
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
        admin.execute(f'GRANT devrimo_embedding TO "{name}"')
        worker_url = url.set(drivername="postgresql+asyncpg", username=name, password=password)
        worker_engine = create_async_engine(worker_url, connect_args=postgres_connect_args(worker_url))
        try:
            sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
            async with sessions() as db:
                lease = await claim_index_job(db, "scoped")
                assert lease is not None and lease[0] == generation_id
                assert await process_index_batch(db, generation_id, "scoped", lease[1])
                stored = await db.scalar(
                    select(KnowledgeIndexVector).where(KnowledgeIndexVector.generation_id == generation_id)
                )
                assert stored is not None and len(stored.embedding_384) == 384
            with psycopg.connect(
                url.set(username=name, password=password).render_as_string(hide_password=False), autocommit=True
            ) as login:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    login.execute("UPDATE knowledge_index_generations SET model='foreign' WHERE false")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    login.execute("DELETE FROM knowledge_index_activations WHERE false")
        finally:
            await worker_engine.dispose()
            admin.execute(f'DROP ROLE "{name}"')


def test_noninherited_foreign_role_membership_is_rejected():
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    name = "dormant_boundary_" + uuid.uuid4().hex[:10]
    password = uuid.uuid4().hex
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE \"{name}\" LOGIN NOINHERIT PASSWORD '{password}'")
        admin.execute(f'GRANT devrimo_embedding,devrimo_api TO "{name}"')
        runtime = create_engine(url.set(drivername="postgresql+psycopg", username=name, password=password))
        try:
            with pytest.raises(RuntimeError, match="foreign write"):
                validate_sync_database_identity(runtime, "embedding")
        finally:
            runtime.dispose()
            admin.execute(f'DROP ROLE "{name}"')
