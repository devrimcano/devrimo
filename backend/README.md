# Devrimo backend

FastAPI hosts the authenticated application API and workspace MCP gateway. An
independent Agno worker executes durable assistant runs. Supabase Auth supplies
identity; one Supabase PostgreSQL database stores application state, jobs,
pgvector indexes and Agno history.

The implementation and operating boundaries are described in
[application architecture](../docs/application-architecture.md) and
[database ownership](../docs/database-ownership.md). OpenAPI is generated from
backend contracts into `frontend/lib/api/schema.ts`.

## Layout

| Module | Responsibility |
| --- | --- |
| `app/api/v1` | Authenticated HTTP commands and queries |
| `app/workspace` | Seven typed operations, authenticated MCP and email approvals |
| `app/assistant` | Durable run queue, leases, ordered events and execution worker |
| `app/agents` | Agno builders, prompts, explicit memory and migration-owned history |
| `app/campus` | Internal MCP adapters, credentials, consent and bounded integration sessions |
| `app/planning` | Canonical timetable, revisioned commands and shared section solver |
| `app/student` | Verified academic context, preferences, update state and deletion |
| `app/knowledge` | Reviewed sources, ingestion, vector generations and hybrid retrieval |
| `app/researchers` | Public researcher directory and import jobs |
| `app/admin` | Organization commands, authorization and audit |
| `app/agentos` | Protected operations and measurement service |
| `app/db`, `alembic` | Models, runtime privilege checks and release-only migrations |

## Local development

Install backend dependencies in a virtual environment and copy `.env.example`
to `.env` with development Auth, encryption and model configuration. The fake
runtime replaces only the model; PostgreSQL, Agno and the queue remain real.

```sh
cd backend
docker compose up --build
```

Compose creates one local pgvector database, runs migrations once, provisions
restricted development logins, then starts the API and domain workers. It is a
development alternative to the hosted Supabase database. Do not use its fixed
local passwords in a remote deployment. Compose reads `.env` for interpolation
only: every application container receives an explicit environment allowlist,
and `DEVRIMO_ENV_FILE=` prevents the image's dotenv fallback from reintroducing
the shared file. The encryption-key placeholder is valid only in explicit
development/test environments; staging and production processes that decrypt
stored values require a generated key.

For host processes, run Alembic and `python -m app.db.bootstrap_local` once, then
start the API and assistant worker in separate terminals with their own role
credentials. The assistant requires `WORKSPACE_GATEWAY_URL=http://127.0.0.1:8000/mcp/`.
Set `AGENT_RUNTIME=fake` on the assistant for development without a model call.
Detailed environment and worker commands are in the ownership guide. The Next.js
frontend must point `NEXT_PUBLIC_API_URL` at the API address.

## Assistant and campus access

The model sees `search`, `read`, `plan`, `update`, `undo`, `send_email`, and
`compute`. Resource kinds specify the relevant domain. It receives no SQL tool,
credential tool or raw upstream MCP tool inventory. External MCP clients use
the same operations at `/mcp/` with the authenticated user's bearer token.

| Internal adapter | Data |
| --- | --- |
| SAIS | Student information, transcript, registered schedule and announcements |
| Course information | Catalog, sections, prerequisites and curriculum categories |
| ODTUClass | Enrolled courses, announcements, syllabi and deadlines |
| Webmail | Mailbox reads and explicitly approved sends/replies |

Adapters launch only when needed and reuse sessions scoped by user, source and
credential revision. Each call rechecks account, consent and credentials before
using a cached result or session. `CAMPUS_SESSION_MAX_SIZE` bounds the pool;
`CAMPUS_SESSION_IDLE_SECONDS` controls idle expiry. Credentials remain encrypted
at rest and absent from response/model schemas. Campus subprocesses inherit only
allowlisted environment variables and their own connection settings.

The assistant worker cannot read campus credentials or write domain tables.
Email sending requires the saved exact draft, an owner/session-validated paused
run, and an API-issued capability. SMTP uncertainty blocks automatic replay.
Arbitrary model-supplied attachment file paths are unsupported.

`AGENT_PROFILE=scholar` selects the main prompt/runtime; the legacy profile uses
the same seven-tool boundary. Admin runtime configuration controls the model and
history limits. Explicit memories use revisioned owner commands; academic facts
remain in verified owner resources.

## Knowledge

Ingestion publishes canonical records. The embedding worker creates derived
vectors in separately owned tables. Saving provider settings creates an immutable
candidate generation; admins preview its search results and activate it when
coverage is complete. Queries keep using the prior active model during a build.
Full-text, trigram and vector rankings are fused inside PostgreSQL. See the
[campus intelligence guide](../docs/campus-intelligence.md) for source adapters.

## Verification

```sh
cd backend
.venv/bin/python -m pytest tests -q
```

Tests require local PostgreSQL with pgvector. `TEST_DATABASE_URL` selects the
maintenance database; each run creates and drops a disposable database and
applies actual migrations. Permission tests use distinct login identities.
`AGENT_RUNTIME=fake` swaps only inference; scripted tests exercise Agno tool
calls and confirmations. Campus provider traffic and SMTP are mocked.

From `frontend`, generate contracts with
`PYTHON=../backend/.venv/bin/python npm run contracts:generate`, then run
`npm run lint`, `npx tsc --noEmit` and the production build. Clear build-time
PostHog credentials for local verification to avoid uploading development maps.

## Operations

Run migrations with a release-only identity. Supply every runtime process its
own restricted login to the same Supabase PostgreSQL database. Do not share the
migration credential or the Supabase Auth administration key across workers.
Deployment templates and checks live in `deployment/` and `scripts/deploy-vps.sh`.

Agno telemetry is disabled. Optional PostHog observations and redacted Agno traces
run in the assistant worker; trace schema changes belong to Alembic. Pinned campus
adapter revisions are recorded in `/opt/mcp/MANIFEST` and exposed by `/health`.
Network-level egress enforcement belongs to host configuration: campus mail uses
raw IMAP/SMTP and cannot be constrained by an HTTP proxy alone.

The current test suite does not establish live campus-provider availability,
real-model answer quality, or a measured concurrent-user capacity. Deployment
success must be verified separately from local test/build success.
