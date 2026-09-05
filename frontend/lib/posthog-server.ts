import { PostHog } from "posthog-node";
import { getPostHogHost, getPostHogKey } from "@/lib/env";
import {
  EVENT_REQUEST_COMPLETED,
  OUTCOME_SUCCESS,
  POSTHOG_DISTINCT_ID_HEADER,
  POSTHOG_SESSION_HEADER,
  REQUEST_ID_HEADER,
  serviceProperties,
} from "@/lib/telemetry";

let client: PostHog | null = null;
let warned = false;

/**
 * How long a report may hold a request open.
 *
 * Next.js server functions are short-lived, so a report that is not flushed
 * before the function is torn down never happens — but a flush that hangs on an
 * unreachable PostHog would hold a student's request open for the SDK's own
 * timeout. Bounded, and the bound is short: losing one report is much cheaper
 * than a stalled response.
 */
const FLUSH_TIMEOUT_MS = 2000;

/**
 * The server-side PostHog client for route handlers and `instrumentation.ts`.
 *
 * Returns `null` when unconfigured so every call site is a safe optional call.
 * Events are flushed immediately rather than batched — a batched exception
 * report that never flushes is the same as no error tracking.
 */
export function getPostHogServer(): PostHog | null {
  const key = getPostHogKey();
  if (!key) {
    if (!warned && process.env.NODE_ENV !== "production") {
      warned = true;
      console.error(
        "NEXT_PUBLIC_POSTHOG_KEY variable required by PostHog is missing or un-configured, " +
          "this causes events to be silently missed. This error stops appearing once " +
          "NEXT_PUBLIC_POSTHOG_KEY is configured",
      );
    }
    return null;
  }

  if (!client) {
    client = new PostHog(key, {
      host: getPostHogHost(),
      flushAt: 1,
      flushInterval: 0,
    });
  }
  return client;
}

/** Correlation and identity for one server-side unit of work. */
export type ServerTelemetryContext = {
  requestId?: string | null;
  /** The *verified* Supabase user id. Never a client-supplied header. */
  distinctId?: string | null;
  /** The browser's replay session, which is not an identity claim. */
  sessionId?: string | null;
  [key: string]: unknown;
};

// The same error can reach both a route's catch block and `onRequestError`.
// One failure should be one issue, carrying the context of whichever layer was
// closest to it — which is the one that reports first.
const reported = new WeakSet<object>();

function withTimeout(promise: Promise<unknown>): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, FLUSH_TIMEOUT_MS);
    promise
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(timer);
        resolve();
      });
  });
}

// Identity fields have their own slots on the PostHog call and must not be
// duplicated into the event's properties.
const IDENTITY_KEYS = new Set(["requestId", "distinctId", "sessionId"]);

function eventProperties(context: ServerTelemetryContext) {
  const properties: Record<string, unknown> = {
    ...serviceProperties("devrimo-web-server"),
    ...(context.requestId ? { request_id: context.requestId } : {}),
    // Links a server-side report to the recording of the browser that caused it.
    ...(context.sessionId ? { $session_id: context.sessionId } : {}),
  };
  for (const [key, value] of Object.entries(context)) {
    if (IDENTITY_KEYS.has(key) || value === undefined) continue;
    properties[key] = value;
  }
  return properties;
}

/**
 * Report a server-side exception, once, with bounded awaited delivery.
 *
 * Callers must await this. `onRequestError` and route handlers are the only
 * windows in which the send is guaranteed to happen, and an unawaited flush in
 * a serverless-style runtime is a report that may simply never leave.
 */
export async function reportServerException(
  error: unknown,
  context: ServerTelemetryContext = {},
): Promise<boolean> {
  const posthog = getPostHogServer();
  if (!posthog) return false;

  const exception = error instanceof Error ? error : new Error(String(error));
  if (typeof error === "object" && error !== null) {
    if (reported.has(error)) return false;
    reported.add(error);
  }

  try {
    posthog.captureException(exception, context.distinctId ?? undefined, eventProperties(context));
    await withTimeout(posthog.flush());
    return true;
  } catch {
    // An error reporter that throws would turn one failure into two.
    return false;
  }
}

/**
 * Capture a server-side event, with the same guards and the same bound.
 *
 * `flush` is opt-out for a reason. The client sends each event as it is
 * captured, so awaiting the flush buys certainty rather than delivery — and it
 * buys it with a network round trip on the critical path of every request. It
 * is worth paying for a failure, which is rare and must not be lost, and not
 * worth paying on every successful request in the app.
 */
export async function reportServerEvent(
  event: string,
  context: ServerTelemetryContext = {},
  { flush = true }: { flush?: boolean } = {},
): Promise<void> {
  const posthog = getPostHogServer();
  if (!posthog) return;
  try {
    posthog.capture({
      distinctId: context.distinctId ?? context.requestId ?? "anonymous",
      event,
      properties: eventProperties(context),
    });
    if (flush) await withTimeout(posthog.flush());
  } catch {
    // Telemetry must never break a response.
  }
}

/** The outcome of one proxied request, on the shared event name. */
export async function reportRequestOutcome(context: ServerTelemetryContext): Promise<void> {
  await reportServerEvent(EVENT_REQUEST_COMPLETED, context, {
    flush: context.outcome !== OUTCOME_SUCCESS,
  });
}

const POSTHOG_COOKIE = /ph_phc_.*?_posthog=([^;]+)/;

/**
 * The browser's distinct id, read from PostHog's own cookie.
 *
 * Used only to attribute events from *signed-out* visitors, so an anonymous
 * failure still lands on the same person as their browser events. Once a
 * request is authenticated the verified Supabase user id is used instead, and
 * this is never an authorization signal in either case.
 */
export function distinctIdFromCookies(cookieHeader: string | string[] | undefined): string | null {
  if (!cookieHeader) return null;
  const cookies = Array.isArray(cookieHeader) ? cookieHeader.join("; ") : cookieHeader;
  const match = cookies.match(POSTHOG_COOKIE);
  if (!match?.[1]) return null;
  try {
    return (JSON.parse(decodeURIComponent(match[1])) as { distinct_id?: string }).distinct_id ?? null;
  } catch {
    return null;
  }
}

/**
 * The headers a proxy hop forwards upstream.
 *
 * The browser's replay session and the correlation id both continue to the
 * broker, which is what lets one failure be followed from the recording, to
 * this app's logs, to a backend issue. The distinct id is forwarded too and the
 * broker deliberately ignores it: identity there comes from the verified JWT.
 */
export function tracingHeadersFrom(request: Request, requestId?: string): Record<string, string> {
  const forwarded: Record<string, string> = {};
  for (const name of [POSTHOG_DISTINCT_ID_HEADER, POSTHOG_SESSION_HEADER]) {
    const value = request.headers.get(name);
    if (value) forwarded[name] = value;
  }
  const correlation = requestId ?? request.headers.get(REQUEST_ID_HEADER);
  if (correlation) forwarded[REQUEST_ID_HEADER] = correlation;
  return forwarded;
}
