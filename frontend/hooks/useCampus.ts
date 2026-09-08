"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { jsonFetch } from "@/lib/api/fetcher";
import type {
  CampusConnection,
  CampusConnectionInput,
  CampusVerifyResult,
} from "@/lib/types";

async function fetchConnection() {
  return jsonFetch<CampusConnection>("/api/campus/connection");
}

export function useCampus() {
  const queryClient = useQueryClient();

  const query = useQuery({ queryKey: ["campus"], queryFn: fetchConnection });

  const setConnection = (connection: CampusConnection) => {
    queryClient.setQueryData(["campus"], connection);
    // Saving a connection rebuilds the agent container, so its status is stale.
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  };

  const connect = useMutation({
    mutationFn: (input: CampusConnectionInput) =>
      jsonFetch<CampusConnection>("/api/campus/connection", { method: "PUT", body: input }),
    onSuccess: setConnection,
  });

  const disconnect = useMutation({
    mutationFn: () =>
      jsonFetch<CampusConnection>("/api/campus/connection", { method: "DELETE" }),
    onSuccess: setConnection,
  });

  const apply = useMutation({
    mutationFn: () => jsonFetch<CampusConnection>("/api/campus/apply", { method: "POST" }),
    onSuccess: setConnection,
  });

  /**
   * Checks credentials without saving them. Deliberately not a mutation with
   * cached state — the answer is only meaningful for the exact values that
   * were submitted.
   */
  const verify = useMutation({
    mutationFn: (input: { metu_username: string; metu_password: string }) =>
      jsonFetch<CampusVerifyResult>("/api/campus/connection/verify", {
        method: "POST",
        body: input,
      }),
  });

  return {
    connection: query.data ?? null,
    tools: query.data?.tools ?? [],
    isLoading: query.isLoading,
    error: query.error ?? connect.error ?? disconnect.error ?? apply.error,
    refetch: query.refetch,
    connect,
    disconnect,
    apply,
    verify,
  };
}
