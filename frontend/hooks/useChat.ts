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

export async function loadSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  const data = await jsonFetch<{ messages: ChatMessage[] }>(`/api/sessions/${sessionId}`);
  return data.messages ?? [];
}
