#!/usr/bin/env bash
# Codex Cloud environment -- maintenance script for the devrimo repo.
#
# Paste this into the Codex environment "Maintenance script" field:
#
#     bash .codex/cloud/maintenance.sh
#
# It runs on every cached-container resume, before the agent phase. Because the
# cache is a filesystem snapshot, PostgreSQL is not running when the container
# comes back: this is what starts it. Dependency syncs are hash-gated so a
# resume on the same lockfiles does no work.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ensure_postgres_role
ensure_bashrc
sync_backend_deps
sync_frontend_deps
ensure_local_database

log "ready: tests use their own throwaway database from $TEST_DATABASE_URL"
log "        the server uses the local copy DATABASE_URL=$DATABASE_URL"
log "backend tests:  backend/.venv/bin/python -m pytest tests -q   (run from backend/)"
log "frontend:       npm run contracts:check && npm run lint && npm run build   (run from frontend/)"
