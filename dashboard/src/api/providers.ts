/**
 * Provider availability — is each provider usable, and what the failover
 * sweep did about it (provider-failover D20).
 *
 * Every state here is server-derived.  The daemon owns the six-state reducer,
 * the override expiry, the hold kinds and the re-route counts; the dashboard
 * renders them and never recomputes one, because a browser deriving
 * "unavailable" a second time is how the banner and the doctor end up
 * disagreeing about the same outage.
 *
 * Kept in its own module rather than in ``hooks.ts`` for the same reason as
 * ``activity.ts`` and ``taskSubtasks.ts``: the banner mounts in the app shell
 * and the hold/undo strip in every task view, and dozens of tests stub
 * ``hooks.ts`` wholesale with a fixed set of hooks.  A separate module keeps
 * those stubs honest instead of forcing each one to learn about providers.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getProviderAvailabilityApiProvidersAvailabilityGet,
  postProviderRecheckApiProvidersProviderRecheckPost,
  postProviderRerouteUndoApiProvidersRerouteUndoPost,
  postProviderStateApiProvidersProviderStatePost,
  providerHeldTasks,
} from "./client";
import type {
  ProviderAvailabilityStatus,
  ProviderHeldTask,
  ProviderHeldTasksResponse,
  ProviderHoldDetail,
  ProviderOverride,
  ProviderRecheckResponse,
  ProviderRerouteUndoBody,
  ProviderRerouteUndoResponse,
  ProviderSetStateResponse,
  ProviderStateRequest,
  ProviderStatusResponse,
  TaskReroute,
} from "./client";

export type {
  ProviderAvailabilityStatus,
  ProviderHeldTask,
  ProviderHeldTasksResponse,
  ProviderHoldDetail,
  ProviderOverride,
  ProviderRecheckResponse,
  ProviderRerouteUndoResponse,
  ProviderSetStateResponse,
  ProviderStatusResponse,
  TaskReroute,
};

export const PROVIDER_AVAILABILITY_KEY = ["providers", "availability"] as const;
export const PROVIDER_HELD_TASKS_KEY = ["providers", "held-tasks"] as const;

/**
 * The text a refused provider command carries.
 *
 * The REST routes answer a refusal with ``{"error": "..."}`` and the client
 * interceptor wraps it as ``API 400: ...``; the operator wants the daemon's
 * sentence, not the transport prefix.
 */
export function providerErrorText(error: unknown): string {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (payload && typeof payload === "object" && "error" in payload) {
    const text = (payload as { error?: unknown }).error;
    if (typeof text === "string" && text) return text;
  }
  if (error instanceof Error) return error.message.replace(/^API \d{3}: /, "");
  return String(error ?? "request failed");
}

/**
 * Every tracked provider's effective state.
 *
 * Polled on a 30s cadence — the banner must appear within a sweep or two of
 * an outage even on a page that never opens the Metrics tab — and also
 * invalidated by ``provider.*`` / ``notify.provider_state`` frames on the
 * WebSocket (see ``ws/useEventStream``), so a change between halves shows at
 * once while the socket is up.
 */
export function useProviderAvailability(options?: { refetchInterval?: number; enabled?: boolean }) {
  return useQuery({
    queryKey: PROVIDER_AVAILABILITY_KEY,
    queryFn: async ({ signal }) =>
      (
        await getProviderAvailabilityApiProvidersAvailabilityGet({ signal, throwOnError: true })
      ).data as ProviderStatusResponse,
    retry: 1,
    refetchInterval: options?.refetchInterval ?? 30_000,
    enabled: options?.enabled ?? true,
  });
}

function useInvalidateProviders() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: PROVIDER_AVAILABILITY_KEY });
    void queryClient.invalidateQueries({ queryKey: PROVIDER_HELD_TASKS_KEY });
  };
}

export interface SetProviderStateInput extends ProviderStateRequest {
  provider: string;
}

/**
 * Set or clear an operator override (``aq provider set-state``, D6).
 *
 * ``state: "auto"`` clears the override; ``disabled`` needs a reason and an
 * expiry story (``for``/``until``/``no_expiry``), which the daemon enforces.
 * Settled rather than succeeded: a refused write can still race a sweep, and
 * the card must show whatever the daemon now holds.
 */
export function useSetProviderState() {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: async ({ provider, ...body }: SetProviderStateInput) =>
      (
        await postProviderStateApiProvidersProviderStatePost({
          path: { provider },
          body,
          throwOnError: true,
        })
      ).data as ProviderSetStateResponse,
    onSettled: invalidate,
  });
}

/** Run the provider's login probe now (``aq provider recheck``). */
export function useRecheckProvider() {
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: async (provider: string) =>
      (
        await postProviderRecheckApiProvidersProviderRecheckPost({
          path: { provider },
          throwOnError: true,
        })
      ).data as ProviderRecheckResponse,
    onSettled: invalidate,
  });
}

/**
 * Send re-routed tasks back to the profile they came from (D16).
 *
 * A refusal for every named task is a 400 whose ``error`` joins the per-task
 * reasons; a partial success is a 200 carrying ``refused`` beside ``undone``,
 * so the caller reads both.
 */
export function useUndoReroute() {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateProviders();
  return useMutation({
    mutationFn: async (body: ProviderRerouteUndoBody) =>
      (
        await postProviderRerouteUndoApiProvidersRerouteUndoPost({ body, throwOnError: true })
      ).data as ProviderRerouteUndoResponse,
    onSettled: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["task"] });
      void queryClient.invalidateQueries({ queryKey: ["projectGraph"] });
    },
  });
}

/**
 * The tasks currently held by an unavailable provider (``provider_held_tasks``).
 *
 * The server-derived list the Tasks tab's *held by provider* filter narrows
 * to; the browser never guesses which tasks a provider outage holds.  Only
 * fetched while the filter is on.
 */
export function useProviderHeldTasks(projectId?: string, enabled = true) {
  return useQuery({
    queryKey: [...PROVIDER_HELD_TASKS_KEY, projectId ?? "all"],
    queryFn: async () => {
      const { data } = await providerHeldTasks({
        body: projectId ? { project_id: projectId } : {},
        throwOnError: true,
      });
      const result = data as ProviderHeldTasksResponse & { error?: string };
      // Command routes may report a refusal in band with a 200.
      if (result.success === false) throw new Error(result.error || "Could not list held tasks");
      return result;
    },
    enabled,
    refetchInterval: 30_000,
  });
}
