/**
 * Provider-reported account quota.
 *
 * Kept in a focused module because the usage header mounts in the app shell,
 * while many page tests replace the broad legacy ``hooks.ts`` module.  The
 * legacy module re-exports this hook for existing callers.
 */

import { useQuery } from "@tanstack/react-query";
import { getProviderUsageApiProvidersUsageGet } from "./client";
import type { ProviderUsageResponse, ProviderUsageSnapshot } from "./client";

export const PROVIDER_USAGE_KEY = ["providers", "usage"] as const;

/**
 * Each provider's newest reading per ``(provider, window, scope)`` series.
 *
 * ``stale`` and ``age_seconds`` are server-computed and used verbatim.  These
 * values are polled because they move on a probe/transcript cadence rather
 * than on every dashboard event.
 */
export function useProviderUsage(options?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: PROVIDER_USAGE_KEY,
    queryFn: async ({ signal }) =>
      (
        await getProviderUsageApiProvidersUsageGet({ signal, throwOnError: true })
      ).data as ProviderUsageResponse,
    // An install that has never probed returns an empty list, which is a
    // valid answer and not worth three backoff retries.
    retry: 1,
    refetchInterval: options?.refetchInterval ?? 60_000,
  });
}

export type { ProviderUsageResponse, ProviderUsageSnapshot };
