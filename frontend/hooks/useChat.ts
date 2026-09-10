"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { jsonFetch } from "@/lib/api/fetcher";
import type { ChatMessage, ChatSession } from "@/lib/types";

export function useChatSessions() {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["chat-sessions"],
    queryFn: () => jsonFetch<{ sessions: ChatSession[] }>("/api/sessions").then((data) => data.sessions),
  });

  const remove = useMutation({
    mutationFn: (sessionId: string) => jsonFetch<void>(`/api/sessions/${sessionId}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["chat-sessions"] }),
  });

  const removeAll = useMutation({
    mutationFn: () => jsonFetch<{ deleted: number }>("/api/sessions", { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["chat-sessions"] }),
  });

  return {
    sessions: query.data ?? [],
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    error: query.error,
    refetch: query.refetch,
    remove,
    removeAll,
  };
}

/**
 * A conversation fetched before it is asked for.
 *
 * Measured on the live app: opening a chat costs about 530ms of server time and
 * ~50ms of network. The database is in Frankfurt and a query round trip is
 * ~40ms, so a request that touches it a dozen times is most of a second before
 * a single word appears. The second click is instant, because by then it is
 * already in hand.
 *
 * Hovering a row is a reliable signal that the row is about to be clicked, and
 * the fetch has nothing else to wait for. Started on pointer enter, the request
 * usually finishes before the button is released.
 *
 * Consume-once, deliberately. A transcript that stays cached goes stale the
 * moment a new turn is added to it, so an entry is removed as soon as it is
 * read; revisiting a conversation fetches it again, which is correct. The
 * timeout covers a hover that never became a click.
 */
const PREFETCH_TTL_MS = 30_000;
const prefetched = new Map<string, { promise: Promise<ChatMessage[]>; expires: number }>();

function fetchSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  return jsonFetch<{ messages: ChatMessage[] }>(`/api/sessions/${sessionId}`).then((data) => data.messages ?? []);
}

export function prefetchSessionMessages(sessionId: string): void {
  const now = Date.now();
  for (const [key, entry] of prefetched) if (entry.expires <= now) prefetched.delete(key);
  if (!sessionId || prefetched.has(sessionId)) return;
  // Errors are swallowed here on purpose: a hover must never surface a failure
  // the student did not ask for. The real load below fetches again and reports.
  const promise = fetchSessionMessages(sessionId).catch(() => {
    prefetched.delete(sessionId);
    return [] as ChatMessage[];
  });
  prefetched.set(sessionId, { promise, expires: now + PREFETCH_TTL_MS });
}

export async function loadSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  const warm = prefetched.get(sessionId);
  prefetched.delete(sessionId);
  if (warm && warm.expires > Date.now()) {
    const messages = await warm.promise;
    // An empty result may be a swallowed prefetch failure rather than an empty
    // conversation, so it is never trusted in place of a real read.
    if (messages.length) return messages;
  }
  return fetchSessionMessages(sessionId);
}
