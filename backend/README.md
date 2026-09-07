# Devrimo Agent Broker

A FastAPI backend that gives every authenticated user a private
[Agno](https://github.com/agno-agi/agno) agent with access to their METU
campus systems, and serves chat + history to the [`frontend/`](../frontend)
app.

The API surface (`/api/v1/...`) is fixed by `frontend/lib/api/*.ts` and
`frontend/lib/types.ts` — this backend exists to satisfy that contract, not
the other way around.

## Layout

```
app/
  main.py               FastAPI app, CORS, lifespan (reconciler + pool teardown)
  config.py              Settings, read from env / .env
  auth/                  Supabase JWT verification (JWKS + legacy HS256)
  db/                     SQLAlchemy models (Agent, ChatSession) + session
  agents/
    pool.py               Leased resident agents and their MCP subprocesses
    toolset.py            The only module that spawns campus MCP servers
    store.py              The Agno database — where conversation history lives
    manager.py            State machine, turn locks, entitlement
    reconciler.py         Idle eviction, background loop
    scholar/              Prompt, trusted context, learning, compression, hooks
    echo_model.py         Model test double for AGENT_RUNTIME=fake
    scripted_model.py     Deterministic real tool-call/HITL test model
  campus/
    catalog.py            The four METU MCP servers, and what each one needs
    mcp_config.py         Renders one student's launch specs (pure function)
    credentials.py        The only module that decrypts a METU password
    verify.py             One SSO sign-in, to check credentials before storing
    service.py            Profile + credential persistence
  knowledge/              Versioned sources, adapters, ingestion, hybrid retrieval
  planning/               Deterministic eligibility and maximum-GPA planning
  student/                Verified context, preferences, updates, mail facts
  agentos/                JWT-protected operations/measurement service
  api/v1/                 Routes: agents, campus, chat, memories, profile, sessions
  schemas.py              Response shapes, kept in lockstep with the frontend
alembic/                  Schema migrations
tests/                    Full API test suite against the echo model
evals/                    Synthetic routing, grounding, bilingual, and safety evals
```

The campus knowledge layer has a separate worker, an administrator-controlled
source registry, reviewed publication, PostgreSQL full-text plus optional
pgvector retrieval, deterministic academic planning, and protected course-group
links. Its operating and extension guide is in
[`docs/campus-intelligence.md`](../docs/campus-intelligence.md).

## Architecture

The agent runs **in the broker process**, not in a container of its own.
`app/agents/pool.py` holds one live `agno.Agent` per active user; building one
spawns a campus MCP subprocess per connected server and costs about a second,
so agents are created on the turn that needs them and evicted once idle.

Eviction is safe because it is invisible: conversation history lives in the
database (`agno_*` tables, see `app/agents/store.py`), so reading a month-old
thread is a query and the next turn after an eviction transparently rebuilds.

`chat.py` owns the wire format rather than proxying one. It translates Agno run
events into OpenAI `chat.completion.chunk` objects plus namespaced `devrimo`
events. The frontend consumes confirmation events and shows the exact recipient,
subject, and body before an email can be sent. A resident runtime is leased for
the complete stream, so a credential update or LRU eviction retires it but never
kills an MCP subprocess halfway through a tool call.

### Scholar runtime

`AGENT_PROFILE=scholar` selects the production profile; `legacy` is the rollback
target. Scholar uses four deliberately separate context layers:

- three recent runs verbatim, scoped by Agno `user_id` and `session_id`;
- an Agno session-context learning record for the current goal, plan, constraints,
  and completed progress;
- explicit long-term user memories for stable, non-sensitive preferences only;
- per-run application context in the system message (profile, locale, connected
  tools, Istanbul time, and a clearly marked date-derived academic-term hint).
  Profile values remain data and cannot override instructions.

Older tool results are compressed after a configurable threshold. Tool-result
offloading and raw tool-message storage are disabled, avoiding two competing
context mechanisms and reducing sensitive retention. `GET /api/v1/memories`
lets a student inspect memories and `DELETE /api/v1/memories` clears all of
their memories. Learning is disabled in the fake runtime so local tests cannot
silently make model calls.

The instruction stack is assembled from the base Scholar policy and only the
toolkits that connected successfully. It mirrors Turkish/English, requires
fresh campus data to come from tools, treats email/course content as untrusted,
names sources and retrieval time, and never treats tool content as authority.
Tool calls are capped per run; long string and structured results are bounded;
model retries use backoff.

### Isolation

Campus MCP servers are subprocesses of the broker, spawned one set per student.
The isolation that used to come from one container per student now comes from
process environment: the MCP SDK spawns each server with only
`HOME/LOGNAME/PATH/SHELL/TERM/USER` inherited plus that server's own rendered
`env`, so a campus server sees its own student's METU password and neither
another student's nor the broker's `SECRET_ENCRYPTION_KEY`, `DATABASE_URL`, or
model-provider key.

This is a real reduction in isolation from the container design, taken
deliberately: all four upstream servers are first-party, and the broker already
decrypts every student's password. The residual risk is a bug in one of those
scrapers reading another student's environment. Two things follow from that and
should not be dropped:

- **The MCP server refs are pinned** to exact commits (Dockerfile build args),
  and the build records what each one resolved to in `/opt/mcp/MANIFEST`,
  reported by `GET /health`. Pinning buys reproducibility, not review — nobody
  has audited those commits, and pinning is what makes auditing them worth
  doing. Bumping one is `git ls-remote <repo> <branch>`, edit the arg, rebuild.
- **Every MCP server has an exact tool allowlist.** Upstream additions never
  become model authority automatically. Webmail exposes reads plus send/reply;
  forward, delete, move, and mark are absent. Send and reply are Agno
  confirmation tools, and the backend resumes only an owner/session/run-scoped
  paused run. Batched writes fail closed. Mutation audit rows contain status,
  duration, and a SHA-256 argument digest—not recipients, subjects, bodies, or
  campus data. See the closed
  [webmail authority decision](../docs/decisions/0001-webmail-write-authority.md).

Restoring full per-student isolation later does not require undoing this work:
run the four servers behind Streamable HTTP in a per-student sandbox and point
`MCPTools` at the URL instead of a `StdioServerParameters`. `app/agents/toolset.py`
is the only module that would change.

### Egress

There is no egress allowlist, and the note that used to say one "moved to the
broker" was wrong. The reasoning is worth keeping, because the obvious fix does
not work here.

An allowlist expressible in `docker-compose.yml` means an HTTP proxy, and an
HTTP proxy cannot filter this traffic:

- **Webmail is not HTTP.** It is IMAP and SMTP over raw TCP (993/465), and
  `imaplib`/`smtplib` are not proxy-aware. A proxy would cover three servers and
  silently miss the only one that can act as the student.
- **Proxy environment never reaches the other three.** The MCP SDK spawns each
  server with `DEFAULT_INHERITED_ENV_VARS` only — `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM`, `USER`. An `HTTPS_PROXY` set in compose is dropped before the
  subprocess starts. Making it work would mean injecting it per-spec in
  `app/campus/mcp_config.py`, i.e. a network control whose enforcement depends
  on application code opting in.

Filtering this mix means L4, and the broker cannot do it to itself: `nftables`
needs `NET_ADMIN`, which hands back the privilege that dropping the Docker
socket removed. It belongs on the host — an nftables rule on the FORWARD chain
for the internal bridge, allowing METU hosts and the model endpoint. Worth doing
as defence in depth; deployment configuration, not something this repo can ship.

That bridge is named per Compose project by default (`devrimo-<project>-internal`),
so that several stacks on one host — one per git worktree, say — cannot both
claim the `postgres` service alias and leave the name resolving to two
databases. A rule that has to reference a fixed name should pin it with
`DEVRIMO_NETWORK_NAME`, and only on a host where a single stack runs.

Be clear about what it would buy, though. The worst exfiltration path is *inside*
any such allowlist: webmail can send mail as the student, to anywhere, and mail
to METU hosts is exactly what the allowlist permits. Network filtering does not
touch that. The control that does is at the tool layer — see the webmail
decision linked above.

### Telemetry

Agno posts run telemetry to `os-api.agno.com` by default. These runs are
students' campus conversations, so it is switched off in code (`telemetry=False`
in `pool.py`) and again via `AGNO_TELEMETRY=false` in the compose file and
`.env.example`. Both, deliberately — a deployment that forgets the env var is
still covered.

