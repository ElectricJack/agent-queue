/** Mirror of src/metrics/histogram.py: fixed millisecond bounds, additive merge. */
export const BOUNDS_MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000] as const;

export interface Hist {
  kind: "hist";
  counts: number[];
  count: number;
  sum: number;
  max: number;
}

export function isHist(value: unknown): value is Hist {
  const h = value as Hist | null;
  return !!h && typeof h === "object" && h.kind === "hist" && Array.isArray(h.counts)
    && h.counts.length === BOUNDS_MS.length + 1;
}

export function mergeHists(values: Iterable<unknown>): Hist {
  const out: Hist = {
    kind: "hist", counts: new Array(BOUNDS_MS.length + 1).fill(0), count: 0, sum: 0, max: 0,
  };
  for (const value of values) {
    if (!isHist(value)) continue;
    value.counts.forEach((count, i) => { out.counts[i] = out.counts[i]! + count; });
    out.count += value.count ?? 0;
    out.sum += value.sum ?? 0;
    out.max = Math.max(out.max, value.max || 0);
  }
  return out;
}

export function percentile(hist: Hist | null | undefined, q: number): number | null {
  if (!isHist(hist) || hist.count <= 0) return null;
  const target = q * hist.count;
  let seen = 0;
  for (let i = 0; i < hist.counts.length; i++) {
    const count = hist.counts[i]!;
    if (count <= 0) continue;
    if (seen + count >= target) {
      const lower = i > 0 ? BOUNDS_MS[i - 1]! : 0;
      const upper = i < BOUNDS_MS.length ? BOUNDS_MS[i]! : Math.max(hist.max, lower);
      const fraction = Math.min(1, Math.max(0, (target - seen) / count));
      return lower + (upper - lower) * fraction;
    }
    seen += count;
  }
  return hist.max;
}

/** Exact above an edge; a threshold within a bucket cannot be reconstructed. */
export function countOver(hist: Hist | null | undefined, thresholdMs: number): number {
  if (!isHist(hist)) return 0;
  return hist.counts.reduce((total, count, i) =>
    (i > 0 ? BOUNDS_MS[i - 1]! : 0) >= thresholdMs ? total + count : total, 0);
}

export function pickHist(sample: Record<string, unknown>, path: string): Hist | null {
  let cursor: unknown = sample;
  for (const part of path.split(".")) {
    if (cursor == null || typeof cursor !== "object") return null;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  return isHist(cursor) ? cursor : null;
}

export function histPercentile(sample: Record<string, unknown>, path: string, q: number): number | null {
  return percentile(pickHist(sample, path), q);
}
