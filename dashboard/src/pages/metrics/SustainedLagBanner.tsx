import { useMemo } from "react";
import type { MetricsSample } from "../../api/metrics";
import { isHist, mergeHists, percentile } from "./histogram";
import { pick } from "./series";

export interface LagVerdict {
  p95: number;
  since: number;
  samples: number;
  candidates: string[];
}

/** Mirror of src/doctor/perf_checks.py over the samples already on screen. */
// Public pure reader is also used without rendering the banner.
// eslint-disable-next-line react-refresh/only-export-components
export function sustainedLag(samples: MetricsSample[], windowSeconds = 180): LagVerdict | null {
  const newest = samples[samples.length - 1];
  if (!newest || newest.perf?.enabled === false) return null;
  const rows = samples.filter((row) => row.ts >= newest.ts - windowSeconds);
  const loopRows = rows.filter((row) => {
    const drift = row.perf?.loop?.drift;
    return isHist(drift) && drift.count > 0;
  });
  if (loopRows.length < 120) return null;
  const p95 = percentile(mergeHists(loopRows.map((row) => row.perf?.loop?.drift)), 0.95);
  if (p95 == null || p95 <= 500) return null;

  const candidates: string[] = [];
  for (const metric of ["pool_wait", "query"] as const) {
    const value = percentile(mergeHists(rows.map((row) => row.perf?.db?.[metric])), 0.95);
    if (value != null && value > 100) candidates.push(metric === "query" ? "db_query" : "db_pool_wait");
  }
  for (const [resource, threshold] of [["memory", 5], ["io", 20], ["cpu", 20]] as const) {
    for (let i = rows.length - 1; i >= 0; i--) {
      const value = pick(rows[i]!, `perf.host.psi.${resource}.some_avg10`);
      if (value == null) continue;
      if (value >= threshold) candidates.push(`host_${resource}_pressure`);
      break;
    }
  }

  // Index routes directly: route-template labels may themselves contain dots.
  const routes = new Map<string, unknown[]>();
  for (const row of rows) {
    for (const [label, entry] of Object.entries(row.perf?.api?.routes ?? {})) {
      if (!isHist(entry.latency)) continue;
      const hists = routes.get(label) ?? [];
      hists.push(entry.latency);
      routes.set(label, hists);
    }
  }
  let slowest: { label: string; p95: number } | null = null;
  for (const [label, hists] of routes) {
    const merged = mergeHists(hists);
    if (merged.count < 10) continue;
    const value = percentile(merged, 0.95);
    if (value != null && (slowest == null || value > slowest.p95)) slowest = { label, p95: value };
  }
  if (slowest) candidates.push(`api_route:${slowest.label}`);
  if (candidates.length === 0) candidates.push("synchronous_python");
  return { p95, since: loopRows[0]!.ts, samples: loopRows.length, candidates };
}

export default function SustainedLagBanner({ samples }: { samples: MetricsSample[] }) {
  const verdict = useMemo(() => sustainedLag(samples), [samples]);
  if (!verdict) return null;
  const since = new Date(verdict.since * 1000).toISOString().slice(11, 19) + "Z";
  return (
    <div
      role="status"
      aria-label="Event-loop lag"
      className="rounded-lg border border-amber-900/60 bg-amber-950/40 p-3 text-sm text-amber-100"
    >
      Event-loop lag p95 {verdict.p95.toFixed(0)} ms sustained since {since} — candidate causes,
      not a diagnosis: {verdict.candidates.join(", ")}
    </div>
  );
}
