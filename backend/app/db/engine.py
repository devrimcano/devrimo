"""Connection policy shared by application, Agno and release migrations."""

from sqlalchemy.engine import URL, make_url

# Supabase installs pgvector/pg_trgm in extensions; local PostgreSQL commonly
# installs them in public. Neither schema grants CREATE to runtime identities.
# Omitting $user prevents a login-named schema from shadowing application SQL.
SEARCH_PATH = "public,extensions"


def postgres_driver_url(url: str | URL, driver: str = "postgresql+psycopg") -> URL:
    """Translate driver-specific TLS parameters without weakening their mode."""
    parsed = make_url(url)
    query = dict(parsed.query)
    source_key, target_key = ("sslmode", "ssl") if driver == "postgresql+asyncpg" else ("ssl", "sslmode")
    if source_key in query:
        mode = query.pop(source_key)
        if mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("Unsupported PostgreSQL TLS mode")
        if target_key in query and query[target_key] != mode:
            raise ValueError("Conflicting PostgreSQL TLS modes")
        query[target_key] = mode
    return parsed.set(drivername=driver, query=query)


def postgres_connect_args(url: str | URL) -> dict:
    driver = make_url(url).drivername
    if driver == "postgresql+asyncpg":
        return {"server_settings": {"search_path": SEARCH_PATH}}
    return {"options": f"-csearch_path={SEARCH_PATH}"}
