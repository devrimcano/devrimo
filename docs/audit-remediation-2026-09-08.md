# Audit remediation — 2026-09-08

These changes address the audit of baseline `0f4d55d`. They are implemented in
the working tree; this document does not attest to a pushed or deployed release.
PostHog privacy behavior (`PH-PRIV`) is intentionally unchanged.

| Finding | Remediation |
| --- | --- |
| SEC-1 | Lock embedding settings and require a replacement provider key when changing the endpoint origin. |
| SEC-2 | Fence academic writes against deletion; invalidate pending private-cache fills so stale responses cannot recreate erased data. |
| PH-1 | Distinguish interrupted model execution and uncertain durable finalization from successfully committed runs. |
| PH-2 | Add assistant job and worker lifecycle coverage, knowledge lifecycle events, and durable run/request/worker correlation. |
| PH-3 | Preserve tracing headers when reconnecting to persisted chat streams. |
| PH-4 | Reject malformed SSE JSON and invalid required events without recording their contents. |
| PH-5 | Emit bounded AI instrumentation availability/failure events. |
| PH-6 | Report unexpected domain-worker failures through sanitized, grouped exception issues. |
| PH-7 | Include authentication setup in BFF outcome/error reporting. |
| PostHog P3 | Supply fingerprints on the first exception capture and record correlated browser cancellation failures. |
| DB-1 | Revoke public Data API access to migration metadata and enable its RLS in revision 0030. |
| DB-2 | Bound async and Agno pools, cap actual runtime logins, and reserve server capacity; test nested campus connection use. |
| DB-3 | Exclude only named migration-owned search objects from ORM comparison and enforce a clean schema-drift gate. |
| RED-1 | Share canonical hashing and reject timetable idempotency-key collisions using persisted request digests. |
| RED-2 | Share catalog expansion, profile preparation, restriction normalization, and eligibility verdicts across HTTP and MCP. |
| RED-3 | Share snapshot freshness and refresh-failure policy, releasing transactions before upstream refreshes. |
| RED-4 | Make server-side legacy timetable conversion authoritative. |

Additional database hardening enforces production TLS, validates the configured
JWKS endpoint and JWT issuer, and supplies one-time scoped release/backup role
provisioning. The deployment dump uses a separate read-only identity. Restore
tests exercise actual PostgreSQL dumps with synthetic academic and Agno chat data
in disposable databases, including Supabase's `extensions` schema layout.

## Validation

- Full backend suite: 631 passed. The final worker error-event change was then
  verified by the 66-test worker/observability regression suite.
- Supabase extension-layout database, role, backup, and restore suite: 50 passed.
- Frontend regression tests: 16 passed; TypeScript, ESLint, generated API contract
  check, and production build passed.
- Ruff passed for all changed Python files; shell syntax and diff checks passed.

Tests used synthetic data and disposable local PostgreSQL databases. No live
PostHog test events, production data changes, or paid upgrades were performed.

## Release prerequisites

Apply migrations 0030–0032 and provision scoped release/backup credentials before
using the updated deployment script. Follow [database ownership](database-ownership.md)
for the bootstrap sequence, protected environment files, login limits, and
recovery checks. These operations have not been applied to production here.

Supabase-managed backups/PITR remain deferred at the user's request. The signed-in
Dashboard showed Free with no managed backups on 2026-09-08. Local restore tests
do not establish off-host retention or a production recovery window.
