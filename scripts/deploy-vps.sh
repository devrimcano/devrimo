#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="/opt/devrimo"
BACKUP_DIR="$DEPLOY_DIR/.backups"
RUNTIME_DIR="/var/lib/devrimo"
RELEASE_ARCHIVE="${RELEASE_ARCHIVE:?RELEASE_ARCHIVE is required}"
DEPLOY_SHA="${DEPLOY_SHA:?DEPLOY_SHA is required}"
NODE_BIN="/opt/devrimo/node/bin"
LOCK_FILE="$DEPLOY_DIR/.deploy.lock"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another Devrimo deployment is already running"; exit 1; }

test -f "$RELEASE_ARCHIVE"
test -f "$DEPLOY_DIR/backend/.env"
test -f "$DEPLOY_DIR/frontend/.env.local"

# The application connects through an async driver; pg_dump reaches the same
# database through libpq, so only the driver suffix is dropped. tr strips any
# surrounding quotes (octal escapes keep this line quote-free).
DATABASE_URL="$(sed -n -e 's/^DATABASE_URL=//p' "$DEPLOY_DIR/backend/.env" | tail -1 | tr -d '\042\047')"
test -n "$DATABASE_URL" || { echo "DATABASE_URL is not set in backend/.env"; exit 1; }
PG_URL="${DATABASE_URL/+asyncpg/}"

stamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Keep a compact source rollback snapshot. Runtime dependencies, caches,
# credentials, campus state, and databases are deliberately excluded.
tar -czf "$BACKUP_DIR/source-$stamp.tar.gz" \
  --exclude='./frontend/node_modules' \
  --exclude='./frontend/.next' \
  --exclude='./frontend/.env.local' \
  --exclude='./backend/.venv' \
  --exclude='./backend/.env' \
  --exclude='./backend/.campus-state' \
  --exclude='./backend/.agentos' \
  --exclude='./node' \
  --exclude='./.npm' \
  --exclude='./.local' \
  --exclude='./.ssh' \
  --exclude='./.backups' \
  --exclude='./.deploy.lock' \
  --exclude='./.deployed-sha' \
  --exclude='./.release-sha' \
  -C "$DEPLOY_DIR" .
chmod 600 "$BACKUP_DIR/source-$stamp.tar.gz"

# pg_dump takes a transactionally consistent snapshot while the old API process
# keeps serving. The custom format restores with pg_restore and compresses.
pg_dump --format=custom --no-owner --file="$BACKUP_DIR/devrimo-$stamp.dump" "$PG_URL"
chmod 600 "$BACKUP_DIR/devrimo-$stamp.dump"

stage_dir="$(mktemp -d "$DEPLOY_DIR/.release.XXXXXX")"
trap 'rm -rf "$stage_dir"' EXIT
tar -xzf "$RELEASE_ARCHIVE" -C "$stage_dir"

# Deploy an exact source tree instead of extracting over the previous release.
# Runtime state and secrets are preserved; stale source files are removed.
rsync -a --delete \
  --exclude='.env.local' \
  --exclude='node_modules/' \
  --exclude='.next/' \
  "$stage_dir/frontend/" "$DEPLOY_DIR/frontend/"
rsync -a --delete \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='.campus-state/' \
  --exclude='.agentos/' \
  --exclude='devrimo.db*' \
  --exclude='secrets/' \
  "$stage_dir/backend/" "$DEPLOY_DIR/backend/"

bash -lc "
  set -e
  cd '$DEPLOY_DIR/backend'
  .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true
  .venv/bin/python -m pip install -r requirements.txt
"

# The running services learn which commit they are, so every event, log line
# and exception can be attributed to a deploy rather than to a date. Written
# before the restarts below, and outside both rsync targets so `--delete` in
# the step above cannot remove it.
printf '%s\n' "$DEPLOY_SHA" > "$DEPLOY_DIR/.release-sha"

bash -lc "
  set -e
  export PATH='$NODE_BIN':\$PATH
  # GIT_COMMIT_SHA names the release the browser source maps are uploaded
  # under; NEXT_PUBLIC_RELEASE is the same value baked into the bundle, so a
  # browser exception and its source map agree on which build they came from.
  export GIT_COMMIT_SHA='$DEPLOY_SHA'
  export NEXT_PUBLIC_RELEASE='$DEPLOY_SHA'
  cd '$DEPLOY_DIR/frontend'
  rm -rf .next
  npm ci
  npm run build
"

# Keep the migration/restart window short. Alembic migrations in this project
# are additive; the database snapshot above remains available for recovery.
sudo /usr/bin/systemctl stop devrimo-api.service
if ! bash -lc "cd '$DEPLOY_DIR/backend' && .venv/bin/python -m alembic upgrade head"; then
  sudo /usr/bin/systemctl start devrimo-api.service
  exit 1
fi

# The knowledge worker imports the same application modules as the API, so it
# keeps serving the previous release from memory until it is restarted too.
# Every long-running unit that loads this source tree belongs in this list.
sudo /usr/bin/systemctl restart devrimo-api.service
sudo /usr/bin/systemctl restart devrimo-web.service
sudo /usr/bin/systemctl restart devrimo-knowledge-worker.service

for attempt in {1..30}; do
  api_ok=false
  web_ok=false
  worker_ok=false
  curl -fsS http://127.0.0.1:8000/health >/dev/null && api_ok=true
  curl -fsS -o /dev/null http://127.0.0.1:3000/ && web_ok=true
  systemctl is-active --quiet devrimo-knowledge-worker.service && worker_ok=true
  if "$api_ok" && "$web_ok" && "$worker_ok"; then
    printf '%s\n' "$DEPLOY_SHA" > "$DEPLOY_DIR/.deployed-sha"
    rm -f "$RELEASE_ARCHIVE"
    echo "Devrimo deployment $DEPLOY_SHA is healthy"
    exit 0
  fi
  sleep 2
done

sudo /usr/bin/systemctl status devrimo-api.service --no-pager || true
sudo /usr/bin/systemctl status devrimo-web.service --no-pager || true
sudo /usr/bin/systemctl status devrimo-knowledge-worker.service --no-pager || true
journalctl -u devrimo-api.service -u devrimo-web.service -u devrimo-knowledge-worker.service -n 100 --no-pager || true
exit 1
