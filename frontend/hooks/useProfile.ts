"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Profile, ProfileInput } from "@/lib/types";
import { jsonFetch } from "@/lib/api/fetcher";

async function fetchProfile() {
  return jsonFetch<Profile>("/api/profile");
}

export function useProfile() {
  const queryClient = useQueryClient();

  // A profile changes when the student edits it, and this hook is told about
  // that directly by the mutation below. The global ten-second default meant
  // every window focus refetched it: measured at roughly one fetch per eleven
  // seconds across a session, for a row that had not changed since sign-in.
  const query = useQuery({ queryKey: ["profile"], queryFn: fetchProfile, staleTime: 5 * 60_000 });

  const update = useMutation({
    mutationFn: (input: ProfileInput) =>
      jsonFetch<Profile>("/api/profile", { method: "PATCH", body: input }),
    onSuccess: (profile) => queryClient.setQueryData(["profile"], profile),
  });

  return {
    profile: query.data ?? null,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    queryError: query.error,
    error: query.error ?? update.error,
    refetch: query.refetch,
    update,
  };
}
