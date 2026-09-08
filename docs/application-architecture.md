# Application architecture

Devrimo is a domain modular monolith with independently operated workers. FastAPI
hosts authenticated application commands and the workspace MCP gateway. Next.js
renders the product and proxies authenticated requests. Agno executes assistant
runs in its own worker. Supabase Auth supplies identity, and one Supabase
PostgreSQL database stores application records, durable jobs, vectors, and Agno
history. The local Compose PostgreSQL service is a development environment.

## Boundaries

| Module | Responsibility |
| --- | --- |
| `workspace` | Seven typed operations, authenticated resource routing, exact email approvals |
| `student` | Verified academic context, preferences, update state and deletion |
| `planning` | Minute-precise timetable state, revisions, deterministic section solver |
| `campus` | SAIS, ODTUClass, catalog and mail adapters; consent and credential-scoped sessions |
| `knowledge` | Published source content, ingestion, independent derived vector generations and retrieval |
| `assistant` | Durable commands, leases, event sequences, cancellation and worker execution |
| `agents` | Agno construction, prompts, runtime configuration, history and explicit memories |
| `admin`, `researchers` | Organization administration and directory import commands |

Workers use distinct PostgreSQL login roles. Permission checks reject broad or
foreign write grants at startup; Alembic alone owns DDL. For the precise grants,
process commands and configuration, see [database ownership](database-ownership.md).
The API hosts multiple owner services, so its authenticated command host has
application write access. Assistant tools reach those services through MCP;
assistant workers cannot edit student, timetable, catalog, or knowledge tables.

## Assistant operations

| Operation | Input and behavior |
| --- | --- |
| `search` | A typed resource, query and bounded filters; returns records with provenance |
| `read` | A typed resource reference; owner-scoped data or a live upstream read |
| `plan` | A semester planning request; returns an unsaved `planning.proposal` |
| `update` | Editable resource, changes, expected revision and idempotency key |
| `undo` | Timetable or explicit memory resource, revision and idempotency key |
| `send_email` | Exact draft; pauses for the student's confirmation |
| `compute` | Bounded arithmetic expression |

The same seven tools are exposed by the Agno builders and `/mcp/`. Domain details
are resource types, not another model-visible tool for every upstream endpoint.
The closed resource vocabulary is in `app/workspace/resources.py`; it includes
SAIS transcript and registered schedule, ODTUClass courses and assignments,
mailbox reads, catalog queries, campus knowledge, preferences and the editable
planning timetable. Registered courses remain distinct from a proposed timetable.

The HTTP bearer token establishes identity. User IDs and credentials are never
accepted as model tool arguments. The gateway checks the active account before
every operation, and integrations recheck consent and credential revision even
for cached answers. Upstream MCP processes are internal adapters, reused in a
bounded per-user/per-source pool with serialized calls and an idle timeout.
Their lifecycle is independent of Agno agent objects.

## Durable turns and external effects

`POST /api/v1/chat/completions` snapshots authorized context and enqueues a run.
An idempotency key identifies a submitted message. A database constraint allows
one queued/running turn per user. The worker claims a lease, calls Agno, and
persists ordered SSE frames before clients receive them. The response includes
`X-Run-ID`; `/chat/runs/{id}/events?after=N` replays subsequent frames. Disconnects
do not cancel execution. `/chat/runs/{id}/cancel` is an explicit owner command.
The Next.js stream reader resumes stored frames after a transport interruption.

For streaming requests in the web proxy, `api_request_completed` reports the `response_headers`
phase only. The web proxy emits `chat_stream_completed` after consuming the
stream, with `completed`, `awaiting_confirmation`, `failed`, or `interrupted`
status and the same request/run correlation. Use that terminal event for chat
stream reliability; an HTTP 200 alone does not mean the run succeeded.
Browser source maps are generated only when both PostHog upload credentials
are supplied at build time; the uploader removes them after upload.

The token needed for gateway calls is encrypted in the queued run and erased on
completion. Workers reject expired tokens. Lease expiry marks a started run
interrupted instead of replaying it: an external effect may already have happened.

Email approval is issued from the saved paused requirement, after verifying its
user and session. The exact draft digest and a hashed capability are stored in
an API-owned approval ledger in the same transaction as the continuation job.
The capability travels in trusted transport context, never in model arguments.
The gateway records execution before SMTP. Repeated successful requests return
the saved delivery result; an uncertain outcome cannot be sent again with the
same approval. Replies preserve threading while sending exactly the displayed
subject and body. Arbitrary local-file attachment paths are not accepted.

## Planning and mutations

The canonical timetable key is `(user_id, term)`. It stores exact start/duration
minutes, selected entries, course pool, options, alternatives and favorites.
Every accepted command appends a revision and checks its expected predecessor.
Semester proposals contain an `application` command only when every selected
meeting has a valid canonical time. Apply that command with the existing `update`
operation and a new idempotency key: it replaces timetable entries at the returned
expected revision. Conflicting edits are rejected and a successful application
can be undone. The proposal resource itself is neither a saved timetable nor writable.
A repeated idempotency key returns the original result; different changes with
that key conflict. Undo appends a new revision instead of deleting history.

The UI saves through the same command service used by the agent. It displays
loading, saving and conflict states, retains failed commands for an idempotent retry,
and scopes recovery copies to the account and term. Old browser-only plans require explicit import. The browser
no longer implements section-combination search; both planner entry points use
`planning/solver.py`, with exact minute overlap rules and bounded search.
Preferences, update-state commands and explicit assistant memories also have
revision and idempotency ledgers. Sensitive academic facts are read from their
owner resources rather than copied into learned memory.

## Embeddings

Ingestion only publishes canonical source records. The embedding worker reads
published content and writes its own vector/job tables. A generation freezes
provider, model, dimensions, prefixes and encrypted provider credentials. Exact
embedding-input fingerprints allow reuse without invoking a provider again.
Only public campus knowledge enters this index; student transcripts and mail do
not enter a shared vector corpus.

Saving embedding settings creates a candidate generation. Existing retrieval
continues using the active generation's query model. Admin search can preview a
candidate; activation checks complete coverage of current published content and
atomically changes the active pointer. Publication and activation share a
transaction lock so content changes cannot race the coverage check. Old vectors
with a mismatched content hash are excluded. Earlier complete generations can
be reactivated. With no active vector generation, indexed PostgreSQL full-text
and trigram search still operate.

## Validation and operation

The backend suite creates a disposable PostgreSQL database and applies all
Alembic migrations, including pgvector and runtime permissions. The fake agent
runtime replaces only model inference; queue, Agno persistence, events and
application services remain real. Gateway tests include an actual MCP SDK
transport roundtrip. External campus providers and SMTP are mocked in tests.

Production setup requires the existing Supabase project's PostgreSQL connection
and separately provisioned runtime logins. Supabase Auth keys alone do not
supply that database connection. Do not point different processes at separate
databases or add a broad service-role credential to repair a denied worker write.
Concurrent-user capacity has not been measured. Source changes and test results
alone do not establish which release is running on the remote application.
