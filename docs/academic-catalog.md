# Reviewed academic catalog

Shared course information belongs to the organization catalog. Student
transcripts, profile fields, and actual registrations remain separate SAIS data.
An imported observation is evidence, not a published course.

## Storage and administration

The additive `0033_academic_catalog` migration creates 20 catalog tables.
Courses and terms have stable identities. Course revisions, sections, meetings,
instructors, instructor assignments, restrictions, prerequisite groups and
requirements, and replacement relationships have dedicated relational tables.
Releases select immutable revisions, with an active release pointer per term.
Foreign keys and database guards enforce organization consistency and protect
published history.

JSONB retains original source responses, draft edit documents, field overrides,
component evidence and job checkpoints. Published course facts and rules are
read from the relational records. Keeping the original response allows parser
changes to be investigated without losing the evidence behind a course.

The Courses tab supports term/department/text/state filters, coverage indicators,
course and section editing, structured eligibility rules, explicit verification
with evidence, source history, import progress, selected-course publication,
conflict acknowledgement and release rollback. API permissions distinguish
read, edit/import and publish access; stale draft edits and publication against
an outdated release are rejected.

Source review includes up to 100 recent course and contributing listing
observations. Expanding an observation loads its original response, parsed
candidate, arguments and parsing issues through an organization-scoped admin
endpoint. Observation time and actual source fetch time are shown separately.

## Activation

1. Apply the additive academic-catalog migration with the migration identity.
2. Enable `ACADEMIC_CATALOG_INGESTION_ENABLED=true` for the catalog worker.
3. Use **Admin → Courses / Dersler** to import, inspect source differences, edit
   drafts, and publish selected courses. Campus admins can edit/publish within
   their organization; operators can read.
4. Enable `ACADEMIC_CATALOG_READS_ENABLED=true` consistently for the API and
   assistant processes after initial publication. This routes shared course
   reads through published releases and removes shared raw Course Info tools
   from student MCP toolsets. Personal Course Info operations remain available.

Do not enable publication as an operational workflow while leaving student
readers on the old path. An unpublished or incomplete course has an explicit
unavailable/unknown status; the published read path must never silently fetch
the old source cache.

## Importing a retained cache

Run from `backend` using the destination catalog database configuration:

```sh
python -m app.academic_catalog.import_cli --term 20261
```

This is a dry run. Add `--apply` to write recovered observations to drafts.
Use `--source-url-env CATALOG_IMPORT_SOURCE_DATABASE_URL` to read a separate
PostgreSQL snapshot. Supply the URL through that environment variable, not a
command-line argument. `--organization` selects the destination organization.
The source transaction is read-only, and only ownerless `course-catalog` cache
rows are read. No source credentials or student payloads are imported.

Request identities must match the stored cache digest. Unknown queries are
reported rather than assigned an invented semester. Imported timestamps remain
observation timestamps; no cache import claims a new METU fetch or publishes
records automatically.

The September 7 snapshot examined during implementation contained 987 shared
responses. The backfill recovered exact identities for 946 (771 restrictions,
51 course details, 124 listings); 41 response identities could not be recovered.
These are snapshot measurements, not live METU coverage.

A complete rehearsal in an isolated PostgreSQL database produced 2,222 course
identities, 2,296 drafts and 946 observations, with zero releases and zero
claimed new source fetches. The admin list used four SQL queries for a page of
50 courses in a term containing 2,087 courses. It batches related reads to
avoid a query per course. Following the audit, admin filtering, counts, and
pagination now run in SQL before page hydration. Public listing filters also
run in SQL while retaining the existing full matching-list response contract.

## Source traffic

In **Admin → Courses**, choose the term and click **Import all departments**
to queue a full university import without entering department or course codes.
The job discovers departments, then course listings, details, prerequisites,
replacements, and section restrictions. **Refresh jobs** shows its progress.
Imported courses remain drafts until reviewed and published. Repeated clicks
reuse an active full import; directory-only maintenance has a separate scope.

The catalog worker reuses the vendored SAIS parser with a database admission
gate before every HTTP request, including authentication and redirects.
`CATALOG_WARM_DAILY_LIMIT` limits admitted attempts, not MCP tool invocations.
Reservations are conservative: cancellation after reservation does not refund
the budget. Database failure fails closed before contacting METU.

Default import hours are 01:00–07:00 Europe/Istanbul with a 20–30 second gap and
200 admitted attempts per day. On-demand imports enter the same queue. Jobs
retain their remaining operations and resume after pacing/budget deferral.
Authentication errors stop the job; transient failures have bounded backoff.
Publication is not available to the catalog worker identity.

Department and thesis listings must identify the department and term.
Detail and rule pages must identify the requested course and term, and restriction pages
must also identify the section. Numeric form values and labelled page headers
are checked; human-readable term labels are matched against SAIS's own semester
selector. Missing or conflicting identity is a failed read, even when the page
contains a plausible detail or rule table.

Maintenance removes unreferenced failed observations after 30 days. Successful
observations are eligible after 90 days only when a newer successful answer for
the exact source request exists. The latest answer and all draft or relationally
referenced publication evidence remain available. Drafts also conservatively
protect matching source scope because older draft reference lists were capped.

Jobs rotate eligible campus accounts and use leases with ownership tokens, so
an expired worker cannot continue writing after another worker claims its job.
Source identities are checked before parsing; malformed or partially readable
rule tables cannot become verified empty rules.

## Publication and planning

Every publication produces an immutable release. Selected course revisions
change atomically while the other release entries carry forward. An admin
correction remains an override until explicitly removed; a new source value
is shown as a conflict instead of silently replacing it. Rollback creates
another release referencing previous revisions, without resetting freshness.

The Sources view can remove selected overrides with a reason and a draft
revision check. This replays retained source observations into the selected
draft fields, retaining original evidence timestamps. If there is no source
value, the field becomes unknown. Other overrides and published revisions
remain intact, and the restored draft still requires publication. Admin
requests validate typed academic fields and cannot supply derived freshness,
source timestamps or observation identities.

Source evidence, parsing completeness, freshness, and a student's eligibility
are separate properties. An empty verified restriction table is unrestricted;
an unreadable table is unknown. Missing GPA, year, or applicable program data
cannot produce verified eligibility. Minimum prerequisite grades are enforced.

Details, sections and restrictions expire after seven days; directory,
listing, prerequisite and replacement evidence expires after 30 days. A
configured registration start invalidates older time-sensitive evidence.
Verification with admin evidence has its own timestamp and expiry; it does not
invent a source fetch timestamp. Updating one section cannot renew another
section's restriction evidence.

Automatic plans require verified eligibility and required meeting information.
Unavailable requested courses are reported, not silently dropped. Manual
tentative entries remain possible but cannot make a plan verified. A plan keeps
its catalog provenance; later publication requires revalidation rather than
silently rewriting a student's timetable.

Only explicitly untimed, academically verified offerings may participate in
credit planning without meetings. An empty or unreadable schedule alone does
not establish that a course is untimed.

## Deployment checks

Run the catalog, import-worker, prerequisite, timetable, ownership and API tests
on PostgreSQL, regenerate the OpenAPI frontend contract, and run the frontend
typecheck/lint/build. Verify source-request budgets with a mocked transport;
tests must never authenticate to METU.

Before cutover, reconcile migration/backfill results and publish initial
reviewed coverage. Verify the deployed release and migration state separately
from CI and public health. Keep old tables for recovery until the published
read path has been validated. Content rollback uses a catalog release, not
restoration of a raw source bypass.
