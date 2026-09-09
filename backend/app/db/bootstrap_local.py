"""Provision restricted logins for the disposable local Compose database.

Never use these development passwords outside the host-only local stack.
Run after Alembic with the local migration identity; no secrets are printed.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.engine import postgres_connect_args


async def main():
    settings = get_settings()
    url = make_url(settings.database_migration_url or settings.database_url)
    if settings.environment != "development" or url.host not in {"localhost", "127.0.0.1", "postgres"}:
        raise RuntimeError("Local bootstrap is restricted to the development database")
    engine = create_async_engine(url, connect_args=postgres_connect_args(url))
    try:
        async with engine.begin() as connection:
            for owner in (
                "api",
                "knowledge",
                "embedding",
                "researcher",
                "directory",
                "catalog",
                "assistant",
                "planning",
                "student",
            ):
                login = f"devrimo_{owner}_local"
                if not await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:name)"), {"name": login}
                ):
                    await connection.execute(
                        text(
                            f"CREATE ROLE {login} LOGIN PASSWORD 'devrimo_local' "
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                        )
                    )
                await connection.execute(text(f"GRANT devrimo_{owner} TO {login}"))
                # Same numbers the release migrations grant, so a local run
                # hits the same connection ceiling production does. The API's
                # is two generations of its pool; see 0034.
                limit = {"api": 18, "assistant": 5, "catalog": 6}.get(owner, 3)
                await connection.execute(text(f"ALTER ROLE {login} CONNECTION LIMIT {limit}"))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
