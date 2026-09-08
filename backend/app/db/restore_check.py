"""Verify an application dump in a new disposable PostgreSQL database.

Never restores over a supplied database. RESTORE_ADMIN_URL is a maintenance
connection with CREATEDB; only a random devrimo_restore_* database is touched.
PG_BIN_DIR optionally selects the matching PostgreSQL client installation.
"""

import argparse
import os
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from app.db.engine import postgres_driver_url, require_postgres_tls


def _pg_tool(name: str) -> list[str]:
    return [str(Path(os.environ["PG_BIN_DIR"]) / name) if os.environ.get("PG_BIN_DIR") else name]


def _validated_url(url: str):
    parsed = postgres_driver_url(url, "postgresql")
    if parsed.host not in {None, "localhost", "127.0.0.1", "::1", "postgres"}:
        require_postgres_tls(parsed)
    return parsed


def _pg_environment(url: str) -> dict[str, str]:
    parsed = _validated_url(url)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for key, value in {"PGHOST": parsed.host, "PGPORT": parsed.port, "PGUSER": parsed.username,
                       "PGPASSWORD": parsed.password, "PGDATABASE": parsed.database}.items():
        if value is not None:
            env[key] = str(value)
    for key, value in parsed.query.items():
        env_key = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
                   "sslkey": "PGSSLKEY", "connect_timeout": "PGCONNECT_TIMEOUT", "options": "PGOPTIONS"}.get(key)
        if env_key is None or not isinstance(value, str):
            raise ValueError(f"Unsupported backup connection option: {key}")
        env[env_key] = value
    return env


def dump_application(source_url: str, output: Path) -> None:
    """Credentials travel through process environment, never CLI arguments."""
    env = _pg_environment(source_url)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as destination:
            subprocess.run(
                [*_pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-acl",
                 "--schema=public", "--schema=ai", "--schema=extensions",
                 "--extension=vector", "--extension=pg_trgm", "--enable-row-security"],
                env=env, stdout=destination, check=True,
            )
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def verify_restore(dump: Path, admin_url: str) -> dict:
    """Fail on any restore error, then verify both persistence schemas."""
    admin = _validated_url(admin_url)
    if admin.database != "postgres":
        raise ValueError("RESTORE_ADMIN_URL must target the postgres maintenance database")
    name = "devrimo_restore_" + uuid.uuid4().hex[:16]
    target = admin.set(database=name).render_as_string(hide_password=False)
    started = time.monotonic()
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as maintenance:
        # Policies in the dump reference these application groups. On an empty
        # drill server create only missing NOLOGIN placeholders, then remove them.
        created_roles = []
        maintenance.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            for owner in ("api", "assistant", "knowledge", "embedding", "researcher", "directory",
                          "catalog", "student", "planning", "backup"):
                role = f"devrimo_{owner}"
                if maintenance.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone() is None:
                    maintenance.execute(sql.SQL(
                        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role)))
                    created_roles.append(role)
            with psycopg.connect(target, autocommit=True) as empty:
                # template0 still includes an empty public schema, whereas a
                # schema-selected pg_dump explicitly recreates that schema.
                empty.execute("DROP SCHEMA public")
            with dump.open("rb") as source:
                subprocess.run(
                    [*_pg_tool("pg_restore"), "--dbname", name,
                     "--no-owner", "--no-acl", "--exit-on-error"],
                    env=_pg_environment(target), stdin=source, check=True,
                )
            with psycopg.connect(target) as restored:
                revision = restored.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
                schemas = dict(restored.execute(
                    "SELECT schemaname,count(*) FROM pg_tables WHERE schemaname IN ('public','ai') GROUP BY schemaname"
                ).fetchall())
                if not schemas.get("public") or not schemas.get("ai"):
                    raise RuntimeError("Restore is missing public or ai tables")
                restored.execute("SELECT * FROM ai.agno_sessions LIMIT 0")
                restored.execute("SELECT * FROM public.student_academic_snapshots LIMIT 0")
                row_counts = {}
                for (table,) in restored.execute(
                    "SELECT schemaname||'.'||tablename FROM pg_tables "
                    "WHERE schemaname IN ('public','ai') ORDER BY schemaname,tablename"
                ).fetchall():
                    schema, relation = table.split(".", 1)
                    row_counts[table] = restored.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(relation)
                    )).fetchone()[0]
                return {"revision": revision, "tables": schemas,
                        "row_counts": row_counts,
                        "restore_seconds": round(time.monotonic() - started, 2)}
        finally:
            maintenance.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in created_roles:
                maintenance.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    args = parser.parse_args()
    print(verify_restore(args.dump, os.environ["RESTORE_ADMIN_URL"]))


if __name__ == "__main__":
    main()
