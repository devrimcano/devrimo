import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db import models  # noqa: F401 -- registers models on Base.metadata
from app.db.base import Base
from app.db.engine import postgres_connect_args

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# A staged release connects before runtime configuration is switched over.
# Explicit release credentials therefore do not load or validate runtime dotenv
# credentials. Runtime processes still apply the full ownership/endpoint checks.
migration_url = os.environ.get("DATABASE_MIGRATION_URL")
if not migration_url:
    settings = get_settings()
    if settings.environment in {"production", "staging"}:
        raise RuntimeError("DATABASE_MIGRATION_URL is required for release migrations")
    migration_url = settings.database_migration_url or settings.database_url
if make_url(migration_url).get_backend_name() != "postgresql":
    raise RuntimeError("Release migrations require PostgreSQL")
config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=migration_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.execute("SET LOCAL search_path TO public,extensions")
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(migration_url, connect_args=postgres_connect_args(migration_url))
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
