# Devrimo — handoff

Written for whoever picks this up next. It covers what the project is, how it is
laid out, how to verify a change, how it reaches production, what the owner has
asked for, and the traps that have already cost time.

Last updated: 2026-09-06.

---

## 1. What this is

**Devrimo** is a student assistant for METU/ODTÜ, live at `devrimo.ates.digital`.
It signs a student in, connects to the university's own systems on their behalf,
and answers questions about their studies. Two surfaces matter:

- **Chat** — an agent with access to the student's campus data.
- **Schedule planner** (`/schedule`) — a visual weekly timetable builder. Pull in
  the courses your curriculum says you still need, see each course's sections
  with instructor, day, time and room, see which sections you are *not allowed*
  to register for and why, then generate a conflict-free week.

The planner is where most of the recent work has gone, and where the product is
most opinionated: it is meant to be something a student can register from, not a
pretty grid.

### Non-negotiables the product rests on

- **Never invent campus data.** A course, a section, a time, a room, a credit or
  an eligibility verdict either came from METU or is not shown. When something
  cannot be read, say so — "we could not read your electives" and "you have no
  electives left" are different sentences and the UI must not confuse them.
- **Turkish is not an afterthought.** Turkish collation (`AÂBCÇDEFGĞHIİJKLMNOÖPQRSŞTUÜVWXYZ`),
  Turkish uppercase (`i` → `İ`), and folded search (a student typing on an
  English keyboard is the normal case). Every user-facing string is bilingual
  via `t(tr, en)` / `pick({tr, en})`.
- **Be gentle with METU.** These requests carry a real student's credentials.
  One at a time, slowly, off-peak, with a hard daily ceiling. A burst that looks
  like a crawler risks that student's account, not ours. See
  `backend/app/campus/warmer.py` — its docstring is a design constraint, not a
  comment.

---

## 2. Layout

```
backend/                     FastAPI + async SQLAlchemy + Alembic, Postgres 16
  app/
    api/v1/                  HTTP surface. schedule.py is the big one.
    agents/                  Agno 3.0.1 runtime, agent pool, turn locks, toolsets
    campus/                  Everything that talks to METU
      course_info.py         The catalog read path + its caches. Central.
      curriculum.py          Deterministic "what do I still have to take"
      eligibility.py         Section restriction rules -> a verdict
      departments.py         153 departments, code <-> abbreviation
      departments.json       Generated directory (see scripts/)
      warmer.py              Overnight cache warm-up. Read its docstring first.
      catalog.py             Which MCP servers exist and how they are launched
    core/
      ttl_cache.py           In-process cache with per-key single flight
      persistent_cache.py    Shared DB cache (schedule_data_cache table)
      digest.py              stable_digest / owner_digest — cache keys, erasure
    planning/                Transcript + student-card sync from SAIS
      mcp_bridge.py          Parses SAIS payloads. Fragile by nature; well commented.
    knowledge/               Campus knowledge base, ingestion, retention sweeps
    student/purge.py         Data erasure. Anything cached per student must be reachable from here.
    db/models.py             All tables
  alembic/versions/          Migrations, linear chain
  vendor_patches/course_info/  Local patches to a third-party MCP server (see §5)
  tests/                     pytest, async by default

frontend/                    Next.js 16 / React 19 / Tailwind v4 / Supabase auth
  app/(app)/schedule/        The planner page
  app/api/**                 Server-side proxy: the browser never calls the broker directly
  components/schedule/
    schedule-planner.tsx     ~1500 lines. The planner. Pool, search, grid, generation.
    planner-assistant.tsx    Collapsible AI panel (SSE against /api/chat)
    planner-intro.tsx        Interactive walkthrough with data-tour spotlights
  lib/api/schema.ts          GENERATED from the backend's OpenAPI. See §4.

scripts/deploy-vps.sh        The deploy. Runs on the host over SSH.
.github/workflows/deploy.yml verify -> deploy, on every push to main
```

### The MCP servers

Campus access is per-student subprocesses speaking MCP over stdio: `sais`,
`course_info`, `odtuclass`, `webmail`. They are launched from
`app/campus/catalog.py`, one process per student per server, each carrying that
student's credentials in its environment. Spawning one costs about a second.

---

## 3. How a catalog read works

This is the hot path and worth understanding before changing anything near it.

```
HTTP handler (api/v1/schedule.py)
  -> catalog_session(db, user_id)          one MCP connection for the whole request
  -> call_course_info(...)                 app/campus/course_info.py
       1. in-process TTLCache (15 min, single-flighted)
       2. persistent DB cache (shared across students for catalog tools)
       3. per-student asyncio.Lock, then the MCP subprocess
  -> vendor_patches/course_info/sais_client.py
       -> HTTPS to student.metu.edu.tr
```

