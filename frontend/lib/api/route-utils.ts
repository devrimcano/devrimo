import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getAuth } from "@/lib/supabase/server";
import { isSupabaseConfigured } from "@/lib/env";
import { ApiError } from "@/lib/api/errors";
import { withUpstreamContext } from "@/lib/api/request-context";
import {
  distinctIdFromCookies,
  reportRequestOutcome,
  reportServerException,
  tracingHeadersFrom,
} from "@/lib/posthog-server";
import {
  OUTCOME_EXPECTED_FAILURE,
  OUTCOME_UNEXPECTED_FAILURE,
  POSTHOG_SESSION_HEADER,
  REQUEST_ID_HEADER,
  outcomeForStatus,
  requestIdFrom,
} from "@/lib/telemetry";

export type AuthSession = NonNullable<Awaited<ReturnType<typeof getAuth>>>;

/**
 * A discriminated union rather than an inferred one, so `"error" in result`
 * narrows to a defined `error` at every call site.
 */
export type RequireAuthResult =
  | { error: NextResponse; auth?: undefined }
  | { error?: undefined; auth: AuthSession };

export async function requireAuth(): Promise<RequireAuthResult> {
  if (!isSupabaseConfigured()) {
    return { error: NextResponse.json({ error: "Supabase is not configured" }, { status: 503 }) };
  }

  const auth = await getAuth();
  if (!auth?.accessToken) {
    return { error: NextResponse.json({ error: "Unauthorized" }, { status: 401 }) };
  }

  return { auth };
}

/** Correlation and identity for one route handler invocation. */
export type RouteTelemetry = {
  requestId: string;
  /**
   * Who this is. The verified Supabase user id once authenticated; before that,
   * the browser's own PostHog id, so a signed-out failure still lands on the
   * person who hit it rather than on nobody.
   */
  distinctId: string | null;
  /** The browser's replay session. Never an identity claim. */
  sessionId: string | null;
  route: string;
  method: string;
};

function telemetryFor(request: Request, route: string, distinctId: string | null): RouteTelemetry {
  return {
    requestId: requestIdFrom(request),
    distinctId,
    sessionId: request.headers.get(POSTHOG_SESSION_HEADER),
    route,
    method: request.method,
  };
}

/** Echo the correlation id so the browser can log the id the server used. */
function withRequestId(response: Response, requestId: string): Response {
  const headers = new Headers(response.headers);
  headers.set(REQUEST_ID_HEADER, requestId);
  // Reconstructed rather than mutated: a streaming response's headers are
  // immutable once it exists, and `body` passes a stream through untouched.
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export async function apiErrorResponse(error: unknown, telemetry?: RouteTelemetry) {
  const context = {
    requestId: telemetry?.requestId,
    distinctId: telemetry?.distinctId,
    sessionId: telemetry?.sessionId,
    route: telemetry?.route,
    method: telemetry?.method,
    source: "api_proxy",
  };

  if (error instanceof ApiError) {
    // 4xx from the broker is expected control flow — a missing session, a busy
    // agent — and reporting it would drown the real failures. 5xx is not.
    if (error.status >= 500) {
      await reportServerException(error, { ...context, status: error.status });
    }
    return NextResponse.json(
      { error: error.message, detail: error.body, request_id: telemetry?.requestId ?? error.requestId },
      { status: error.status },
    );
  }

  // Anything that is not an ApiError never reached the broker at all: a
  // network failure, a bug in a route handler, a malformed response.
  const message = error instanceof Error ? error.message : "Unexpected error";
  await reportServerException(error, { ...context, status: 502 });
  return NextResponse.json({ error: message, request_id: telemetry?.requestId }, { status: 502 });
}

export type AuthenticatedRouteContext = RouteTelemetry & { auth: AuthSession };

/**
 * The shape every authenticated route handler in this app shares.
 *
 * Authentication, upstream correlation, error translation and the request
 * outcome event all used to be repeated per handler — or, more often, only
 * some of them were. Wrapping the handler makes each one impossible to forget,
 * and gives the browser, this app and the broker the same correlation id
 * without a single call site having to pass it.
 *
 * The outcome event is emitted exactly once, here, so a handler that also
 * reports a product event cannot double-count its own failure.
 */
export function authenticatedRoute<Extra extends unknown[]>(
  route: string,
  handler: (context: AuthenticatedRouteContext, request: NextRequest, ...rest: Extra) => Promise<Response> | Response,
  options: { streaming?: boolean } = {},
) {
  return async (request: NextRequest, ...rest: Extra): Promise<Response> => {
    const started = Date.now();
    const anonymousId = distinctIdFromCookies(request.headers.get("cookie") ?? undefined);
    let telemetry = telemetryFor(request, route, anonymousId);

    const report = async (status: number, extra: Record<string, unknown> = {}) => {
      await reportRequestOutcome({
        requestId: telemetry.requestId,
        distinctId: telemetry.distinctId,
        sessionId: telemetry.sessionId,
        route,
        method: telemetry.method,
        status_code: status,
        outcome: outcomeForStatus(status),
        duration_seconds: (Date.now() - started) / 1000,
        streaming: options.streaming ?? false,
        ...extra,
      });
    };

    const result = await requireAuth();
    if (result.error) {
      await report(result.error.status, { outcome: OUTCOME_EXPECTED_FAILURE });
      return withRequestId(result.error, telemetry.requestId);
    }
    // From here on the person is known for certain, which is the only
    // attribution a server-side event should ever use.
    telemetry = { ...telemetry, distinctId: result.auth.user.id };

    try {
      const response = await withUpstreamContext(
        { requestId: telemetry.requestId, forwardHeaders: tracingHeadersFrom(request, telemetry.requestId) },
        () => handler({ ...telemetry, auth: result.auth }, request, ...rest),
      );
      await report(response.status);
      return withRequestId(response, telemetry.requestId);
    } catch (error) {
      // A handler that throws instead of returning `apiErrorResponse`. Next's
      // `onRequestError` would see this too, and deduplicates against it.
      const response = await apiErrorResponse(error, telemetry);
      await report(response.status, { outcome: OUTCOME_UNEXPECTED_FAILURE });
      return withRequestId(response, telemetry.requestId);
    }
  };
}
