/**
 * Provider-level worker allocation (provider-worker-allocation-controls spec):
 * one read, one preview and one apply over the provider's ordinary worker
 * profiles.
 *
 * Everything here is the daemon's answer.  The snapshot, the preview's
 * before/after rows, the busy set an interrupt must authorize and the preview
 * token are all server-derived; the dashboard renders them and sends back only
 * what the operator reviewed.  Apply takes the token and nothing else, so the
 * applied set is the previewed set.
 *
 * A focused module, like ``providers.ts``, so the many page tests that stub
 * ``hooks.ts`` wholesale do not have to learn about allocation; ``hooks.ts``
 * re-exports it for callers that prefer the one import.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getProviderAllocationApiProvidersAllocationGet,
  postProviderAllocationApplyApiProvidersAllocationApplyPost,
  postProviderAllocationPreviewApiProvidersAllocationPreviewPost,
} from "./client";
import type {
  ProviderAllocationApplyBody,
  ProviderAllocationApplyResponse,
  ProviderAllocationPreviewBody,
  ProviderAllocationPreviewResponse,
  ProviderAllocationStatusResponse,
} from "./client";

export type {
  ProviderAllocationApplyBody,
  ProviderAllocationApplyResponse,
  ProviderAllocationPreviewBody,
  ProviderAllocationPreviewResponse,
  ProviderAllocationStatusResponse,
};

export const PROVIDER_ALLOCATION_KEY = ["providers", "allocation"] as const;

/**
 * Every ordinary worker profile grouped by provider (``GET /api/providers/allocation``).
 *
 * ``pool.*`` and ``session.*`` frames invalidate it (``ws/useEventStream``);
 * the poll reconciles what the socket does not carry, such as a routing
 * preference change or an allocation made from the CLI.
 */
export function useProviderAllocation(options?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: PROVIDER_ALLOCATION_KEY,
    queryFn: async ({ signal }) =>
      (
        await getProviderAllocationApiProvidersAllocationGet({ signal, throwOnError: true })
      ).data as ProviderAllocationStatusResponse,
    retry: 1,
    refetchInterval: options?.refetchInterval ?? 15_000,
  });
}

/** Preview one allocation request: read-only, and the source of the token apply consumes. */
export function useProviderAllocationPreview() {
  return useMutation({
    mutationFn: async (body: ProviderAllocationPreviewBody) =>
      (
        await postProviderAllocationPreviewApiProvidersAllocationPreviewPost({
          body,
          throwOnError: true,
        })
      ).data as ProviderAllocationPreviewResponse,
  });
}

/**
 * The structured apply result a refused or failed apply carries, or null.
 *
 * ``POST /api/providers/allocation/apply`` answers a refusal (a stale token
 * with its fresh preview, a missing busy authorization) and an apply that
 * failed part-way (every profile row) with the whole result and a 4xx; the
 * client interceptor throws it with the body on ``payload``.  A plain
 * ``{"error": ...}`` refusal (out of scope) has neither ``error_code`` nor
 * ``status`` and stays an error.
 */
export function allocationApplyFailure(error: unknown): ProviderAllocationApplyResponse | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const body = payload as ProviderAllocationApplyResponse;
  if (!body.error_code && !body.status) return null;
  return { ...body, success: false };
}

/**
 * Apply a reviewed preview by its token.
 *
 * Resolves with the daemon's result whether it applied, refused or failed
 * part-way, so the caller reads ``status`` / ``error_code`` in one place and
 * can never mistake a partial apply for success; only a transport failure or
 * an unstructured refusal rejects.  Settled rather than succeeded: a refused
 * or partial apply may still have changed profiles and sessions.
 */
export function useProviderAllocationApply() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: ProviderAllocationApplyBody) => {
      try {
        return (
          await postProviderAllocationApplyApiProvidersAllocationApplyPost({
            body,
            throwOnError: true,
          })
        ).data as ProviderAllocationApplyResponse;
      } catch (error) {
        const result = allocationApplyFailure(error);
        if (result) return result;
        throw error;
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: PROVIDER_ALLOCATION_KEY });
      void queryClient.invalidateQueries({ queryKey: ["pools"] });
      void queryClient.invalidateQueries({ queryKey: ["sessions", "pool"] });
      void queryClient.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}
