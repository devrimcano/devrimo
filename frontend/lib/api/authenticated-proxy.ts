import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { getApiBaseUrl } from "@/lib/env";
import { apiErrorResponse, authenticatedRoute, type AuthenticatedRouteContext } from "@/lib/api/route-utils";
import { REQUEST_ID_HEADER } from "@/lib/telemetry";
import { tracingHeadersFrom } from "@/lib/posthog-server";

export type ProxyContext = { params: Promise<{ path: string[] }> };

async function forward(
  telemetry: AuthenticatedRouteContext,
  request: NextRequest,
  context: ProxyContext,
  namespace: "admin" | "schedule" | "student",
) {
  const { path } = await context.params;
  const safePath = path.map(encodeURIComponent).join("/");
  const upstream = `${getApiBaseUrl()}/api/v1/${namespace}/${safePath}${request.nextUrl.search}`;
  const hasBody = request.method !== "GET" && request.method !== "HEAD" && request.body !== null;
  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers: {
      Accept: request.headers.get("accept") ?? "application/json",
      Authorization: `Bearer ${telemetry.auth.accessToken}`,
      ...(hasBody ? { "Content-Type": request.headers.get("content-type") ?? "application/json" } : {}),
      // This proxy bypasses `apiFetch`, so the correlation id and browser
      // session it forwards are attached explicitly. Without them the whole
      // admin, schedule and student surface reached the broker anonymous.
      ...tracingHeadersFrom(request, telemetry.requestId),
    },
    body: hasBody ? request.body : undefined,
    cache: "no-store",
  };
  if (hasBody) init.duplex = "half";
  const response = await fetch(upstream, init);

  return new NextResponse(response.body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/json",
      ...(response.headers.get("content-disposition")
        ? { "content-disposition": response.headers.get("content-disposition")! }
        : {}),
      // The broker's own id, which is the one its logs and issues carry.
      [REQUEST_ID_HEADER]: response.headers.get(REQUEST_ID_HEADER) ?? telemetry.requestId,
    },
  });
}

/**
 * The catch-all proxy for the admin, schedule and student namespaces.
 *
 * Wrapped like every other route so a broker that is unreachable — which this
 * handler reaches by raw `fetch`, with no `ApiError` in sight — becomes an
 * issue and a request outcome rather than an unexplained 500.
 */
export function proxyRoute(namespace: "admin" | "schedule" | "student") {
  return authenticatedRoute(
    `/api/${namespace}/[...path]`,
    async (telemetry, request: NextRequest, context: ProxyContext) => {
      try {
        return await forward(telemetry, request, context, namespace);
      } catch (error) {
        return apiErrorResponse(error, telemetry);
      }
    },
  );
}
