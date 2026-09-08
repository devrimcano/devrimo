"""TLS remains explicit when the same database uses asyncpg and libpq."""

import pytest
from sqlalchemy.engine import make_url

from app.agents.store import _sync_postgres_url
from app.db.engine import postgres_driver_url


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "prefer", "allow", "disable"])
def test_agno_preserves_tls_and_escaped_credentials(mode):
    original = make_url(f"postgresql+asyncpg://role.project:p%40ss%2Fword@pooler.example:5432/postgres?ssl={mode}")
    converted = make_url(_sync_postgres_url(original.render_as_string(hide_password=False)))
    assert converted.drivername == "postgresql+psycopg"
    assert converted.username == original.username
    assert converted.password == original.password
    assert converted.host == original.host
    assert converted.port == 5432
    assert converted.database == "postgres"
    assert converted.query == {"sslmode": mode}


def test_existing_libpq_settings_survive_and_roundtrip():
    url = "postgresql+psycopg://role:p@pooler.example/postgres?sslmode=verify-full&sslrootcert=%2Fcerts%2Fca.pem"
    sync = postgres_driver_url(url)
    assert sync.query == {"sslmode": "verify-full", "sslrootcert": "/certs/ca.pem"}
    assert postgres_driver_url(sync, "postgresql+asyncpg").query["ssl"] == "verify-full"


def test_conflicting_tls_options_fail_closed():
    with pytest.raises(ValueError, match="Conflicting"):
        postgres_driver_url("postgresql+asyncpg://u:p@host/db?ssl=require&sslmode=disable")


def test_unsupported_tls_value_is_not_silently_disabled():
    with pytest.raises(ValueError, match="Unsupported"):
        postgres_driver_url("postgresql+asyncpg://u:p@host/db?ssl=unexpected")


def test_staged_release_uses_only_explicit_migration_identity():
    import os
    import subprocess
    import sys
    from pathlib import Path

    from app.config import get_settings

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            # Local fixture PostgreSQL has no TLS certificate. The explicit
            # release-identity path must still ignore inconsistent runtime URLs;
            # production TLS rejection is exercised in test_database_hardening.
            "ENVIRONMENT": "test",
            "DATABASE_MIGRATION_URL": get_settings().database_url,
            "DATABASE_URL": "postgresql+asyncpg://postgres:unused@old-runtime.invalid/old",
            "ASSISTANT_DATABASE_URL": "postgresql+asyncpg://unused:unused@other.invalid/old",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
