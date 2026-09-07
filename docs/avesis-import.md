# AVESIS academic directory

The importer collects the English public METU AVESIS directory into the backend's
PostgreSQL database. It does not use student credentials, publish a UI, create
embeddings, or run on a schedule.

## Run

From `backend`, activate the project virtual environment and select the intended
PostgreSQL `DATABASE_URL`. The local copy used for validation is
`postgresql+asyncpg://devrimo:devrimo@localhost:5432/devrimo_avesis_remote_20260907`.
The copied database is private development data; do not expose it through a
public service or commit its backup.

```sh
alembic upgrade head
python -m app.researchers.cli sync --dry-run --researcher-id 4199
python -m app.researchers.cli sync --researcher-id 4199
python -m app.researchers.cli sync
python -m app.researchers.cli status RUN_ID
python -m app.researchers.cli sync --resume RUN_ID
```

`--dry-run` performs complete directory discovery and parses the selected
profiles' sections without opening a database. It reports linked activity counts;
actual imports additionally fetch the individual academic detail pages.
`--limit N` selects only N profiles. Without a limit, every discovered profile is
selected. Limited runs report their selection explicitly and are not full imports.

A fresh `sync` refreshes existing data; `--resume` continues the saved selection,
skipping already completed sections and profiles. No records are deleted on
refresh. Successful sections replace their prior snapshot; failed sections retain
it and appear in the run's errors. A profile's timestamp reflects its last successful
read, not a guarantee that every section refreshed. Check the run report for coverage.
An unexpected shutdown leaves durable checkpoints; use the printed run ID to resume.

Exit codes: 0 completed (including explicitly reported extraction warnings),
2 incomplete/interrupted or invalid import configuration, 1 unexpected failure,
130 operator interruption. A completed limited run covers only its selection.

## Source and identity

Discovery uses the same public search endpoint and researcher category as the
AVESIS directory, with 50 entries per page. Count changes, repeated identities,
empty responses, and partial results abort discovery. The sitemap is reconciled
but never used to manufacture identities. On September 7, 2026 the directory had
2,510 unique IDs and the sitemap 2,508 profiles; `daric` and `ekaraca` were present
only in the directory, with no unmatched sitemap profiles.

`researchers.id` is the METU AVESIS source ID. Names and aliases may change without
creating a new person. Academic sections are unique by researcher, section, and
language. Only `en` is imported; `tr` can be added later without duplicating people.
Within each section, academic items use AVESIS IDs/URLs or a normalized text hash
to avoid duplicates. Coauthors can legitimately reference the same academic work.

Stored data includes professional contact information, affiliations, research
areas, education, experience, publications, projects, activities, teaching and
supervision information when published, awards, metrics, and source links.
Variable section data is stored as JSONB with normalized visible text and extracted
items/links. Original Turkish titles in an English view remain unchanged.
Script contents are not stored or executed. Unknown layouts fail closed; unknown
sections with valid page structure preserve text and report a warning.

CVs, photos, attached files, external sites, and robots-excluded routes are retained
as links only. Extra interactive metrics panels are not expanded and are explicitly
listed as extraction warnings. General visible metrics are stored.

## Network and proxies

Direct access is the default. Optionally set the backend secret:

```sh
AVESIS_PROXY_URL=http://username:password@proxy.example:8080
```

All importer traffic then uses that HTTP CONNECT proxy, including discovery and
robots requests. There is no automatic rotation or fallback to direct connections,
and ambient proxy environment variables are ignored. Credentials are redacted from
settings representations and never placed in reports or database records.

Requests are serialized with a one-second minimum interval, longer robots crawl
rules, and Retry-After support. Transient failures receive at most three retries.
Persistent denial, challenge pages, or proxy connection failures stop the run with
its progress saved. A full academic import can take many hours because it follows
individual publication and project links as well as profile sections.

The guarded fetcher resolves and validates the origin, connects to the selected
public address, and preserves the original Host and TLS hostname. The dedicated
proxy transport is needed because the default HTTP library CONNECT implementation
does not honor the TLS hostname override when tunnelling to a pinned IP. An actual
local authenticated CONNECT + TLS integration test verifies the custom path.

## Validation

```sh
pytest tests/test_researcher_import.py tests/test_campus_intelligence.py -q
ruff check app/researchers tests/test_researcher_import.py
```

The existing test fixture creates and drops a randomly named test database through
`TEST_DATABASE_URL` (a PostgreSQL maintenance database). It does not truncate the
configured project database or the imported remote copy.

## Verified run (September 7, 2026)

- Remote `main` baseline: `2e0ef18`.
- Remote PostgreSQL snapshot restored locally as `devrimo_avesis_remote_20260907`;
  its `0018_student_timetable` revision upgraded successfully to `0019_avesis_researchers`.
- Live researcher 4199 imported successfully, including all nine profile sections
  and eleven linked publication pages. A second import reported `unchanged` and
  kept one researcher with 21 section/detail rows.
- All 469 backend tests passed. For the local test environment, set
  `AGENTOS_JWKS_FILE=` to avoid inheriting an obsolete local JWKS path from `.env`.
- Full import run: `d533dd3e-b1bb-4764-83cb-d13285a72875` (2,510 selected).
  Started against the local copy; completion must be checked with `status`.
  Process output is `/tmp/avesis-full-import-20260907.log` on this machine.

To inspect that run from `backend`:

```sh
DATABASE_URL=postgresql+asyncpg://devrimo:devrimo@localhost:5432/devrimo_avesis_remote_20260907 \
  .venv/bin/python -m app.researchers.cli status d533dd3e-b1bb-4764-83cb-d13285a72875
```

## Admin dashboard

Open **Admin → Operations → Researchers** (`/admin?section=researchers`). Admins can search saved profiles, inspect recent runs, and filter incomplete items. Progress refreshes every five seconds. Only super admins can start or resume imports.

**New import** queues either the full English directory or a 10-person sample. **Resume** queues unfinished work from an interrupted or incomplete run, retaining completed checkpoints. The API worker consumes queued jobs; closing the browser does not stop the import. After a worker process stops, use Resume once its database connection releases the execution lock. The worker does not automatically restart interrupted jobs. CLI and dashboard imports share the same database lock, preventing concurrent crawls.

Deploy migration 0019 before starting the API. Proxy configuration remains server-side; the dashboard displays only whether it is configured.

### PostHog instrumentation

The Researchers tab emits `admin_section_viewed`; Start and Resume retain the shared `admin_action` event. Each executed import attempt emits `researcher_import_started` and one `background_job_completed` event with `job_kind=researcher_import`, the run ID, resume flag, duration, status, and aggregate counts. Expected source failures and incomplete imports are distinct from unexpected defects; shutdown cancellation is a cancelled outcome. Re-submitting an already completed run emits no new job events.

Structured `researcher_*` logs cover profile starts/completion, saved or checkpoint-skipped sections, HTTP response status, retry/backoff, and errors. Profile logs include the researcher ID and run ID; section logs include the section key. These use the existing PostHog Logs pipeline in both API workers and newly started CLI processes. CLI exit flushes buffered logs and events. Raw profile bodies, names, emails, search terms, URLs, and proxy credentials are not added to this instrumentation; unexpected exception messages are replaced with a safe error type.

A force-killed process cannot flush a terminal event. Its existing database checkpoints and last exported logs remain available; Resume starts a new observed attempt for the same run. Already-running processes need a restart to load this instrumentation.
