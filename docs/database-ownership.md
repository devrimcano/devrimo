# Database and worker ownership

All persistence uses one Supabase PostgreSQL database: application tables,
PostgreSQL job state, pgvector indexes, and Agno's `ai` schema. Configure every
identity against the same endpoint, port, and database. Use the direct or
session-pool endpoint consistently. Supabase Auth remains the identity provider.

Every application, Agno, and migration connection uses the explicit search path
`public,extensions`, supporting pgvector and pg_trgm installed in either schema.
Release migration 0029 grants runtime groups `USAGE` on an existing `extensions`
schema without granting `CREATE`; it does not relocate Supabase-managed extensions.
The path excludes login-named schemas. Run the PostgreSQL integration suite with
`TEST_EXTENSION_SCHEMA=extensions` to test the full migration chain against the
Supabase layout; the default suite covers extensions installed in `public`.

Run Alembic once in a release process with `DATABASE_MIGRATION_URL`. Runtime
containers no longer run migrations. The release identity needs schema ownership
and, for the initial bootstrap through revision 0030, permission to create the
`devrimo_*` NOLOGIN groups and limit their logins. Subsequent releases use the
scoped owner described below. Runtime logins must have
exactly one group, no membership in the migration owner, and no CREATE, superuser,
BYPASSRLS, CREATEROLE, or CREATEDB privileges. Provision login passwords through
your secret manager; migrations never contain production passwords.

An explicit `DATABASE_MIGRATION_URL` is the complete release connection input;
Alembic does not load runtime dotenv credentials in that mode. This allows a
staged migration before the runtime endpoint is switched over. For asyncpg use
`?ssl=require` (or a stronger verification mode); Agno and the release backup
translate that option to libpq's `sslmode` while preserving the selected mode.

For example, create `embedding_worker LOGIN` using an interactive password prompt
and grant it `devrimo_embedding`. Supply that login through `DATABASE_URL` and set
`DATABASE_RUNTIME_ROLE=embedding`. Never grant `devrimo_api` to a worker to resolve
a permission error. Worker startup checks actual inherited PostgreSQL privileges,
including column grants, and refuses foreign write access. No code switches roles.

| Process | Runtime role | Writable data |
| --- | --- | --- |
| FastAPI | api | Application command tables; no Agno DDL |
| Assistant/AgentOS | assistant | Run execution state, immutable event append, audits, memory, `ai.agno_*` except schema versions |
| Knowledge ingestion | knowledge | Canonical records, ingestion jobs, source fetch progress columns |
| Embedding | embedding | Index vectors and index jobs only |
| Researcher import | researcher | Researcher directory and import state |
| Directory synchronization | directory | Account directory and organization initialization |
| Catalog warming / retention | catalog | Schedule/catalog cache only |
| Planning owner | planning | Student timetables and timetable revision history |
| Student owner | student | Academic snapshots, context, preferences, update state, resource revisions |

The API currently hosts the domain command implementations together. Its
application DML identity supports these commands. Agno uses a separate
`ASSISTANT_DATABASE_URL` in that process. The API enqueues authenticated runs; inference executes only in the separate
assistant worker. Standalone assistant callers use the
workspace HTTP gateway; they cannot open planning or student sessions. Configure
`WORKSPACE_GATEWAY_URL` and bind the authenticated user token through the trusted
client context. A resource reference or model argument never establishes identity.

Application tables have RLS enabled and grants revoked from `PUBLIC`, `anon`,
`authenticated`, and `service_role`. Worker policies authorize their server-side
operations; user authorization remains in authenticated application commands.
Keep `ai` out of the Supabase Data API exposed-schema list. New migrations must
explicitly grant owner privileges and RLS policies; do not add broad default grants.

## Local development

`cd backend && docker compose up --build` uses one host-only local pgvector
PostgreSQL instance. A one-shot migration service initializes schema and creates
restricted development logins before API/workers start. The `devrimo_local`
password is for this local stack only. The migration identity never reaches
worker environments. Normal test runs use disposable databases and real
PostgreSQL role-denial tests; no SQLite substitute is used.

For host processes, migrate using the existing local development database, then
run `python -m app.db.bootstrap_local` once. Set the desired
`devrimo_<owner>_local` login and runtime role explicitly when launching a worker.
The API needs the `devrimo_api_local` URL and assistant URL. Environment overrides
should be passed by a process-specific environment file rather than copied into
a shared worker file. The Compose file follows the same boundary: `.env` is used
for interpolation, while each service has an explicit allowlist and an empty
`DEVRIMO_ENV_FILE`. Only the API gateway, assistant, embedding worker, and
catalog warmer receive `SECRET_ENCRYPTION_KEY`; retention shares the catalog
database role but performs no decryption. A generated key is required for those
crypto responsibilities in staging and production; the documented placeholder
remains available for explicit development/test runs.

