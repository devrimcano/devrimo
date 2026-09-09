"""Give the API login room to restart without exhausting its connection limit."""

import sqlalchemy as sa

from alembic import op

revision = "0034_api_restart_connection_headroom"
down_revision = "0033_academic_catalog"
branch_labels = None
depends_on = None

# 0030 sized this login at 9 - exactly the API's pool of 8 plus its one
# overflow - which is the right number for one running process and the wrong
# number for a restart. The pooler in front of PostgreSQL keeps the outgoing
# server connections of the generation that just exited for a while after it
# exits, so for that window the old nine and the new one are both counted, and
# the new process is refused. On 2026-09-09 that was not theoretical: the API
# could not obtain a single connection, failed startup, and was restarted by
# systemd every five seconds into the same refusal, while students saw "fetch
# failed" and a planner that never loaded.
#
# Eighteen is two whole generations of that pool. The pool itself is not the
# thing to shrink: test_api_pool_has_room_for_four_campus_calls_and_their_
# nested_cache_reads holds it to four concurrent campus calls carrying two
# leases each plus a nested cache read, which is nine, and a smaller pool would
# make four students at once wait out the pool timeout. Every login's limit
# together now caps at 47 against this database's max_connections of 60, and
# real usage across all of them sits near 26.
API_LOGIN_LIMIT = 18


def _api_logins(db):
    return [
        login
        for (login,) in db.execute(
            sa.text("""
            SELECT rolname FROM pg_roles
            WHERE rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)
              AND pg_has_role(oid, 'devrimo_api', 'MEMBER')
        """)
        )
    ]


def upgrade():
    db = op.get_bind()
    for login in _api_logins(db):
        quoted = db.dialect.identifier_preparer.quote(login)
        op.execute(f"ALTER ROLE {quoted} CONNECTION LIMIT {API_LOGIN_LIMIT}")


def downgrade():
    db = op.get_bind()
    for login in _api_logins(db):
        quoted = db.dialect.identifier_preparer.quote(login)
        op.execute(f"ALTER ROLE {quoted} CONNECTION LIMIT 9")
