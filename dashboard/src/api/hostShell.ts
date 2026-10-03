import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  hostShellClose,
  hostShellList,
  hostShellOpen,
  type HostShellListResponse,
  type HostShellOpenResponse,
} from "./client";

const KEY = ["host-shell"] as const;

/** Operator host shells: plain login shells on the AQ machine, never agents. */
export function useHostShells() {
  return useQuery({
    queryKey: KEY,
    queryFn: async () => (await hostShellList({ throwOnError: true })).data as HostShellListResponse,
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useOpenHostShell() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => (await hostShellOpen({ throwOnError: true })).data as HostShellOpenResponse,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}

export function useCloseHostShell() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => (await hostShellClose({ path: { name }, throwOnError: true })).data,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}