## Deployment processes

| Command | Runtime role |
| --- | --- |
| `python -m app.assistant.worker` | assistant |
| `python -m app.knowledge.worker` | knowledge |
| `python -m app.knowledge.embedding_worker` | embedding |
| `python -m app.workers.runtime researcher` | researcher |
| `python -m app.workers.runtime directory` | directory |
| `python -m app.workers.runtime catalog` | catalog |
| `python -m app.workers.runtime retention` | catalog |

Directory sync alone needs the Supabase Auth admin secret. Embedding reads its
encrypted provider configuration; ingestion cannot change the active index or
embedding settings. Catalog and ingestion no longer share a process. API startup
no longer runs directory, researcher, or retention loops.

Install the seven `deployment/devrimo-<name>-worker.service` units. Create their
`devrimo-<name>` system users with read/execute access to the application and venv.
Set each account home to `/var/lib/devrimo-workers/<name>` when creating it:
`useradd --system --home-dir /var/lib/devrimo-workers/<name> --shell /usr/sbin/nologin devrimo-<name>`.
For existing accounts, use `usermod --home /var/lib/devrimo-workers/<name> devrimo-<name>`.
The units create that private directory with `StateDirectory` and mode `0700`.
Do not use `/home` for service account homes: `ProtectHome=true` hides it and
PostgreSQL TLS initialization may inspect the account home for certificates.
Set catalog's `CAMPUS_STATE_ROOT=/var/lib/devrimo/campus-catalog` and create that
directory owned by `devrimo-catalog`, mode `0700`. The catalog unit's writable
path matches this directory; it must not share the API's campus state directory.
The template unit
is also available for the generic worker commands.
Use separate root-owned `/etc/devrimo/<name>.env` files, mode 0600. Systemd reads
them before dropping to the service account; workers need not read the files.
Keep `.env.migrations` release-only, mode 0600, outside all runtime environment
files. Set `DEVRIMO_ENV_FILE=` to disable the shared dotenv fallback; the provided
units do this and consume only their systemd environment. The deployment script requires it and checks every worker unit after restart.
The VPS deployment uses `/etc/devrimo/api.env` and the seven isolated worker
environment files. Supabase is the active database through its IPv4 session
pooler; the previous local database is retained only as a rollback backup.
Worker account home directories match their private systemd state directories.

The unit files are release-managed configuration. Before restarting services
after a release that changes a unit, install the updated files and reload
systemd. Preserve deployment-specific path overrides when installing the seven
worker units and generic template with `sudo install -m 0644 deployment/devrimo-*-worker.service deployment/devrimo-worker@.service /etc/systemd/system/`, then run `sudo systemctl daemon-reload`. The deployment script
restarts the existing unit files but does not install or reload changed unit
files; this step is required for `DEVRIMO_RUNTIME_COMPONENT=retention` and the
other explicit component identities to take effect. Retention must keep its
catalog database role while receiving no encryption key.

Agno 3.0.1 schema is frozen in Alembic. Missing tables fail explicitly; runtime
never creates tables or upgrades schemas. For an Agno package upgrade, include
reviewed schema/data migrations in the same release and validate existing history.

## Durable assistant runs

The API writes immutable authenticated run requests to `assistant_runs`. Its
worker may update only execution/lease columns and append `assistant_run_events`;
it cannot change request payload, user, session, idempotency identity, or create
mail approvals. Authentication and one-use email capability tokens are encrypted
at rest and cleared on terminal runs. The assistant login cannot read campus
credentials. The gateway uses its integration owner to access upstream MCPs.

`POST /api/v1/chat/completions` returns an `X-Run-ID` and persisted SSE events.
Reconnect with `GET /api/v1/chat/runs/{id}/events?after=N`; event `id` values are
monotonic per run. Disconnecting does not cancel execution. Explicit cancellation
is owner-scoped and checked by the worker heartbeat. A lost running lease becomes
interrupted and is never automatically replayed, because an email may already
have been sent. Confirmation jobs resume the exact persisted Agno run; the
separate API-owned one-use approval record prevents a second external send.

Use `WORKSPACE_GATEWAY_URL` in the assistant environment; for local Compose it is
`http://broker:8000/mcp/`. Configure `WORKSPACE_GATEWAY_ALLOWED_HOSTS` explicitly on
the API for internal hostnames. `ASSISTANT_WORKER_CONCURRENCY` defaults to 4 (1–128)
and each replica claims distinct queued runs. Only the worker initializes model
tracing. No production inference occurs in the API process.

## Connection budget and drift checks

