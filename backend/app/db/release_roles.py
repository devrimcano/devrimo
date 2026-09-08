"""One-time administrator provisioning of scoped release/backup identities.

Run after the existing release migrations. Set passwords through psql's
interactive password command, never source control or command arguments.
"""

from sqlalchemy import text


def maintain_backup_access(connection) -> None:
    """Include newly migrated application tables in read-only backup coverage."""
    if not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='devrimo_backup')")):
        return
    quote = connection.dialect.identifier_preparer.quote
    for schema, table in connection.execute(text(
        "SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('public','ai') "
        "AND tablename NOT IN (SELECT c.relname FROM pg_class c JOIN pg_depend d ON d.objid=c.oid "
        "WHERE d.deptype='e')"
    )):
        name = f"{quote(schema)}.{quote(table)}"
        connection.execute(text(f"GRANT SELECT ON TABLE {name} TO devrimo_backup"))
        connection.execute(text(f"DROP POLICY IF EXISTS devrimo_backup_read ON {name}"))
        connection.execute(text(
            f"CREATE POLICY devrimo_backup_read ON {name} FOR SELECT TO devrimo_backup USING (true)"
        ))
    for schema, sequence in connection.execute(text("""
        SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('public','ai') AND c.relkind='S'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')
    """)):
        connection.execute(text(f"GRANT SELECT ON SEQUENCE {quote(schema)}.{quote(sequence)} TO devrimo_backup"))


def provision_release_roles(connection) -> None:
    """Transfer only application objects; leave Supabase/extension ownership alone."""
    quote = connection.dialect.identifier_preparer.quote
    for role in ("devrimo_release", "devrimo_backup"):
        if connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"), {"r": role}):
            raise RuntimeError(f"{role} already exists; inspect it before provisioning")
        connection.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                "NOREPLICATION NOBYPASSRLS"))
    # Membership permits a managed Postgres administrator to transfer ownership.
    admin = quote(connection.scalar(text("SELECT current_user")))
    connection.execute(text(f"GRANT devrimo_release TO {admin}"))
    for schema in ("public", "ai"):
        connection.execute(text(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO devrimo_release"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO devrimo_backup"))
    if connection.scalar(text("SELECT to_regnamespace('extensions') IS NOT NULL")):
        connection.execute(text("GRANT USAGE ON SCHEMA extensions TO devrimo_release,devrimo_backup"))
    # Serial sequences follow their table owner; standalone sequences are handled
    # separately. Never transfer extension-owned objects (vector, pg_trgm, etc.).
    objects = connection.execute(text("""
        SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('public','ai') AND c.relkind IN ('r','p','S')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')
        ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END
    """)).all()
    for schema, name, kind in objects:
        object_type = "SEQUENCE" if kind == "S" else "TABLE"
        connection.execute(text(f"ALTER {object_type} {quote(schema)}.{quote(name)} OWNER TO devrimo_release"))
    maintain_backup_access(connection)
    for schema in ("public", "ai"):
        connection.execute(text(f"ALTER DEFAULT PRIVILEGES FOR ROLE devrimo_release IN SCHEMA {schema} "
                                "GRANT SELECT ON TABLES TO devrimo_backup"))
        connection.execute(text(f"ALTER DEFAULT PRIVILEGES FOR ROLE devrimo_release IN SCHEMA {schema} "
                                "GRANT SELECT ON SEQUENCES TO devrimo_backup"))
    connection.execute(text(f"REVOKE devrimo_release FROM {admin}"))


def main() -> None:
    import os

    from sqlalchemy import create_engine

    from app.db.engine import postgres_connect_args, postgres_driver_url, require_postgres_tls

    url = postgres_driver_url(os.environ["DATABASE_BOOTSTRAP_URL"])
    if url.host not in {"localhost", "127.0.0.1", "postgres"}:
        require_postgres_tls(url)
    engine = create_engine(url, connect_args=postgres_connect_args(url))
    try:
        with engine.begin() as connection:
            provision_release_roles(connection)
    finally:
        engine.dispose()
    print("Scoped release and backup roles provisioned without logins; set passwords before enabling LOGIN.")


if __name__ == "__main__":
    main()
