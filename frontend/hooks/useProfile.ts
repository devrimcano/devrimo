"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Profile, ProfileInput } from "@/lib/types";
import { jsonFetch } from "@/lib/api/fetcher";

async function fetchProfile() {
  return jsonFetch<Profile>("/api/profile");
}

export function useProfile() {
  const queryClient = useQueryClient();

  const query = useQuery({ queryKey: ["profile"], queryFn: fetchProfile });

  const update = useMutation({
    mutationFn: (input: ProfileInput) =>
      jsonFetch<Profile>("/api/profile", { method: "PATCH", body: input }),
    onSuccess: (profile) => queryClient.setQueryData(["profile"], profile),
  });

  return {
    profile: query.data ?? null,
    isLoading: query.isLoading,
    error: query.error ?? update.error,
    refetch: query.refetch,
    update,
  };
}
