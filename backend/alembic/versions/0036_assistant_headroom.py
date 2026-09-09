"""Give assistant workers enough login headroom to restart."""

import sqlalchemy as sa

from alembic import op

revision = "0036_assistant_headroom"
down_revision = "0035_api_connection_headroom"
branch_labels = None
depends_on = None

# The API keeps one assistant-owned Agno connection. The assistant worker has
# an async pool of 2+1 and a separate Agno pool of 1. During a restart the
# pooler can still count the retiring worker's four connections while the
# replacement opens four more: 1 + 4 + 4 = 9. Together with the other bounded
# runtime logins, this caps application roles at 51 of PostgreSQL's 60
# connections and leaves nine for Supabase and release operations.
ASSISTANT_LOGIN_LIMIT = 9
PREVIOUS_ASSISTANT_LOGIN_LIMIT = 5


def _assistant_logins(db):
    return [
        login
        for (login,) in db.execute(
            sa.text("""
            SELECT rolname FROM pg_roles
            WHERE rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)
              AND pg_has_role(oid, 'devrimo_assistant', 'MEMBER')
        """)
        )
    ]


def _set_limit(limit: int) -> None:
    db = op.get_bind()
    for login in _assistant_logins(db):
        quoted = db.dialect.identifier_preparer.quote(login)
        op.execute(f"ALTER ROLE {quoted} CONNECTION LIMIT {limit}")


def upgrade() -> None:
    _set_limit(ASSISTANT_LOGIN_LIMIT)


def downgrade() -> None:
    _set_limit(PREVIOUS_ASSISTANT_LOGIN_LIMIT)
