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
test -f "$DEPLOY_DIR/backend/.env.migrations"
test -f "$DEPLOY_DIR/frontend/.env.local"

# Release-only credentials may target the staged database before runtime cutover.
# Translate asyncpg's TLS query option for libpq without modifying credentials.
PG_URL="$(
  set -a
  source "$DEPLOY_DIR/backend/.env.migrations"
  set +a
  python3 - <<'PYURL'
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

value = os.environ.get("DATABASE_MIGRATION_URL")
if not value:
    raise SystemExit("DATABASE_MIGRATION_URL is required in backend/.env.migrations")
url = urlsplit(value)
if url.scheme.split("+", 1)[0] != "postgresql":
    raise SystemExit("Release backup requires PostgreSQL")
query = dict(parse_qsl(url.query, keep_blank_values=True))
if "ssl" in query:
    mode = query.pop("ssl")
    if mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise SystemExit("Unsupported PostgreSQL TLS mode")
    if "sslmode" in query and query["sslmode"] != mode:
        raise SystemExit("Conflicting PostgreSQL TLS modes")
    query["sslmode"] = mode
print(urlunsplit(("postgresql", url.netloc, url.path, urlencode(query), url.fragment)))
PYURL
)"

stamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Keep a compact source rollback snapshot. Runtime dependencies, caches,
# credentials, campus state, and databases are deliberately excluded.
tar -czf "$BACKUP_DIR/source-$stamp.tar.gz" \
  --exclude='./frontend/node_modules' \
  --exclude='./frontend/.next' \
  --exclude='./frontend/.env.local' \
  --exclude='./backend/.venv' \
  --exclude='./backend/.env*' \
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
#
# A PostgreSQL client may connect to a newer server, but pg_dump refuses to
# create a dump when its major version is older than the server's. The VPS had
# pg_dump 16 while the production database had moved to 17, so resolve a
# matching client before starting the release. Prefer a versioned client that
# is already installed, then install the small client package when the host's
# PostgreSQL apt repository provides it.
server_version_num="$(psql "$PG_URL" -AtXqc 'SHOW server_version_num')"
case "$server_version_num" in
  (''|*[!0-9]*)
    echo "Could not determine the PostgreSQL server version" >&2
    exit 1
    ;;
esac
server_major="$((10#$server_version_num / 10000))"

pg_dump_bin=""
pg_dump_major() {
  "$1" --version | sed -nE 's/.*PostgreSQL[^0-9]*([0-9]+)\..*/\1/p'
}

for candidate in \
  "$(command -v "pg_dump$server_major" 2>/dev/null || true)" \
  "/usr/lib/postgresql/$server_major/bin/pg_dump" \
  "/usr/local/pgsql-$server_major/bin/pg_dump"; do
  if [ -x "$candidate" ] && [ "$(pg_dump_major "$candidate")" = "$server_major" ]; then
    pg_dump_bin="$candidate"
    break
  fi
done

if [ -z "$pg_dump_bin" ] && command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  echo "Installing PostgreSQL $server_major client for the production backup"
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "postgresql-client-$server_major"
  candidate="/usr/lib/postgresql/$server_major/bin/pg_dump"
  if [ -x "$candidate" ] && [ "$(pg_dump_major "$candidate")" = "$server_major" ]; then
    pg_dump_bin="$candidate"
  fi
fi

if [ -z "$pg_dump_bin" ]; then
  echo "No PostgreSQL $server_major pg_dump client is available on the VPS" >&2
  exit 1
fi

echo "Creating PostgreSQL $server_major backup with $pg_dump_bin"
"$pg_dump_bin" --format=custom --no-owner --file="$BACKUP_DIR/devrimo-$stamp.dump" "$PG_URL"
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
  --exclude='.env*' \
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

# The Course Info MCP server is a third-party package with local patches, and
# until now installing them was a manual step somebody had to remember. The
# Dockerfile patches src/ before building a wheel, which is right for an image
# build; this host installed the wheel and never rebuilds, so src/ is a
# directory Python never imports and the patch has to reach site-packages too.
# Both are written so the two stay identical and an image build produces the
# same thing.
#
# Copied only when the content differs, so an unchanged deploy neither rewrites
# the files nor restarts anything on their account. A verification that the
# server still starts and still lists its tools follows the copy: a broken
# patch here takes out the catalog for every student, and it would do it
# silently, because a subprocess that fails to start reads as "METU is down".
patch_src="$DEPLOY_DIR/backend/vendor_patches/course_info"
if [ -d "$patch_src" ]; then
  # Globbed rather than pinned to a version, so a python bump in that venv
  # does not silently stop patching.
  site="$(echo /opt/mcp/course-info/.venv/lib/python*/site-packages/metu_course_info_mcp)"
  src="/opt/mcp/course-info/src/metu_course_info_mcp"
  backup="/opt/mcp/course-info/.patch-backup"
  changed=false
  mkdir -p "$backup"
  for name in models.py sais_client.py server.py; do
    [ -f "$patch_src/$name" ] || continue
    [ -f "$site/$name" ] || { echo "::error::$site/$name is missing; refusing to patch"; exit 1; }
    if ! cmp -s "$patch_src/$name" "$site/$name"; then
      cp -n "$site/$name" "$backup/$name" 2>/dev/null || true
      cp "$patch_src/$name" "$site/$name"
      [ -d "$src" ] && cp "$patch_src/$name" "$src/$name"
      echo "course-info patch: updated $name"
      changed=true
    fi
  done
  if [ "$changed" = true ]; then
    if ! /opt/mcp/course-info/.venv/bin/python -c "import metu_course_info_mcp.sais_client as m; assert hasattr(m, 'SAISClient')"; then
      echo "::error::the patched course-info server no longer imports; rolling back"
      for name in models.py sais_client.py server.py; do
        [ -f "$backup/$name" ] && cp "$backup/$name" "$site/$name"
      done
      exit 1
    fi
    echo "course-info patch: verified"
  fi
fi

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
# may change ownership and remove retired columns; keep the recovery snapshot.
sudo /usr/bin/systemctl stop devrimo-api.service
if ! bash -lc "cd '$DEPLOY_DIR/backend' && set -a && source .env.migrations && set +a && .venv/bin/python -m alembic upgrade head"; then
  sudo /usr/bin/systemctl start devrimo-api.service
  exit 1
fi

# The knowledge worker imports the same application modules as the API, so it
# keeps serving the previous release from memory until it is restarted too.
# Every long-running unit that loads this source tree belongs in this list.
sudo /usr/bin/systemctl restart devrimo-api.service
sudo /usr/bin/systemctl restart devrimo-web.service
worker_units=(devrimo-assistant-worker.service devrimo-knowledge-worker.service devrimo-embedding-worker.service devrimo-researcher-worker.service devrimo-directory-worker.service devrimo-catalog-worker.service devrimo-retention-worker.service)
sudo /usr/bin/systemctl restart "${worker_units[@]}"

for attempt in {1..30}; do
  api_ok=false
  web_ok=false
  worker_ok=false
  curl -fsS http://127.0.0.1:8000/health >/dev/null && api_ok=true
  curl -fsS -o /dev/null http://127.0.0.1:3000/ && web_ok=true
  systemctl is-active --quiet "${worker_units[@]}" && worker_ok=true
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
sudo /usr/bin/systemctl status "${worker_units[@]}" --no-pager || true
journalctl -u devrimo-api.service -u devrimo-web.service -u devrimo-knowledge-worker.service -n 100 --no-pager || true
exit 1
