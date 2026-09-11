#!/usr/bin/env bash
# Codex Cloud environment -- setup script for the devrimo repo.
#
# Paste this into the Codex environment "Setup script" field:
#
#     bash .codex/cloud/setup.sh
#
# It runs once per cache build with internet access and is idempotent. The
# environment it builds mirrors the `verify` job in
# .github/workflows/deploy.yml: PostgreSQL 16 + pgvector, a backend virtualenv
# with requirements-dev.txt, and frontend node_modules. There is no local
# Postgres in this repo otherwise, so the backend suite cannot run without it.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

log "repo root: $DEVRIMO_ROOT"

if ! command -v pg_ctlcluster >/dev/null 2>&1 || ! dpkg -s postgresql-16-pgvector >/dev/null 2>&1; then
  log "installing PostgreSQL 16, its client and pgvector from PGDG"
  as_root apt-get update -qq
  as_root apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg lsb-release
  as_root install -d /usr/share/postgresql-common/pgdg
  as_root curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  as_root sh -c "echo \"deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt \$(lsb_release -cs)-pgdg main\" > /etc/apt/sources.list.d/pgdg.list"
  as_root apt-get update -qq
  as_root apt-get install -y -qq --no-install-recommends \
    postgresql-16 postgresql-client-16 postgresql-16-pgvector
else
  log "PostgreSQL 16 + pgvector already installed"
fi

ensure_postgres_role
as_postgres psql -v ON_ERROR_STOP=1 -q -c \
  "SELECT 'pgvector present' WHERE EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')" >/dev/null

sync_backend_deps
sync_frontend_deps
ensure_bashrc

# The snapshot that Codex caches is a filesystem image, not a running machine,
# so PostgreSQL would be left with a dirty data directory if it kept running.
# Stop it cleanly; maintenance.sh starts it again on every resume.
log "stopping PostgreSQL so the cached snapshot is clean"
stop_postgres

log "setup complete: backend .venv, frontend node_modules, PostgreSQL 16 + pgvector"