Things that are load-bearing here:

- **`_SHARED_TOOL_TTLS` is an allowlist.** A tool on it is cached once and served
  to every student. The two `get_student_*` tools return one person's
  curriculum and must never be on it — `tests/test_cache_sharing.py` pins this.
- **Catalog rows are written with `owner_hash=None`**, so one student erasing
  their data does not delete a course list everyone reads. Anything derived from
  one student must set it, so `app/student/purge.py` can reach it.
- **The lock is taken inside the single-flight factory, never around it.** Order
  is always flight-then-lock. Inverting it deadlocks the whole catalog.
- **SAIS is stateful.** The tokens for the next request are read out of the
  previous response and the "current page" lives in a PHP session. Two calls in
  flight land on each other's pages, and a stale token returns the *previous
  page* rather than an error — so the failure mode is a plausible wrong answer,
  not a crash. This is why calls are serialised at two levels.

---

## 4. Verifying a change

There is no local Postgres and no local backend venv. Everything runs against
the production host, which has both. Helper scripts live in the session
scratchpad (see §7); the recipes are:

**Backend tests (the full suite, 452 passing).** `tests/conftest.py` builds a
throwaway database at import time and the application role cannot create one, so
the run happens as the host's `postgres` superuser over peer auth. It creates
and drops its own database and never touches `devrimo`.

```
PYTHONPATH=/tmp/pytools:<staged>/backend \
TEST_DATABASE_URL="postgresql://postgres@/postgres" \
/opt/devrimo/backend/.venv/bin/python -m pytest tests/ -q
```

**Import preflight.** Stage the tree on the host and import it with the live
venv *before* touching services. An import cycle otherwise surfaces as the API
failing to come back with the old source already replaced.

**Undefined names.** `undefined_names.py` walks each function with a real scope
chain. It takes the path to scan as an argument and reports how many files it
scanned — if it says zero, it scanned nothing and its "0 undefined names" means
nothing. This has burned us: `py_compile` does not catch a call to a function
that no longer exists, and that shipped a 500 to production once.

**Frontend.** `npx tsc --noEmit`, `npm run lint`, and a real `next build`. The
build must be run **on the host**: this repo is a git worktree, so `node_modules`
is a symlink and Turbopack refuses it locally.

**`npm run contracts:generate` is mandatory whenever a backend route changes.**
CI regenerates `frontend/lib/api/schema.ts` from the backend's OpenAPI document
and fails the entire run on any difference. There is no local backend env, so
export the OpenAPI JSON on the host and run `openapi-typescript` against it
locally. Forgetting this has blocked a deploy once already.

---

## 5. How it reaches production

**`.github/workflows/deploy.yml` runs on every push to `main`.** A `verify` job
(full pytest against a pgvector service, plus `contracts:check`, lint and build),
then `scripts/deploy-vps.sh` over SSH. A failing `verify` skips the deploy
entirely, so the site is never left half-updated.

**Work must reach `main` to survive.** For months the habit was to deploy
straight from the working tree and never push. That stopped being safe the
moment a collaborator started merging PRs: the workflow deployed `main` with
`rsync --delete` and erased three weeks of planner work off the live server. It
was recoverable only because it was committed locally. Deploying from the
working tree is still fine for trying something on the live box — but treat it
as temporary, because the next merge wipes it.

**The deploy is systemd, not Docker.** `devrimo-api`, `devrimo-web`,
`devrimo-knowledge-worker` behind Caddy. The Dockerfile exists and patches
`vendor_patches/course_info/*.py` into the image before building a wheel, but
**this host installed the wheel and never rebuilds** — so `src/` is a directory
Python never imports and the patch has to reach site-packages too. `deploy-vps.sh`
now does that, copying only on change and rolling back if the patched server
stops importing. Before that it was a manual step somebody had to remember.

**Migrations run after the API is stopped.** A broken migration chain therefore
takes the site down mid-deploy — which has happened, with
`Can't locate revision identified by '0018_student_timetable'` after a rebase
dropped a migration file while a later one still pointed at it. Check the chain
resolves to a single head before merging anything that touches
`alembic/versions/`.

---

## 6. What the owner has asked for

Recorded because these are standing instructions, not one-off requests.

**Working style**

- Do not touch encryption keys. If you changed one, put it back.
- Do not push, open a PR, rebase or deploy without asking. Approval is per
  action; one yes does not cover the next thing.
- Read the logs before theorising. Guessing from screenshots has been wrong
  every time; `journalctl -u devrimo-api` is the first move, not the last.