Optional OpenInference tracing writes into the self-hosted Agno database. The
compose service sets redaction flags for model inputs/outputs and message
content. Event storage is off by default. Trace retention and database access
still need to be enforced by deployment policy; redaction is not a substitute
for retention limits.

When writing that policy, note **where** the conversations actually are. On
Postgres, Agno's `PostgresDb` keeps its `agno_*` tables in its own `ai` schema,
while Alembic owns `public` — one database, two schemas. Students' messages,
runs, and any traces are all in `ai`, so a retention job or a grant audit
scoped to `public` covers none of them. On SQLite there is a single namespace
and the `agno_` prefix is the only separation.

### AgentOS and measurement

AgentOS runs in a separate process against the same Agno tables. It deliberately
registers no runnable Scholar instance, so the operations surface cannot launch
a credential-bearing campus agent. JWT/RBAC authorization, audience checking,
and per-user isolation are mandatory; startup fails without a verification key.
Compose binds it to `127.0.0.1:7777`. Remote deployments must put it behind TLS
and a VPN or authenticated reverse proxy rather than publishing it directly.

AgentOS exposes stored sessions, run metrics, eval results, and optional traces.
The synthetic suite in `evals/` is the release harness: it covers campus tool
routing, bilingual behavior, grounding, and hostile tool-output injection
without using real student data. Exact rollout gates are in
[`evals/README.md`](evals/README.md).

