#!/usr/bin/env bash
# Shared helpers for the devrimo Codex Cloud environment.
#
# Sourced by setup.sh and maintenance.sh; not meant to be run directly. Every
# path is derived from this file's location, so it works whichever directory the
# Codex container clones the repo into.
set -euo pipefail

DEVRIMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export DEVRIMO_ROOT
export DEVRIMO_BACKEND="$DEVRIMO_ROOT/backend"
export DEVRIMO_FRONTEND="$DEVRIMO_ROOT/frontend"
export DEVRIMO_VENV="$DEVRIMO_BACKEND/.venv"
export DEVRIMO_STATE="$DEVRIMO_ROOT/.codex/cloud/.cache"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql://devrimo:devrimo@localhost:5432/postgres}"
export PG_BIN_DIR="${PG_BIN_DIR:-/usr/lib/postgresql/16/bin}"

# The cloud environment is never pointed at the live database. Everything the
# agent runs against is a disposable copy on this container's own PostgreSQL,
# built from the migrations in this repo -- schema only, never production rows.
# The copy exists so a hand-run server has a real database; the test suite still
# creates and drops its own throwaway database from TEST_DATABASE_URL.
export DEVRIMO_CODEX_DB="${DEVRIMO_CODEX_DB:-devrimo_codex}"
export DATABASE_URL="postgresql+asyncpg://devrimo:devrimo@localhost:5432/${DEVRIMO_CODEX_DB}"
export DATABASE_RUNTIME_ROLE="${DATABASE_RUNTIME_ROLE:-api}"

log() { printf '\n[devrimo] %s\n' "$*"; }

# A database URL that leaves this container is a configuration error, not a task
# to run. Checked before the copy is built or the environment is exported.
assert_local_database() {
  case "$1" in
    *@localhost:*|*@127.0.0.1:*|*//localhost*|*//127.0.0.1*) ;;
    *)
      echo "[devrimo] refusing a non-local database: $1" >&2
      return 1
      ;;
  esac
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

as_postgres() {
  if [ "$(id -u)" -eq 0 ]; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u postgres -- "$@"
    else
      su -s /bin/sh postgres -c "$*"
    fi
  else
    sudo -u postgres -- "$@"
  fi
}

pg_ready() { pg_isready -h 127.0.0.1 -p 5432 -q; }

start_postgres() {
  if pg_ready; then return 0; fi
  log "starting PostgreSQL 16"
  if ! as_root pg_ctlcluster 16 main start; then
    log "cluster not present; creating it"
    as_root pg_createcluster 16 main >/dev/null 2>&1 || true
    as_root pg_ctlcluster 16 main start || true
  fi
  for _ in $(seq 1 30); do
    if pg_ready; then return 0; fi
    sleep 1
  done
  echo "[devrimo] PostgreSQL did not become ready" >&2
  return 1
}

stop_postgres() {
  if pg_ready; then as_root pg_ctlcluster 16 main stop || true; fi
}

# The suite's conftest.py connects to TEST_DATABASE_URL as this role and creates
# and drops its own database, so it needs CREATEDB. It mirrors the `devrimo`
# superuser the GitHub Actions `verify` job provisions.
ensure_postgres_role() {
  start_postgres
  as_postgres psql -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'devrimo') THEN
    CREATE ROLE devrimo LOGIN;
  END IF;
END
$$;
ALTER ROLE devrimo WITH LOGIN SUPERUSER CREATEDB PASSWORD 'devrimo';
SQL
}

# Setup exports do not survive into the agent phase, so the agent-facing
# environment is written to ~/.bashrc instead. Guarded so re-running setup does
# not append the block twice.
ensure_bashrc() {
  local marker="# >>> devrimo codex cloud >>>"
  if grep -qF "$marker" "$HOME/.bashrc" 2>/dev/null; then return 0; fi
  cat >> "$HOME/.bashrc" <<EOF

$marker
export DEVRIMO_ROOT="$DEVRIMO_ROOT"
export PATH="$DEVRIMO_VENV/bin:\$PATH"
export PYTHON="$DEVRIMO_VENV/bin/python"
export TEST_DATABASE_URL="$TEST_DATABASE_URL"
export PG_BIN_DIR="$PG_BIN_DIR"
export DEVRIMO_CODEX_DB="$DEVRIMO_CODEX_DB"
export DATABASE_URL="$DATABASE_URL"
export DATABASE_RUNTIME_ROLE="$DATABASE_RUNTIME_ROLE"
# >>> devrimo codex cloud <<<
EOF
}

local_database_exists() {
  as_postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname = '$DEVRIMO_CODEX_DB'" | grep -qx 1
}

# Rebuild the disposable copy from the migrations. The schema is identical to
# production because it comes from the same Alembic chain, but the rows are not:
# nothing here is seeded from live student data.
reset_local_database() {
  assert_local_database "$DATABASE_URL"
  start_postgres
  log "building the local database copy $DEVRIMO_CODEX_DB (schema from migrations, no live rows)"
  as_postgres psql -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS \"$DEVRIMO_CODEX_DB\""
  as_postgres createdb -O devrimo "$DEVRIMO_CODEX_DB"
  (
    cd "$DEVRIMO_BACKEND"
    ENVIRONMENT=development \
      DATABASE_URL="$DATABASE_URL" \
      DATABASE_RUNTIME_ROLE=api \
      "$DEVRIMO_VENV/bin/python" -m alembic upgrade head
  )
}

ensure_local_database() {
  if ! local_database_exists; then
    reset_local_database
  fi
}

sync_backend_deps() {
  mkdir -p "$DEVRIMO_STATE"
  if [ ! -x "$DEVRIMO_VENV/bin/python" ]; then
    log "creating backend virtualenv at $DEVRIMO_VENV"
    python3 -m venv "$DEVRIMO_VENV"
    "$DEVRIMO_VENV/bin/python" -m pip install -q --upgrade pip
  fi
  local hash
  hash="$(cat "$DEVRIMO_BACKEND/requirements.txt" "$DEVRIMO_BACKEND/requirements-dev.txt" | sha256sum | cut -d' ' -f1)"
  if [ ! -f "$DEVRIMO_STATE/requirements.sha" ] || [ "$(cat "$DEVRIMO_STATE/requirements.sha")" != "$hash" ]; then
    log "installing backend dependencies"
    "$DEVRIMO_VENV/bin/python" -m pip install -q -r "$DEVRIMO_BACKEND/requirements-dev.txt"
    printf '%s' "$hash" > "$DEVRIMO_STATE/requirements.sha"
  else
    log "backend dependencies up to date"
  fi
}

sync_frontend_deps() {
  mkdir -p "$DEVRIMO_STATE"
  local hash
  hash="$(sha256sum "$DEVRIMO_FRONTEND/package-lock.json" | cut -d' ' -f1)"
  if [ ! -d "$DEVRIMO_FRONTEND/node_modules" ] \
    || [ ! -f "$DEVRIMO_STATE/package-lock.sha" ] \
    || [ "$(cat "$DEVRIMO_STATE/package-lock.sha")" != "$hash" ]; then
    log "installing frontend dependencies (npm ci)"
    ( cd "$DEVRIMO_FRONTEND" && npm ci )
    printf '%s' "$hash" > "$DEVRIMO_STATE/package-lock.sha"
  else
    log "frontend dependencies up to date"
  fi
}
