import { getApiBaseUrl } from "@/lib/env";
import { jsonFetch } from "@/lib/api/fetcher";
import { currentUpstreamContext } from "@/lib/api/request-context";

export type ApiRequestInit = Omit<RequestInit, "body"> & {
  body?: unknown;
  token: string;
};

export async function apiFetch<T>(path: string, init: ApiRequestInit): Promise<T> {
  const { token, body, headers, ...rest } = init;
  const url = `${getApiBaseUrl()}/api/v1${path.startsWith("/") ? path : `/${path}`}`;

  return jsonFetch<T>(url, {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      Authorization: `Bearer ${token}`,
      // The correlation id and browser session of the request this call is
      // being made for, so the broker's logs, events and issues join up with
      // this app's and the browser's. An explicit header still wins.
      ...(currentUpstreamContext()?.forwardHeaders ?? {}),
      ...headers,
    },
    body,
  });
}
