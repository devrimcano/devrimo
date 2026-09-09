# Campus intelligence

This layer gives Scholar a configurable, auditable environment for public METU
information and private student context. It deliberately separates retrieval,
deterministic decisions, and protected data.

## Runtime shape

- The API serves student and administrator endpoints and exposes five broad
  Scholar tools: search campus knowledge, read an indexed page, plan a semester,
  reveal an eligible course group, and calculate arithmetic.
- `knowledge-worker` schedules and claims durable ingestion jobs with
  `SKIP LOCKED`, so more replicas can be added without duplicate claims.
  Retries end in a visible dead-letter state.
- PostgreSQL is the system of record and the only supported engine: retrieval
  is built from three of its indexes and cannot be expressed without them.
  Search fuses, by Reciprocal Rank Fusion in a single statement:
  - a GIN-indexed `tsvector` built with Postgres' Turkish (or English)
    Snowball configuration, which stems and folds Turkish's dotted/dotless I;
  - a GIN trigram index (`pg_trgm` word similarity), which covers the
    agglutinative bare-noun forms Snowball over-stems — "kütüphane" against a
    passage containing "kütüphaneye" scores ~0.9, an unrelated passage ~0.0;
  - pgvector cosine distance over an HNSW index for semantic matching.

## Source lifecycle

Embedding provider configuration requires a super administrator because it
selects a destination for server-side requests. Campus administrators can
reindex and activate their organization's existing index generations, but
cannot change provider settings. The legacy shared planner-catalog import
also requires a super administrator; the reviewed catalog supports scoped
campus administrators.

Administrators create a source and immutable draft revisions, preview the
parsed records, then publish a reviewed revision. Publication atomically makes
the revision active and queues ingestion. Previous revisions remain available
for rollback. The UI also installs reviewed starter drafts for the Registrar's
academic calendar, dormitory announcements, Sports Directorate, and METU CC
meturoam documentation.

Supported adapters are curated records, JSON, HTML page, Drupal listing, HTML
table, RSS/Atom, iCalendar, and PDF. The guarded fetcher enforces the source's
exact host allowlist, checks DNS results, blocks private/reserved addresses,
revalidates every redirect, observes robots.txt, applies time and size limits,
and uses conditional requests when the origin supports them.

Long canonical records are split at headings, paragraphs, and sentence
boundaries into deterministic chunks of roughly 450 tokens. Each chunk keeps
its document, section, page, and ordinal metadata. A small tail from the prior
chunk is used only as embedding context, not duplicated in stored source text.
`chunk_max_chars` and `chunk_context_chars` may be set on a source revision
within validated limits; the defaults are 1800 and 240 characters.

An empty parse fails closed: it does not erase the previous good snapshot.
Successful runs hash normalized records, preserve unchanged rows, replace
changed rows, and tombstone records that disappeared. Every answer carries its
source URL, retrieval time, effective time when known, and freshness state.

## Retrieval and conflicts

Queries first apply hard filters such as audience, department, degree level,
term, and validity window. Full-text and current-model semantic candidates are
retrieved independently, then combined with reciprocal-rank fusion. This keeps
their score scales independent and lets PostgreSQL use both its GIN and cosine
HNSW indexes. A minimum semantic similarity rejects unrelated vector matches,
and document-level deduplication prevents one long page from filling the result
set with its own chunks.
Canonical public text is the only content that may be embedded. Student
profiles, transcripts, schedules, email facts, group links, and raw tool output
are excluded from embeddings.

Source priority and freshness resolve ordinary conflicts. When authoritative
sources still disagree, retrieval returns the conflict instead of silently
choosing one. `read_campus_page` reconstructs all ordered chunks of the stored
indexed document; it is not an unrestricted web browser.

## Student context and updates

Verified department and degree context comes from the connected campus account.
A student may confirm it or enter a manual fallback, whose provenance remains
visible. Preferences are allowlisted, inspectable, editable, and deletable.
The update feed combines filtered public records with optional structured mail
facts. Mail-fact storage is opt-in; ingestion hashes and immediately discards
the raw body and stores only typed fields and a source reference. The current
implementation is pull-based and does not send push notifications.

## Academic planning

Before planning, the tool refreshes the student's transcript, schedule, and
academic identity directly from the read-only SAIS MCP connection. The model
cannot supply grades or mark prerequisites as satisfied. Offerings, prerequisite
rules, and planning policies are imported through administrator APIs from an
approved connector or reviewed catalog data.

The planner excludes any course whose offering or rule is unknown, evaluates
prerequisite expressions, removes time conflicts, enforces the configured
credit ceiling and user constraints, and deterministically searches eligible
combinations. It returns the maximum semester GPA under the requested grade
assumption, projected cumulative GPA, selected courses, exclusions, and data
timestamps. This is decision support, not enrollment authority.

## Protected course groups

Administrators enter course-level group invitations manually. Invite URLs are
encrypted at rest and never indexed. A link is decrypted only after the server
checks the student's verified context and enrollment for that course. The audit
row records the user, course, outcome, and reason—not the invite URL.

## Adding an adapter

Implement the adapter protocol in `app/knowledge/adapters.py`, add it to the
adapter registry, and test normalization with fixed local input. Adapters must
return canonical records and must not perform their own network requests;
fetching remains centralized so the SSRF and resource controls cannot be
bypassed. New production sources should start disabled as drafts and pass an
administrator preview before publication.
