/**
 * The vocabulary and metadata shared by every telemetry call in this app.
 *
 * Two things live here rather than in the browser or server module, because
 * both halves need them and they must agree.
 *
 * *Correlation.* One id travels with a unit of work: minted in the browser,
 * sent on the request, forwarded by whichever proxy handles it, echoed back by
 * the broker, and attached to every event, log line and exception any of the
 * three produce. Without it a browser error, a proxy exception and a backend
 * issue describing one failure are three unrelated rows.
 *
 * *Outcomes.* The same three answers everywhere. An expected failure is
 * control flow the product defines — an unauthenticated request, a busy agent.
 * An unexpected failure is a defect or a dependency that broke. One property,
 * so an error rate is a query rather than a guess.
 */

export const REQUEST_ID_HEADER = "x-request-id";
export const POSTHOG_SESSION_HEADER = "x-posthog-session-id";
export const POSTHOG_DISTINCT_ID_HEADER = "x-posthog-distinct-id";

export const OUTCOME_SUCCESS = "success";
export const OUTCOME_EXPECTED_FAILURE = "expected_failure";
export const OUTCOME_UNEXPECTED_FAILURE = "unexpected_failure";

export type Outcome =
  | typeof OUTCOME_SUCCESS
  | typeof OUTCOME_EXPECTED_FAILURE
  | typeof OUTCOME_UNEXPECTED_FAILURE;

/** The one event name every request-shaped unit of work reports under. */
export const EVENT_REQUEST_COMPLETED = "api_request_completed";

/** Bumped when the meaning of an emitted property changes. */
export const TELEMETRY_SCHEMA_VERSION = 2;

export function outcomeForStatus(status: number): Outcome {
  if (status >= 500) return OUTCOME_UNEXPECTED_FAILURE;
  if (status >= 400) return OUTCOME_EXPECTED_FAILURE;
  return OUTCOME_SUCCESS;
}

export function newRequestId(): string {
  // `randomUUID` needs a secure context, which a preview served over plain
  // HTTP is not. A correlation id that is merely unique is fine here.
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return uuid;
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** The correlation id on an incoming request, or a fresh one. */
export function requestIdFrom(request: { headers: Headers }): string {
  return request.headers.get(REQUEST_ID_HEADER)?.trim() || newRequestId();
}

/**
 * Which build this is, so a spike can be attributed to a deploy.
 *
 * `NEXT_PUBLIC_RELEASE` is set by `scripts/deploy-vps.sh` from the same commit
 * it uploads browser source maps under, so an exception and the map that
 * resolves it always name the same revision.
 */
export function getRelease(): string {
  return process.env.NEXT_PUBLIC_RELEASE?.trim() || "";
}

export function getEnvironment(): string {
  return process.env.NEXT_PUBLIC_ENVIRONMENT?.trim() || process.env.NODE_ENV || "development";
}

/** Labels every event from this app carries, whichever half emitted it. */
export function serviceProperties(service: "devrimo-web" | "devrimo-web-server"): Record<string, unknown> {
  const release = getRelease();
  return {
    service,
    environment: getEnvironment(),
    telemetry_schema_version: TELEMETRY_SCHEMA_VERSION,
    ...(release ? { release } : {}),
  };
}
