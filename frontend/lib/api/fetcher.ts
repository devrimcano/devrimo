import { ApiError } from "@/lib/api/errors";
import { REQUEST_ID_HEADER, newRequestId } from "@/lib/telemetry";

/**
 * Client-side fetch for this app's own /api routes.
 *
 * Error extraction lives here for browser routes, authenticated broker calls,
 * and admin calls so every surface handles the same response shape.
 *
 * This is also where a request's correlation id is minted. Every hop from here
 * on — the Next.js route handler, the broker, the broker's logs and any
 * exception either of them raises — carries the same id, and it is attached to
 * the `ApiError` so the component that catches the failure can report it too.
 */
export type JsonFetchInit = Omit<RequestInit, "body"> & { body?: unknown };

export function apiErrorMessage(status: number, payload: unknown): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["detail", "error", "message"] as const) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value;
      if (value && typeof value === "object") {
        const nested = apiErrorMessage(status, value);
        if (nested !== `Request failed (${status})`) return nested;
      }
    }
  }
  return `Request failed (${status})`;
}

export async function apiErrorFromResponse(response: Response, requestId?: string | null): Promise<ApiError> {
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text().catch(() => "");
  // The response header wins: it is the id the server actually used, which
  // differs from the one sent when a hop had to mint its own.
  const correlation = response.headers.get(REQUEST_ID_HEADER) ?? requestId ?? null;
  return new ApiError(apiErrorMessage(response.status, payload), response.status, payload, correlation);
}

export async function jsonFetch<T>(path: string, init: JsonFetchInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const requestId = (headers as Record<string, string> | undefined)?.[REQUEST_ID_HEADER] ?? newRequestId();

  const response = await fetch(path, {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      [REQUEST_ID_HEADER]: requestId,
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return undefined as T;
  if (!response.ok) {
    throw await apiErrorFromResponse(response, requestId);
  }
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text().catch(() => "");
  return payload as T;
}
