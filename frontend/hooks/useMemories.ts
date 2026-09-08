"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { jsonFetch } from "@/lib/api/fetcher";
import type { MemoryList } from "@/lib/api/memories";

export function useMemories() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["memories"],
    queryFn: () => jsonFetch<MemoryList>("/api/memories"),
  });
  const clear = useMutation({
    mutationFn: () => jsonFetch<MemoryList>("/api/memories", { method: "DELETE" }),
    onSuccess: (value) => queryClient.setQueryData(["memories"], value),
  });

  return {
    memories: query.data?.memories ?? [],
    isLoading: query.isLoading,
    error: query.error ?? clear.error,
    clear,
  };
}