The API uses eight connections and one overflow connection, leaving space for
four concurrent campus calls that hold request/integration leases while making
short cache reads. Each worker uses two connections and one overflow connection;
Agno uses one connection with no overflow. For the current eight backend
processes and two Agno engines this is at most 32 application connections,
leaving 28 of the inspected 60 server slots for Supabase services, maintenance,
and headroom. These are per-process limits: recalculate before adding replicas
or increasing concurrent campus access. A regression test exercises that nested pattern.
`DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT` (15 seconds),
`DATABASE_POOL_RECYCLE` (300 seconds), and `AGNO_DATABASE_POOL_SIZE` configure them.
Changes must stay within the server budget and corresponding login limits.

Revision 0030 sets existing restricted login limits: API 9, assistant 5,
catalog/retention 6, other owners 3. Group role limits alone do not cap member
logins; provisioning must set limits on the actual login. Local bootstrap does
this too. Production and staging reject missing/opportunistic TLS modes for
all database identities; use `ssl=require` or CA-backed `verify-full`.

`alembic check` is now a release gate. Only the explicitly named migration-owned
generated search columns and search indexes are excluded from ORM comparison.
Regression tests require a clean database to pass and an unexpected column to
fail. This does not replace reviewing specialized indexes and RLS in migrations.

## Scoped release credentials

After upgrading through the current migrations with the bootstrap identity,
run `python -m app.db.release_roles` with `DATABASE_BOOTSTRAP_URL` supplied through
a protected administrator environment. This one-time command transfers only
application tables/sequences in `public` and `ai` to `devrimo_release`; it leaves
Supabase-managed schemas and extension objects with their original owners.
It also creates `devrimo_backup` with SELECT privileges and SELECT-only RLS
policies. Neither role has BYPASSRLS, CREATEDB, CREATEROLE, or superuser rights.
They are initially NOLOGIN and provisioning refuses to overwrite existing roles.

Set passwords with psql's interactive `\password devrimo_release` and
`\password devrimo_backup` commands. Then enable LOGIN with CONNECTION LIMIT 2
on the release role and CONNECTION LIMIT 1 on the backup role. Set
`DATABASE_MIGRATION_URL` in `.env.migrations` and `DATABASE_BACKUP_URL` in
`.env.backups`, both mode 0600 and excluded from runtime service environments.
Use the same Supabase session-pool endpoint and project-qualified usernames.
Verify the scoped migration login with `alembic current`, `alembic upgrade head`,
and `alembic check` before retiring the old release credential. New applied
migrations maintain the backup role's SELECT policies; a read-only drift check
does not alter policies. A future migration requiring role administration or
extension installation needs an explicit administrator bootstrap step.

## Supabase-managed recovery

Supabase-managed backups/PITR are the selected off-host recovery system. The
deployment dump is an additional pre-release application recovery artifact,
created with the read-only backup identity. It contains both `public` and `ai`
and the extension schema, has mode 0600, and excludes unrelated managed schemas.
It does not replace Supabase's full-project recovery, Auth, or Storage backups.

Before marking managed recovery operational, record the project's actual backup
schedule, retention, successful latest backup, and PITR restore window from its
Dashboard. Proposed targets are RPO <= 24 hours for daily backups (or <= 5 minutes
with PITR enabled), RTO <= 60 minutes, and at least 7 days of retention. These are
acceptance targets, not claims about the current plan or a purchase authorization.
Dashboard access is required to verify or enable the selected settings.

Verified in the signed-in Dashboard on 2026-09-08: this organization is on Free,
the project has no managed backups, Pro starts at $25/month and includes seven
days of daily backups, and PITR is an additional Pro add-on starting at $100/month.
The user selected Supabase-managed recovery and explicitly deferred enabling it
until later. No paid upgrade has been applied; managed recovery remains an
outstanding operational requirement rather than a completed fix.

Run a restore drill against an isolated PostgreSQL server with the matching
PostgreSQL major version and pgvector/pg_trgm available. The maintenance identity
needs CREATEDB and, on an empty cluster, CREATEROLE for temporary NOLOGIN groups:

```sh
# RESTORE_ADMIN_URL is injected securely and must name the postgres maintenance DB.
# PG_BIN_DIR can select the matching PostgreSQL client installation.
python -m app.db.restore_check /protected/path/application.dump
```

The command creates a random `devrimo_restore_*` database, stops on any
pg_restore error, checks the migration revision and both persistence schemas,
reports elapsed restore time, and drops only that temporary database. Tests
exercise an actual dump/restore using synthetic data. For a managed PITR drill,
restore to an isolated project through Supabase, verify Auth and application
journeys with external side effects disabled, record achieved RPO/RTO, and check
credentials after recovery. Repeat after material schema changes and regularly
under the operational recovery schedule; do not restore over production to test.