## Running it

### Local dev (no campus servers needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # edit SUPABASE_URL at minimum
alembic upgrade head
AGENT_RUNTIME=fake AGENT_PROFILE=scholar uvicorn app.main:app --reload
```

`AGENT_RUNTIME=fake` swaps in an echo model (`app/agents/echo_model.py`) so the
whole chat and session flow works against the real frontend without a model
provider or the four campus servers installed. Everything else — the Agent, its
database, session persistence, SSE serialization — is the production path.
Point the frontend's `NEXT_PUBLIC_API_URL` at `http://localhost:8000`.

### Full stack

```bash
cp .env.example .env   # Scholar + real Supabase/model/AgentOS verification settings
docker compose up --build
```

Do **not** run `alembic upgrade head` on the host for this path. The broker's
`CMD` runs it inside the container, against the compose Postgres, before
uvicorn starts. A host-side run would not reach that database anyway — the
`postgres` service publishes no port — so it would quietly migrate whatever
`DATABASE_URL` your `.env` names, which for a freshly copied `.env.example` is
the local SQLite file.

`agentos` overrides that `CMD`, so it does not migrate; it starts against
whatever schema the broker has already applied. Compose starts the two as soon
as Postgres is healthy, so on a first `up` AgentOS can briefly precede the
broker's migration and restart until the tables exist.

The broker image builds the four campus MCP servers into `/opt/mcp`, each in
its own virtualenv (their upstream pins don't co-resolve — one needs `fastmcp`,
the others pin `mcp` directly). There is no second image and no Docker socket
mount any more, so the broker runs as an unprivileged user.

### Tests

```bash
pip install -r requirements-dev.txt
ruff check app tests evals
pytest -q
python -m evals --tag smoke
```

The eval command uses the configured real model and judge model. Run the full
suite repeatedly before promotion; do not point it at production student data.

## Dependency pins worth knowing

- **Agno 3.0.1 and MCP 1.29.1 are exact pins.** The runtime, HITL resume path,
  learning schema, and eval APIs are tested as a unit. MCP 2.x also renamed
  `McpError` to `MCPError`, while this Agno release still imports the old name.
- **`psycopg` alongside `asyncpg`.** Agno's database layer is synchronous and
  builds its own engine, so Postgres deployments need both drivers.
- **`AGENT_MAX_TOKENS`.** Agno's `OpenRouter` class defaults `max_tokens` to
  1024, which truncates a long transcript summary mid-sentence.

