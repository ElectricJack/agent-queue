import { describe, expect, it } from "vitest";
import {
  BOUNDS_MS, countOver, histPercentile, isHist, mergeHists, percentile, pickHist, type Hist,
} from "../histogram";

function hist(...values: number[]): Hist {
  const h: Hist = {
    kind: "hist", counts: new Array(BOUNDS_MS.length + 1).fill(0), count: 0, sum: 0, max: 0,
  };
  for (const value of values) {
    const index = BOUNDS_MS.findIndex((bound) => value <= bound);
    const bucket = index < 0 ? BOUNDS_MS.length : index;
    h.counts[bucket] = h.counts[bucket]! + 1;
    h.count += 1;
    h.sum += value;
    h.max = Math.max(h.max, value);
  }
  return h;
}

describe("stored histograms", () => {
  it("uses the Python millisecond bucket edges", () => {
    expect(BOUNDS_MS).toEqual([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]);
  });

  it("interpolates percentiles within a bucket, including the open bucket", () => {
    const h = hist(...new Array(95).fill(15), ...new Array(5).fill(1500));
    expect(percentile(h, 0.95)).toBe(20);
    expect(percentile(h, 0.99)).toBe(1800);
    expect(percentile(hist(20000), 0.5)).toBe(15000);
    expect(percentile(hist(0), 0.5)).toBe(0.5);
    expect(percentile(hist(15), 0)).toBe(10);
    expect(percentile(hist(15), 1)).toBe(20);
    expect(percentile(hist(15), -1)).toBe(10);
    expect(percentile(hist(15), 2)).toBe(15);
  });

  it("returns gaps for absent, invalid or empty histograms", () => {
    expect(percentile(hist(), 0.5)).toBeNull();
    expect(percentile(null, 0.5)).toBeNull();
    expect(percentile(undefined, 0.5)).toBeNull();
    expect(isHist({ kind: "hist", counts: [] })).toBe(false);
    expect(isHist(null)).toBe(false);
    expect(histPercentile({ perf: { loop: { drift: hist() } } }, "perf.loop.drift", 0.95))
      .toBeNull();
  });

  it("adds buckets, counts and sums without averaging percentiles or mutating inputs", () => {
    const a = hist(3, 30);
    const b = hist(700);
    const before = structuredClone(a);
    const merged = mergeHists([a, b, { kind: "nope" }, null]);
    expect(merged.count).toBe(3);
    expect(merged.sum).toBe(733);
    expect(merged.max).toBe(700);
    expect(merged.counts.reduce((sum, count) => sum + count, 0)).toBe(3);
    expect(percentile(merged, 0.95)).toBeCloseTo(925);
    expect(isHist(merged)).toBe(true);
    expect(a).toEqual(before);
    expect(mergeHists([])).toEqual(hist());
  });

  it("counts strictly above a bucket edge and reads nested histograms", () => {
    expect(countOver(hist(499, 500, 501, 900, 20000), 500)).toBe(3);
    expect(countOver(null, 500)).toBe(0);
    const h = hist(40, 60);
    const sample = { perf: { loop: { drift: h } } };
    expect(pickHist(sample, "perf.loop.drift")).toBe(h);
    expect(histPercentile(sample, "perf.loop.drift", 0.5)).toBe(50);
    expect(histPercentile(sample, "perf.loop.missing", 0.5)).toBeNull();
    expect(pickHist({ perf: null }, "perf.loop.drift")).toBeNull();
  });
});
