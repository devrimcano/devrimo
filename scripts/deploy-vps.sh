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

# Named, not asserted. A bare `test -f` under `set -e` exits with no output at
# all, which is exactly how three deploys in a row died two seconds in while
# the workflow log showed nothing but the exit code.
for required in \
  "$RELEASE_ARCHIVE" \
  "$DEPLOY_DIR/backend/.env" \
  "$DEPLOY_DIR/backend/.env.migrations" \
  "$DEPLOY_DIR/frontend/.env.local"
do
  test -f "$required" || { echo "Required file is missing: $required"; exit 1; }
done

# The scoped read-only backup identity that docs/database-ownership.md
# provisions is preferred and is used whenever it is present. It is not a
# precondition for deploying: a host that has not been through that
# provisioning still has to be able to take its pre-release dump, and refusing
# to deploy at all leaves it running older code indefinitely — which is worse
# than a dump taken with the migration login. Announced rather than silent, so
# "this host still owes itself a backup role" is visible in the deploy log.
BACKUP_ENV="$DEPLOY_DIR/backend/.env.backups"
if [ ! -f "$BACKUP_ENV" ]; then
  echo "backend/.env.backups is absent; taking the pre-release dump with the migration identity"
fi

# Release-only credentials may target the staged database before runtime cutover.
# Translate asyncpg's TLS query option for libpq without modifying credentials.
pg_environment="$(
  set -a
  if [ -f "$BACKUP_ENV" ]; then
    source "$BACKUP_ENV"
  fi
  source "$DEPLOY_DIR/backend/.env.migrations"
  set +a
  python3 - <<'PYURL'
import os
import shlex
from urllib.parse import parse_qsl, unquote, urlsplit

value = os.environ.get("DATABASE_BACKUP_URL")
# Falling back to the migration login is a deliberate, announced downgrade for a
# host without the scoped backup role. Every other check below still applies to
# whichever identity is used; only "these must be two different logins" cannot.
fell_back = not value
if fell_back:
    value = os.environ.get("DATABASE_MIGRATION_URL")
if not value:
    raise SystemExit("DATABASE_BACKUP_URL is required in backend/.env.backups")
url = urlsplit(value)
if url.scheme.split("+", 1)[0] != "postgresql":
    raise SystemExit("Release backup requires PostgreSQL")
if not all((url.hostname, url.username, url.password, url.path.lstrip("/"))):
    raise SystemExit("Backup URL requires an explicit host, database, username, and password")
migration = urlsplit(os.environ.get("DATABASE_MIGRATION_URL", ""))
if (url.hostname, url.port or 5432, unquote(url.path)) != (
    migration.hostname, migration.port or 5432, unquote(migration.path)
):
    raise SystemExit("Backup and migration identities must target the same database endpoint")
if not fell_back and unquote(url.username) == unquote(migration.username or ""):
    raise SystemExit("Backup and migration identities must use separate logins")
query = dict(parse_qsl(url.query, keep_blank_values=True))
if "ssl" in query:
    mode = query.pop("ssl")
    if mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise SystemExit("Unsupported PostgreSQL TLS mode")
    if "sslmode" in query and query["sslmode"] != mode:
        raise SystemExit("Conflicting PostgreSQL TLS modes")
    query["sslmode"] = mode
if query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
    raise SystemExit("Release backup requires PostgreSQL TLS")
values = {"PGHOST": url.hostname, "PGPORT": str(url.port or 5432), "PGDATABASE": unquote(url.path.lstrip("/")),
          "PGUSER": unquote(url.username or ""), "PGPASSWORD": unquote(url.password or "")}
allowed = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
           "sslkey": "PGSSLKEY", "connect_timeout": "PGCONNECT_TIMEOUT", "options": "PGOPTIONS"}
for key, value in query.items():
    if key not in allowed:
        raise SystemExit("Unsupported release backup connection option")
    values[allowed[key]] = value
for key, value in values.items():
    print("export " + key + "=" + shlex.quote(value))
PYURL
)"
eval "$pg_environment"
unset pg_environment

stamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
frontend_releases="$DEPLOY_DIR/.frontend-releases"
frontend_release="$frontend_releases/$DEPLOY_SHA-$stamp"
mkdir -p "$frontend_releases"

# A rollback archive is useful only while there is room to create the next
# one. Older versions accumulated forever and eventually filled the host while
# tar was writing a new snapshot, before any serving file had changed. Remove
# only files created by this script, keep the two newest of each backup type,
# and discard interrupted zero-byte outputs before taking the next snapshot.
prune_deploy_files() {
  local directory="$1" pattern="$2" keep="$3"
  local files=()
  mapfile -t files < <(find "$directory" -maxdepth 1 -type f -name "$pattern" -printf '%f\n' | sort)
  local remove_count
  remove_count=$((${#files[@]} - keep))
  if (( remove_count > 0 )); then
    for name in "${files[@]:0:remove_count}"; do
      rm -f -- "$directory/$name"
    done
  fi
}

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'source-*.tar.gz' -o -name 'devrimo-*.dump' \) -size 0 -delete
# One complete source snapshot and one matching-generation database dump are
# sufficient for rollback; the live tree and frontend release retain their own
# previous versions separately.
prune_deploy_files "$BACKUP_DIR" 'source-*.tar.gz' 1
prune_deploy_files "$BACKUP_DIR" 'devrimo-*.dump' 1

# Every frontend release contains its own node_modules and .next tree. Keeping
# all historical builds duplicated gigabytes indefinitely. Retain the active
# target and the newest complete inactive build for immediate rollback; remove
# only directories whose names were generated by this deploy script.
prune_frontend_releases() {
  local active rollback="" directory name resolved
  active="$(readlink -f "$DEPLOY_DIR/frontend" 2>/dev/null || true)"
  local releases=()
  mapfile -t releases < <(find "$frontend_releases" -mindepth 1 -maxdepth 1 -type d -printf '%p\n' | sort -r)
  for directory in "${releases[@]}"; do
    resolved="$(readlink -f "$directory")"
    name="$(basename "$directory")"
    [[ "$name" =~ ^([0-9a-f]{40}|previous)-[0-9]{8}-[0-9]{6}$ ]] || continue
    [ "$resolved" = "$active" ] && continue
    if [ -z "$rollback" ] && [ -s "$directory/.next/BUILD_ID" ] \
      && { [ -f "$directory/server.js" ] || [ -d "$directory/node_modules/next" ]; }; then
      rollback="$resolved"
      continue
    fi
    case "$resolved" in
      ("$frontend_releases"/*) rm -rf -- "$directory" ;;
      (*) echo "Refusing to prune frontend path outside $frontend_releases: $resolved" >&2; exit 1 ;;
    esac
  done
}

# df is instant and the backup directory holds a handful of files, but du on
# the frontend releases walks several gigabytes of node_modules: measured at
# twenty-seven seconds per deploy, spent three times, to print a number that
# free space already implies. What pruning is actually judged on is how many
# releases are left, and counting directories is free.
report_storage() {
  df -h "$DEPLOY_DIR"
  du -sh "$BACKUP_DIR" 2>/dev/null || true
  echo "frontend releases retained: $(find "$frontend_releases" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
}

echo "Deployment storage before frontend pruning:"
report_storage
prune_frontend_releases
echo "Deployment storage after frontend pruning:"
report_storage

# Failed workflows upload a uniquely named archive and cannot reach the normal
# success cleanup. The current archive is retained; every other file matching
# the workflow's exact release prefix is stale and safe to remove.
current_archives=" $(basename "$RELEASE_ARCHIVE") "
# Written as an `if` rather than `test && assign`: under `set -e` a bare test
# that fails is the script's exit status, so the ordinary case of "no frontend
# archive" would end the deploy here, two lines into it, saying nothing.
if [ -n "${FRONTEND_ARCHIVE:-}" ]; then
  current_archives="$current_archives$(basename "$FRONTEND_ARCHIVE") "
fi
while IFS= read -r -d '' archive; do
  case "$current_archives" in
    (*" $(basename "$archive") "*) ;;
    (*) rm -f -- "$archive" ;;
  esac
done < <(find /tmp -maxdepth 1 -type f \
  '(' -name 'devrimo-release-*.tar.gz' -o -name 'devrimo-frontend-*.tar.gz' ')' -print0)

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
  --exclude='./.frontend-releases' \
  --exclude='./.release.*' \
  --exclude='./.deploy.lock' \
  --exclude='./.deployed-sha' \
  --exclude='./.release-sha' \
  -C "$DEPLOY_DIR" .
chmod 600 "$BACKUP_DIR/source-$stamp.tar.gz"

# pg_dump takes a transactionally consistent snapshot while the old API process
# keeps serving. The custom format restores with pg_restore and compresses.
# Ubuntu's default client can be older than Supabase. Prefer the installed
# client matching the server without changing the host's default PostgreSQL tools.
server_version_num="$(psql -AtXqc 'SHOW server_version_num')"
case "$server_version_num" in
  (''|*[!0-9]*)
    echo "Could not determine the PostgreSQL server version" >&2
    exit 1
    ;;
esac
server_major="$((10#$server_version_num / 10000))"
pg_dump_bin="/usr/lib/postgresql/$server_major/bin/pg_dump"
if [ ! -x "$pg_dump_bin" ]; then
  pg_dump_bin="$(command -v pg_dump)"
fi
# The dump goes through the same pooler everything else does, and the pooler
# drops a long COPY often enough to be a deploy's most common cause of death:
# "PQgetCopyData() failed ... SSL error: unexpected eof while reading", mid
# table, on a change that had nothing to do with the database. Three attempts
# with a pause, because the next one usually works; if none does, the deploy
# still stops, since a release without a rollback point is exactly what this
# dump exists to prevent.
dump_attempt=1
until "$pg_dump_bin" --format=custom --no-owner --no-acl \
  --schema=public --schema=ai --schema=extensions --extension=vector --extension=pg_trgm \
  --enable-row-security --file="$BACKUP_DIR/devrimo-$stamp.dump"; do
  if [ "$dump_attempt" -ge 3 ]; then
    echo "::error::The pre-release database dump failed three times; not deploying without a rollback point" >&2
    exit 1
  fi
  echo "Pre-release dump attempt $dump_attempt failed; retrying" >&2
  rm -f "$BACKUP_DIR/devrimo-$stamp.dump"
  dump_attempt=$((dump_attempt + 1))
  sleep $((dump_attempt * 10))
done
chmod 600 "$BACKUP_DIR/devrimo-$stamp.dump"
unset PGPASSWORD PGUSER PGHOST PGPORT PGDATABASE PGSSLMODE PGSSLROOTCERT PGSSLCERT PGSSLKEY PGOPTIONS PGCONNECT_TIMEOUT

stage_dir="$(mktemp -d "$DEPLOY_DIR/.release.XXXXXX")"
trap 'rm -rf "$stage_dir"' EXIT
tar -xzf "$RELEASE_ARCHIVE" -C "$stage_dir"
frontend_tool="$stage_dir/scripts/frontend_release.py"
# Build and exercise HTML/assets before touching any serving files or backend.
# Database credentials have been removed from the build environment above.
# The frontend is built by whoever can build it fastest. This host has two
# cores and 1.9G of RAM shared with the API, the workers and a Postgres client,
# and `npm ci` plus `next build` on it is the largest part of a deploy - so the
# workflow builds a standalone bundle on a runner and sends it. When it did not
# (a hand-run deploy, or a workflow older than this) the host still builds,
# because a deploy path with only one way to work has no way to work.
prebuilt_args=()
if [ -n "${FRONTEND_ARCHIVE:-}" ] && [ -f "$FRONTEND_ARCHIVE" ]; then
  prebuilt_frontend="$stage_dir/.frontend-prebuilt"
  mkdir -p "$prebuilt_frontend"
  tar -xzf "$FRONTEND_ARCHIVE" -C "$prebuilt_frontend"
  # The bundle names the commit it was built from. A bundle built from a
  # different commit than the source being deployed would serve one version's
  # pages against another version's API, so it is refused rather than
  # reconciled: the host can still build the right one itself.
  built_sha="$(cat "$prebuilt_frontend/.built-sha" 2>/dev/null || true)"
  if [ "$built_sha" != "$DEPLOY_SHA" ]; then
    echo "::error::Prebuilt frontend is from ${built_sha:-an unnamed commit}, not $DEPLOY_SHA" >&2
    exit 1
  fi
  rm -f "$prebuilt_frontend/.built-sha"
  echo "Installing the frontend built for $DEPLOY_SHA ($(du -sh "$prebuilt_frontend" | cut -f1))"
  prebuilt_args=(--prebuilt "$prebuilt_frontend")
fi
python3 "$frontend_tool" prepare --source "$stage_dir/frontend" \
  --release "$frontend_release" --env-file "$DEPLOY_DIR/frontend/.env.local" \
  --node-bin "$NODE_BIN" --sha "$DEPLOY_SHA" \
  "${prebuilt_args[@]}"
mkdir -p "$DEPLOY_DIR/scripts"
install -m 0644 "$stage_dir/scripts/frontend_release.py" "$DEPLOY_DIR/scripts/frontend_release.py"
install -m 0644 "$stage_dir/scripts/web_error_observer.py" "$DEPLOY_DIR/scripts/web_error_observer.py"
install -m 0755 "$stage_dir/scripts/start-frontend.sh" "$DEPLOY_DIR/scripts/start-frontend.sh"

# The web unit is version-controlled, and installed from the release rather
# than edited by hand on the host. It has to be in place before the switch
# below: a standalone release has no next binary for the old unit to run, so a
# host still holding the old unit would take the new release and then fail to
# start it - and would fail the rollback the same way, because rollback runs
# the same unit. Installing it here means that failure, if it comes, comes
# before anything serving has been touched.
#
# The unit is deliberately indirect (see scripts/start-frontend.sh): it starts
# whichever kind of release is linked, so it is correct both for the standalone
# release going in and for the pre-standalone release retained behind it.
web_unit_src="$stage_dir/deployment/devrimo-web.service"
web_unit_dst="/etc/systemd/system/devrimo-web.service"
if [ -f "$web_unit_src" ] && ! cmp -s "$web_unit_src" "$web_unit_dst"; then
  if [ ! -w "$(dirname "$web_unit_dst")" ]; then
    echo "::error::devrimo-web.service needs updating and $(dirname "$web_unit_dst") is not writable by $(id -un)" >&2
    exit 1
  fi
  cp -n "$web_unit_dst" "$web_unit_dst.pre-$stamp" 2>/dev/null || true
  install -m 0644 "$web_unit_src" "$web_unit_dst"
  systemctl daemon-reload
  echo "Installed devrimo-web.service from the release"
fi

# Deploy an exact source tree instead of extracting over the previous release.
# Runtime state and secrets are preserved; stale source files are removed.
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

# Catalog imports are an active production capability. Both the admin API that
# accepts import requests and the catalog worker that executes them must see
# the same value. Runtime environment files survive source deployments, so set
# the flag explicitly before either service is restarted.
sudo python3 "$stage_dir/scripts/set_runtime_env.py" \
  /etc/devrimo/api.env ACADEMIC_CATALOG_INGESTION_ENABLED true
sudo python3 "$stage_dir/scripts/set_runtime_env.py" \
  /etc/devrimo/catalog.env ACADEMIC_CATALOG_INGESTION_ENABLED true

# Keep the migration/restart window short. Alembic migrations in this project
# may change ownership and remove retired columns; keep the recovery snapshot.
sudo /usr/bin/systemctl stop devrimo-api.service
if ! bash -lc "cd '$DEPLOY_DIR/backend' && set -a && source .env.migrations && set +a && export ENVIRONMENT=production && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check"; then
  sudo /usr/bin/systemctl start devrimo-api.service
  exit 1
fi

# The knowledge worker imports the same application modules as the API, so it
# keeps serving the previous release from memory until it is restarted too.
# Every long-running unit that loads this source tree belongs in this list.
printf '%s\n' "$DEPLOY_SHA" > "$DEPLOY_DIR/.release-sha"
sudo /usr/bin/systemctl restart devrimo-api.service
worker_units=(devrimo-assistant-worker.service devrimo-knowledge-worker.service devrimo-embedding-worker.service devrimo-researcher-worker.service devrimo-directory-worker.service devrimo-catalog-worker.service devrimo-retention-worker.service)
sudo /usr/bin/systemctl restart "${worker_units[@]}"

# Existing installations start with a real directory. Retain it once; future
# switches replace a symlink atomically. Never remove an active build or its deps.
previous_frontend="$(readlink -f "$DEPLOY_DIR/frontend")"
rollback_frontend() {
  trap - ERR
  echo "Frontend activation failed; restoring $previous_frontend" >&2
  sudo /usr/bin/systemctl stop devrimo-web.service || true
  if [ "$previous_frontend" != "$DEPLOY_DIR/frontend" ]; then
    python3 "$frontend_tool" switch --current "$DEPLOY_DIR/frontend" --release "$previous_frontend"
  fi
  sudo /usr/bin/systemctl start devrimo-web.service
}
trap 'rollback_frontend' ERR
sudo /usr/bin/systemctl stop devrimo-web.service
if [ ! -L "$DEPLOY_DIR/frontend" ]; then
  mv "$DEPLOY_DIR/frontend" "$frontend_releases/previous-$stamp"
  previous_frontend="$frontend_releases/previous-$stamp"
fi
python3 "$frontend_tool" switch --current "$DEPLOY_DIR/frontend" --release "$frontend_release"
sudo /usr/bin/systemctl start devrimo-web.service

# Ninety seconds, not sixty: the API now refuses to start rather than hang on a
# slow database, so this window has to cover one crash and one systemd restart.
unhealthy=""
for attempt in {1..45}; do
  api_ok=false
  web_ok=false
  worker_ok=false
  curl -fsS http://127.0.0.1:8000/health >/dev/null && api_ok=true
  python3 "$frontend_tool" check-http && web_ok=true
  systemctl is-active --quiet "${worker_units[@]}" && worker_ok=true
  # Which check is failing, so the log says "the API never came up" instead of
  # rolling the frontend back and leaving the reason to be guessed at.
  unhealthy=""
  "$api_ok" || unhealthy="$unhealthy api"
  "$web_ok" || unhealthy="$unhealthy web"
  "$worker_ok" || unhealthy="$unhealthy workers"
  if "$api_ok" && "$web_ok" && "$worker_ok"; then
    trap - ERR
    printf '%s\n' "$DEPLOY_SHA" > "$DEPLOY_DIR/.deployed-sha"
    # The new snapshots/releases now exist, so enforce the same bounds after a
    # successful cutover. This leaves exactly the current frontend, one tested
    # rollback frontend, one source snapshot and one database dump.
    prune_deploy_files "$BACKUP_DIR" 'source-*.tar.gz' 1
    prune_deploy_files "$BACKUP_DIR" 'devrimo-*.dump' 1
    prune_frontend_releases
    rm -f "$RELEASE_ARCHIVE" ${FRONTEND_ARCHIVE:+"$FRONTEND_ARCHIVE"}
    report_storage
    echo "Devrimo deployment $DEPLOY_SHA is healthy"
    exit 0
  fi
  sleep 2
done

echo "::error::deployment never became healthy; still failing:$unhealthy"
sudo /usr/bin/systemctl status devrimo-api.service --no-pager || true
sudo /usr/bin/systemctl status devrimo-web.service --no-pager || true
sudo /usr/bin/systemctl status "${worker_units[@]}" --no-pager || true
journalctl -u devrimo-api.service -u devrimo-web.service -u devrimo-knowledge-worker.service -n 100 --no-pager || true
rollback_frontend
trap - ERR
exit 1
