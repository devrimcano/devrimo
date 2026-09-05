/**
 * Server-side error capture for the Next.js layer.
 *
 * `onRequestError` fires for every route handler, server component and RSC
 * render that throws — all 17 handlers under `app/api/**` included — so this
 * one hook is the difference between a 500 that is investigable and one that
 * only ever existed in a container log.
 *
 * It is a *fallback*, not the primary reporter. `authenticatedRoute` reports
 * from inside the request, where the verified user id and the correlation id
 * are still known; by the time an error surfaces here, identity is back to a
 * cookie. The central reporter deduplicates by error instance, so one failure
 * stays one issue with the better context of the two.
 */

import type { Instrumentation } from "next";

export function register() {
  // Initialisation happens lazily in `lib/posthog-server.ts`; nothing to do
  // here, but Next expects this export alongside onRequestError.
}

export const onRequestError: Instrumentation.onRequestError = async (error, request, context) => {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  try {
    const { distinctIdFromCookies, reportServerException } = await import("@/lib/posthog-server");
    const { REQUEST_ID_HEADER, POSTHOG_SESSION_HEADER } = await import("@/lib/telemetry");

    // React can replace the thrown instance during Server Component rendering,
    // in which case `digest` is the only stable handle on the real error.
    const digest =
      typeof error === "object" && error !== null && "digest" in error ? String(error.digest) : undefined;

    const header = (name: string) => {
      const value = request.headers?.[name];
      return (Array.isArray(value) ? value[0] : value) ?? null;
    };

    // Awaited deliberately: the handler's lifetime is the only window in which
    // this is guaranteed to send. See the Next.js instrumentation guidance.
    await reportServerException(error, {
      requestId: header(REQUEST_ID_HEADER),
      distinctId: distinctIdFromCookies(request.headers.cookie),
      sessionId: header(POSTHOG_SESSION_HEADER),
      source: "next_request",
      path: request.path,
      method: request.method,
      digest,
      router_kind: context.routerKind,
      route: context.routePath,
      route_type: context.routeType,
    });
  } catch {
    // An error reporter that throws would turn one 500 into two.
  }
};