- Do not tighten timeouts while debugging. A shortened deadline replaces the
  error you are trying to see with "timed out". Split or raise budgets instead.
- Say plainly when something is unmeasured. A design expectation is not a
  measurement.

**Product**

- The chat agent must see **the schedule built in the planner**, not the one
  SAIS reports. `sais_get_schedule` stays available but is only reached when the
  student explicitly asks for the SAIS one — it is out of date roughly always.
- Course search must work by full code *or* full name. Nobody types `phys2`.
- Section restrictions vary per section. Show a red warning naming the rule that
  blocks it, and keep ineligible sections out of generated schedules unless the
  override is on — including for a course added by hand.
- Cache refreshes **append**; an empty result must never wipe what is stored.
- HIST2201/2202 for Turkish citizens.
- The timetable must fit on screen without scrolling, with distinct colours, one
  label per line, and the room or `TBA`. Excel-like, not decorative.
- The introduction is interactive and points at the real control, not a list of
  paragraphs.

**Communication**

- Turkish by default in conversation, unless asked otherwise. Code, comments and
  commit messages stay in English.
- Direct and blunt is fine and preferred. Do not pad.

---

## 7. Credentials and the scratchpad

VPS access (host, port, root password) exists **only in the session scratchpad
helper scripts**, never in this repository, and must stay that way. The scratchpad
also holds the working tools: `preflight.py`, `run_tests_ref.py`,
`build_check.py`, `export_openapi.py`, `undefined_names.py`, `make_archive.py`,
`deploy.py` and a pile of one-off probes. They are convenience, not product —
rewrite rather than trust them, and note that at least one (`undefined_names.py`)
was silently broken for a while.

The student's own METU password has never been entered into a login form by hand
and must not be. Credentials reach the MCP subprocesses through the encrypted
store and nowhere else.

---

## 8. Where the work stands

Recent branches, newest first:

- **`perf/catalog-fetch`** — open as PR #12, not merged. Takes the agent out of
  course discovery (`POST /schedule/curriculum` replaces the agent-driven
  `/ai-plan`, which measured a 95.8s median in production, ~88s of it model
  round trips), holds the SAIS proxy session so a cold eight-section course goes
  from 53 requests to 26, serialises campus calls at both entry points, adds
  `POST /schedule/sections` for batched reads, extends the warmer to courses
  students actually asked for, and makes the deploy install the vendored patch.
- `feat/schedule-planner` (#10) and `fix/api-contract` (#11) — merged and live.

### Deliberately not done

**Reusing a course page's hidden inputs across its sections.** Would take a cold
course from 18 requests to 11, but only if those fields are stable across
sections rather than encoding the last-submitted one. That is an empirical
question about a page that cannot be seen from here, and guessing wrong returns
the previous section's eligibility table for every section — silently, and
looking like a section-restriction bug rather than a caching one. It needs one
live observation first: compare a course page's hidden inputs before and after a
`submit_section` POST.

### Known and queued

An agreed plan exists for these; the speed work above was phase one of it.

**Constraints disappearing** (the owner reported this; three confirmed causes,
all in `schedule-planner.tsx`):

1. Verdicts are not persisted while section lists are, and `toggleCourse`
   returns early when sections are cached — so after a reload every section
   renders as allowed and generation places ineligible ones. Agreed fix: never
   persist verdicts, and fire one batch `POST /constraints` for the whole pool
   once hydration finishes.
2. The bulk path merges an empty answer unconditionally. The backend reports an
   unreadable course as `{"error": ..., "sections": {}}`, so one failure wipes
   good verdicts. The single-course path already guards against this; the guard
   has to move to the bulk one.
3. No cancellation token, so an in-flight run writes verdicts back for courses
   that have since been cleared from the pool.

Agreed behaviour when verdicts cannot be refreshed: **keep the ones you have and
say they may be stale**, rather than silently treating the course as
unrestricted. With nothing held at all, mark the section "unverified" rather
than "allowed".

**UI and interaction** — about fifteen confirmed issues, the sharpest being:
Enter in the search box can add the previous query's first hit (suggestions
update inside a 300 ms debounce); typing a title with no suggestion creates a
pool entry from the raw text; section fetches are not gated on the department
having resolved, so a click in the first few hundred milliseconds returns 422;
`generateSchedule` runs on a snapshot of state while making network calls;
entries outside 08:00–17:00 count toward credits and conflicts but are never
drawn; the `#plan=` share hash is never cleared and silently overwrites a saved
plan on every reload.

**Chat course tools** — `get_course_sections` and `check_section_eligibility`
return `409 Course catalog access is not enabled` in production logs;
`plan_semester` fails validation because the model omits `term`, which should
default to the student's active term rather than being required.
