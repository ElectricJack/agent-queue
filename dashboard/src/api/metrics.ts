/**
 * Fleet metrics history.
 *
 * History is fetched once per range change; everything after that arrives as
 * ``metrics.tick`` frames on the WebSocket and is appended client-side, so a
 * 1 Hz cadence never costs a request.  That is why there is no
 * ``refetchInterval`` here — the socket *is* the refresh.
 */

import { useQuery } from "@tanstack/react-query";
import {
  getMetricsSeriesApiMetricsSeriesGet,
  type MetricsSample,
  type MetricsSeriesResponse,
} from "./client";
import { client } from "./client";

/** The ranges the tab offers, and the window each covers in seconds. */
export const RANGES = {
  "5m": 300,
  "1h": 3600,
  "24h": 86_400,
  "7d": 604_800,
} as const;

export type RangeKey = keyof typeof RANGES;

export const metricsSeriesKey = (range: RangeKey) => ["metrics", "series", range] as const;

/**
 * Points a range keeps in memory once live ticks start appending.
 *
 * Sized to the range's own window at its served step, plus a little slack,
 * so the chart drops points off the left edge at the same rate the range
 * scrolls rather than growing without bound over a long session.
 */
export function maxPoints(range: RangeKey): number {
  const span = RANGES[range];
  // The server serves 1s up to a ~1h span and coarser beyond; either way a
  // 1 Hz live append is what has to be bounded.
  return Math.min(span, 4000) + 120;
}

export function useMetricsSeries(range: RangeKey) {
  return useQuery({
    queryKey: metricsSeriesKey(range),
    queryFn: async ({ signal }): Promise<MetricsSeriesResponse> => {
      const now = Date.now() / 1000;
      const response = await getMetricsSeriesApiMetricsSeriesGet({
        client,
        signal,
        query: { from: now - RANGES[range], to: now, step: "auto" },
        throwOnError: true,
      });
      return response.data as MetricsSeriesResponse;
    },
    // A cold daemon has no history yet; an empty series is a valid answer,
    // not a failure worth three backoff retries.
    retry: 1,
    staleTime: 30_000,
  });
}

export type { MetricsSample, MetricsSeriesResponse };
