/**
 * History from the API, then live ticks appended off the WebSocket.
 *
 * The load is one request per range change.  After that the series grows
 * from ``metrics.tick`` frames, which is the whole point of the 1 Hz
 * cadence: refetching a second of data sixty times a minute would cost more
 * than the sampler that produced it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RANGES, maxPoints, useMetricsSeries, type MetricsSample, type RangeKey } from "../../api/metrics";
import { useRawEventSubscription } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";

/** Seconds between points at each served resolution. */
const STEP_SECONDS: Record<string, number> = { "1s": 1, "1m": 60, "1h": 3600 };

export interface MetricsFeed {
  samples: MetricsSample[];
  /** The resolution the server actually served for this range. */
  step: string;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  /** Newest sample, live or historical — what the stat tiles read. */
  latest: MetricsSample | null;
  refetch: () => void;
}

export function useMetricsFeed(range: RangeKey): MetricsFeed {
  const query = useMetricsSeries(range);
  const [live, setLive] = useState<MetricsSample[]>([]);
  const step = query.data?.step ?? "1s";

  // Every history load supersedes the ticks collected against the previous
  // one — those seconds are now in the fetched window, and keeping both
  // would double every point in the overlap.
  const loadedAt = query.dataUpdatedAt;
  useEffect(() => {
    setLive([]);
  }, [range, loadedAt]);

  // Read by the tick handler without making it a dependency: re-subscribing
  // the WebSocket listener on every append would drop frames.
  const stepRef = useRef(step);
  stepRef.current = step;
  const limitRef = useRef(maxPoints(range));
  limitRef.current = maxPoints(range);

  const onEvent = useCallback((event: NotifyEvent) => {
    if (event.event_type !== "metrics.tick") return;
    const sample = event as unknown as MetricsSample;
    if (typeof sample.ts !== "number") return;
    setLive((prev) => {
      const stride = STEP_SECONDS[stepRef.current] ?? 1;
      const last = prev[prev.length - 1];
      // On a coarse range the server serves minutes or hours; thinning the
      // live tail to the same stride keeps the line's density honest.  The
      // appended point is the latest instantaneous sample rather than a
      // bucket average — the next history load replaces it with the
      // server's roll-up.
      if (last && sample.ts - last.ts < stride) return prev;
      const next = [...prev, sample];
      return next.length > limitRef.current ? next.slice(-limitRef.current) : next;
    });
  }, []);

  useRawEventSubscription(onEvent);

  const samples = useMemo(() => {
    const history = query.data?.samples ?? [];
    const newest = history[history.length - 1];
    const cutoff = newest ? newest.ts : -Infinity;
    const tail = live.filter((sample) => sample.ts > cutoff);
    const merged = [...history, ...tail];
    const limit = maxPoints(range);
    // Trim from the left so the window scrolls rather than growing forever.
    const trimmed = merged.length > limit ? merged.slice(-limit) : merged;
    const floor = (trimmed[trimmed.length - 1]?.ts ?? 0) - RANGES[range];
    return trimmed.filter((sample) => sample.ts >= floor);
  }, [query.data, live, range]);

  return {
    samples,
    step,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    latest: samples[samples.length - 1] ?? null,
    refetch: query.refetch,
  };
}