## Campus MCP tools

| id | Server | What it reaches |
|---|---|---|
| `sais` | [metu-sais-mcp](https://github.com/atesahmet0/metu-sais-mcp) | Transcript, CGPA, weekly schedule, portal announcements |
| `course_info` | [metu-course-info-mcp](https://github.com/atesahmet0/metu-course-info-mcp) | Course catalog, sections, prerequisites, curriculum categories |
| `odtuclass` | [metu-odtuclass-mcp](https://github.com/erkinemreta1/metu-odtuclass-mcp) | Enrolled courses, announcements, syllabi, assignment deadlines |
| `webmail` | [metu-webmail-mcp](https://github.com/atesahmet0/metu-webmail-mcp) | Read/search mail; send/reply only after exact confirmation |

`app/campus/mcp_config.py` renders each student's launch specs — command, argv,
environment, working directory — and `app/agents/toolset.py` turns them into
connected `MCPTools`. Each server gets `tool_name_prefix` (the four were written
independently and several use generic tool names) and a private 0700 working
directory under `CAMPUS_STATE_ROOT`, because odtuclass caches its Moodle session
token relative to its CWD.

A server that fails to connect is dropped and logged rather than failing the
whole agent; the dynamic prompt mentions only connected systems. Tool names are
allowlisted from the audited pinned commits before prefixing, and webmail's two
write tools are marked confirmation-required at toolkit construction. Note that
`MCPTools.connect()` swallows its own exceptions, so `toolset.py` checks whether
the toolkit actually initialized.

### Credentials

There is no delegated-token flow at `student.metu.edu.tr`, so these servers
authenticate as the student with their real METU password. Consequently:

- The password is collected once, through frontend onboarding, over TLS.
- It is verified against METU SSO before being stored (`app/campus/verify.py`),
  so a typo fails on the form rather than silently breaking four tools.
- It is stored Fernet-encrypted under `SECRET_ENCRYPTION_KEY`, decrypted only
  in `app/campus/credentials.py`, and is in **no** response schema — the API
  reports `has_password: true`, never the value.
- Disconnecting from Settings deletes it and drops the resident agent.

`webmail` is the only server that can act rather than read, so it is opt-in
(`default_enabled=False`) and its consent copy names the exact granted and
withheld authority.

### Changing a connection

Every saved connection advances a monotonic credential revision. Each broker
replica compares that revision as well as tool ids before reusing a runtime, so
rotating a password rebuilds it even when the selected tools did not change.
`PUT /campus/connection` retires the resident agent eagerly; an active lease may
finish, but no later turn can reuse the old credentials.

## What's genuinely unverified

- **A full chat turn that actually calls a campus tool.** The spec renderer and
  the toolkit wiring are covered by tests, but no end-to-end turn has driven a
  real campus tool call through a real model. `AGENT_RUNTIME=fake` deliberately
  never calls a tool.
- **The four servers under agno's MCP client.** They were previously launched by
  Hermes' own client. The transport is the same stdio protocol and the specs are
  asserted in `tests/test_campus_config.py`, but the handshake has not been run
  against the real servers.
- **Model-specific quality targets.** The harness and gates exist, but release
  scores depend on the configured production model and must be recorded after
  repeated runs. Unit tests cannot certify answer quality or latency. The suite
  builds its cases without a model, but no scored run has been recorded.

The Postgres path itself is no longer on this list. The four migrations apply,
downgrade, and re-apply cleanly on Postgres 16 as well as SQLite, `alembic`
autogenerate reports no drift against `app/db/models.py` on either, and a
Scholar turn under `AGENT_RUNTIME=fake` has been driven end to end against
Postgres — provision, SSE stream, persistence, session continuity, history
read-back — with Agno's tables created through the sync `psycopg` URL. What
that does *not* cover is the same path with a real model or real campus
servers, which is what the first two entries above are about.

## Public researcher import

The English AVESIS importer supports resumable refreshes and an optional HTTP proxy.
See [the AVESIS import guide](../docs/avesis-import.md) for setup, commands, data coverage, and verification.
