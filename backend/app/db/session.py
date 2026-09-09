import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.engine import database_pool_options, postgres_connect_args
from app.db.ownership import permitted_write

settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    **database_pool_options(settings),
    connect_args=postgres_connect_args(settings.database_url),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_session_factory(owner: str):
    """Return this process's factory, never acquire another owner's identity.

    API application services remain colocated during migration. A standalone
    worker can only request its own owner; PostgreSQL enforces the same boundary
    even if a worker imports SessionLocal directly.
    """
    if owner != settings.database_runtime_role and settings.database_runtime_role != "api":
        raise RuntimeError(f"{settings.database_runtime_role} cannot acquire {owner} database sessions")
    return SessionLocal


# A deploy restarts the API and seven workers within seconds of one another,
# and they all reach for the same pooled database. On 2026-09-09 one of those
# connections was accepted and then never answered: the API sat in this
# validation for twelve minutes, alive but not listening, while systemd - which
# only watches for a process that exits - saw nothing wrong. A bounded wait
# turns that hang into a crash, and Restart=always turns the crash into a
# retry five seconds later.
STARTUP_VALIDATION_TIMEOUT_SECONDS = 30


async def validate_runtime_database(expected_role: str | None = None) -> None:
    """Fail startup on excessive privileges, including inherited column grants."""
    role = settings.database_runtime_role
    if expected_role is not None and role != expected_role:
        raise RuntimeError(f"This process requires DATABASE_RUNTIME_ROLE={expected_role}")
    # Test/dev API keeps existing migration-owned fixtures. Real worker entry
    # points always validate, including local development.
    if expected_role is None and settings.environment not in {"production", "staging"}:
        return
    try:
        async with asyncio.timeout(STARTUP_VALIDATION_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.run_sync(lambda connection: _validate_connection(connection, role))
    except TimeoutError as exc:
        raise RuntimeError(
            "Database identity validation did not finish in "
            f"{STARTUP_VALIDATION_TIMEOUT_SECONDS}s; refusing to start"
        ) from exc


def validate_sync_database_identity(db_engine, role: str) -> None:
    with db_engine.connect() as conn:
        _validate_connection(conn, role)


def _validate_connection(conn, role: str) -> None:
    powerful = conn.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_roles r
            WHERE pg_has_role(current_user, r.oid, 'MEMBER')
            AND (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
        )
    """)
    )
    if powerful:
        raise RuntimeError("Runtime database identity can assume a privileged role")
    if conn.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM pg_roles r
          WHERE pg_has_role(current_user,r.oid,'MEMBER')
          AND has_database_privilege(r.oid,current_database(),'CREATE'))
    """)
    ):
        raise RuntimeError("Runtime database identity must not have database CREATE")
    schemas = conn.execute(
        text("""
        SELECT n.oid FROM pg_namespace n
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
    """)
    ).scalars()
    for schema_oid in schemas:
        if conn.scalar(
            text("""
            SELECT EXISTS (SELECT 1 FROM pg_roles r
              WHERE pg_has_role(current_user,r.oid,'MEMBER')
              AND has_schema_privilege(r.oid,CAST(:schema AS oid),'CREATE'))
        """),
            {"schema": schema_oid},
        ):
            raise RuntimeError("Runtime database identity must not have schema CREATE")
    rows = conn.execute(
        text("""
        SELECT n.nspname, c.relname, a.attname,
               has_table_privilege(r.oid, c.oid,
                   'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') AS table_write,
               has_column_privilege(r.oid, c.oid, a.attnum, 'INSERT,UPDATE,REFERENCES') AS column_write,
               pg_has_role(current_user, c.relowner, 'MEMBER') AS owns
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        JOIN pg_roles r ON pg_has_role(current_user,r.oid,'MEMBER')
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
        AND c.relkind IN ('r','p','v','m','f')
    """)
    )
    for schema, table, column, table_write, column_write, owns in rows:
        if owns:
            raise RuntimeError(f"Runtime database identity owns {schema}.{table}")
        if (table_write and not permitted_write(role, schema, table)) or (
            column_write and not permitted_write(role, schema, table, column)
        ):
            raise RuntimeError(f"Runtime database identity has foreign write access to {schema}.{table}")


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
