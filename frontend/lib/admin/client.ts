import { jsonFetch, type JsonFetchInit } from "@/lib/api/fetcher";

const adminFetch = <T>(path: string, init?: JsonFetchInit) =>
  jsonFetch<T>(`/api/admin/${path.replace(/^\//, "")}`, init);

export function adminGet<T>(path: string) {
  return adminFetch<T>(path);
}

export function adminMutate<T>(path: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body: unknown) {
  return adminFetch<T>(path, { method, body });
}
