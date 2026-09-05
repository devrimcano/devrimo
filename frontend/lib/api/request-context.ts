import { AsyncLocalStorage } from "node:async_hooks";

/**
 * Request-scoped correlation for server-side calls to the broker.
 *
 * Every `lib/api/*` helper runs inside a route handler and needs to forward the
 * same three headers upstream — the correlation id, the browser's replay
 * session, and the distinct id the broker ignores. Threading them through
 * thirteen wrapper signatures would put a parameter nobody reads in front of
 * every caller, and would be forgotten exactly once, on the call that later
 * turned out to matter.
 *
 * `AsyncLocalStorage` is the request-scoped context a route handler already
 * has: the whole handler body, including everything it awaits, runs inside it.
 * This module is server-only — nothing in `app/api/**` or a server component
 * reaches the browser bundle, and `apiFetch` talks to the broker directly, so
 * it never runs there either.
 */
export type UpstreamContext = {
  requestId: string;
  /** Headers every upstream call from this request carries. */
  forwardHeaders: Record<string, string>;
};

const storage = new AsyncLocalStorage<UpstreamContext>();

export function withUpstreamContext<T>(context: UpstreamContext, run: () => T): T {
  return storage.run(context, run);
}

export function currentUpstreamContext(): UpstreamContext | null {
  return storage.getStore() ?? null;
}
