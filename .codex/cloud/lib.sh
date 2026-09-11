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

log() { printf '\n[devrimo] %s\n' "$*"; }

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
# >>> devrimo codex cloud <<<
EOF
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
