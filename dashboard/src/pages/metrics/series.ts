/**
 * What the Metrics tab graphs, and how each line is read out of a sample.
 *
 * Kept apart from the page so the chart definitions can be unit-tested
 * against a fixture without mounting uPlot, and so adding a series is a
 * one-line change rather than a component edit.
 */

import type { Series } from "./chartData";
import type { MetricsSample } from "../../api/metrics";
import { histPercentile, pickHist } from "./histogram";

type Sample = Record<string, unknown>;

/** Nested lookup that treats every missing hop as "no reading". */
export function pick(sample: Sample, path: string): number | null {
  let cursor: unknown = sample;
  for (const part of path.split(".")) {
    if (cursor == null || typeof cursor !== "object") return null;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  return typeof cursor === "number" && Number.isFinite(cursor) ? cursor : null;
}

/**
 * Categorical palette. Deliberately short and fixed: these lines are read
 * side by side, and a generated hue ramp makes "claude" a different colour
 * on every reload.
 */
export const PALETTE = [
  "#818cf8", // indigo
  "#34d399", // emerald
  "#fbbf24", // amber
  "#f87171", // red
  "#60a5fa", // blue
  "#c084fc", // purple
  "#2dd4bf", // teal
  "#fb923c", // orange
] as const;

/** Stable colour per key, so a series keeps its colour across renders. */
export function colorFor(index: number): string {
  return PALETTE[index % PALETTE.length] as string;
}

/**
 * Every key seen under *path* across the whole window.
 *
 * Breakdowns (per harness, per model, per profile) are open sets — a harness
 * that only ran for the first ten seconds of the range still deserves a line
 * for those ten seconds, so the union is taken over all samples rather than
 * read off the newest one.
 */
export function breakdownKeys(samples: Sample[], path: string): string[] {
  const keys = new Set<string>();
  for (const sample of samples) {
    let cursor: unknown = sample;
    for (const part of path.split(".")) {
      if (cursor == null || typeof cursor !== "object") {
        cursor = null;
        break;
      }
      cursor = (cursor as Record<string, unknown>)[part];
    }
    if (cursor && typeof cursor === "object") {
      for (const key of Object.keys(cursor as Record<string, unknown>)) keys.add(key);
    }
  }
  return [...keys].sort();
}

function fixed(defs: Array<[string, string, string]>): Series[] {
  return defs.map(([key, label, color]) => ({
    key,
    label,
    color,
    value: (sample: Sample) => pick(sample, key),
  }));
}

function p(path: string, q: number, label: string, color: string): Series {
  return {
    key: `${path}.p${q * 100}`, label, color,
    value: (sample) => histPercentile(sample, path, q),
  };
}

/** Probe shutdown and unavailable readings are gaps, including on gauge lines. */
function perfSeries(series: Series[]): Series[] {
  return series.map((definition) => ({
    ...definition,
    value: (sample) => {
      const perf = sample.perf as { enabled?: boolean } | undefined;
      return perf?.enabled === false ? null : definition.value(sample);
    },
  }));
}

function breakdown(samples: Sample[], path: string, offset = 0): Series[] {
  return breakdownKeys(samples, path).map((name, index) => ({
    key: `${path}.${name}`,
    label: name,
    color: colorFor(offset + index),
    value: (sample: Sample) => pick(sample, `${path}.${name}`),
  }));
}

export interface ChartDef {
  id: string;
  title: string;
  unit: string;
  series: Series[];
}

/**
 * Build the chart list for a window of samples.
 *
 * Breakdown series depend on the data (which harnesses ran, which models
 * billed), so this is a function of the samples rather than a constant.
 */
export function buildCharts(samples: MetricsSample[]): ChartDef[] {
  const rows = samples as unknown as Sample[];
  return [
    {
      id: "agents",
      title: "Running agents",
      unit: "sessions",
      series: [
        ...fixed([["agents.total", "total", PALETTE[0]]]),
        ...breakdown(rows, "agents.by_harness", 1),
        ...breakdown(rows, "agents.by_profile", 3),
      ],
    },
    {
      // Spawn rate first: on a pool fleet the sessions that start children
      // are shorter-lived than the children, so "open right now" sits at ~0
      // while the event table fills up. `active` stays on the chart as the
      // secondary reading, not the headline.
      id: "subagents",
      title: "Sub-agents started per hour",
      unit: "children / hour · open now",
      series: fixed([
        ["subagents.spawned_per_hour", "started / hour", PALETTE[0]],
        ["subagents.active", "open now", PALETTE[1]],
        ["subagents.native", "open — native", PALETTE[2]],
        ["subagents.aq", "open — AQ-delegated", PALETTE[4]],
      ]),
    },
    {
      id: "tokens",
      title: "Tokens per minute",
      unit: "tokens / min",
      series: fixed([
        // `total` includes cache. Plotting input+output as the total is what
        // made this chart look dead: on a warm context the cache read is
        // three orders of magnitude larger than fresh input.
        ["tokens.total_per_min", "total", PALETTE[0]],
        ["tokens.cache_read_per_min", "cache read", PALETTE[6]],
        ["tokens.cache_write_per_min", "cache write", PALETTE[5]],
        ["tokens.input_per_min", "input", PALETTE[1]],
        ["tokens.output_per_min", "output", PALETTE[2]],
        ["tokens.unattributed_per_min", "unattributed", PALETTE[3]],
      ]),
    },
    {
      id: "tokens-by-model",
      title: "Tokens per minute by model",
      unit: "tokens / min",
      series: breakdown(rows, "tokens.by_model", 0).map((series) => ({
        ...series,
        // The per-model buckets are objects; graph the whole of each one,
        // matching the `total` line on the chart above.
        value: (sample: Sample) => pick(sample, `${series.key}.total_per_min`),
      })),
    },
    {
      id: "tasks",
      title: "Tasks by status",
      unit: "tasks",
      series: fixed([
        ["tasks.READY", "READY", PALETTE[0]],
        ["tasks.IN_PROGRESS", "IN_PROGRESS", PALETTE[1]],
        ["tasks.ASSIGNED", "ASSIGNED", PALETTE[4]],
        ["tasks.PAUSED", "PAUSED", PALETTE[2]],
        ["tasks.BLOCKED", "BLOCKED", PALETTE[3]],
      ]),
    },
    {
      id: "slots",
      title: "Worktree slots and pool supply",
      unit: "slots / sessions",
      series: fixed([
        ["slots.used", "slots in use", PALETTE[0]],
        ["slots.total", "slots provisioned", PALETTE[6]],
        ["slots.cap", "cap", PALETTE[3]],
        ["agents.by_lifecycle.pool", "pool workers", PALETTE[1]],
        ["tasks.READY", "ready (demand)", PALETTE[2]],
      ]),
    },
    {
      id: "throughput",
      title: "Throughput and the stall ladder",
      unit: "per hour",
      series: fixed([
        ["throughput.completions_per_hour", "completions", PALETTE[1]],
        ["throughput.prs_per_hour", "completions with a PR", PALETTE[4]],
        ["merges_per_hour", "merges (this daemon)", PALETTE[6]],
        ["stall.nudges_per_hour", "nudges", PALETTE[2]],
        ["stall.kills_per_hour", "kills", PALETTE[3]],
      ]),
    },
    {
      id: "load",
      title: "Machine load",
      unit: "runnable processes",
      series: fixed([
        ["machine.load1", "1 min", PALETTE[0]],
        ["machine.load5", "5 min", PALETTE[1]],
        ["machine.load15", "15 min", PALETTE[2]],
        ["machine.cpu_count", "cores", PALETTE[3]],
      ]),
    },
    {
      id: "loop-lag",
      title: "Event-loop lag",
      unit: "ms",
      series: perfSeries([
        p("perf.loop.drift", 0.95, "p95", PALETTE[0]),
        {
          key: "perf.loop.drift.max", label: "max", color: PALETTE[3],
          value: (sample) => {
            const hist = pickHist(sample, "perf.loop.drift");
            return hist && hist.count > 0 ? hist.max : null;
          },
        },
      ]),
    },
    {
      id: "api-latency",
      title: "API latency",
      unit: "ms",
      series: perfSeries([
        p("perf.api.all", 0.95, "all p95", PALETTE[0]),
        p("perf.api.all", 0.5, "all p50", PALETTE[1]),
        p("perf.relay.http", 0.95, "relay to daemon p95", PALETTE[2]),
      ]),
    },
    {
      id: "database",
      title: "Database",
      unit: "ms / count",
      series: perfSeries([
        p("perf.db.pool_wait", 0.95, "pool wait p95", PALETTE[0]),
        p("perf.db.query", 0.95, "query p95", PALETTE[1]),
        ...fixed([
          ["perf.db.pool.checked_out", "connections checked out", PALETTE[4]],
          ["perf.db.counters.slow_queries", "slow queries", PALETTE[3]],
        ]),
      ]),
    },
    {
      id: "host-pressure",
      title: "Host pressure",
      unit: "%",
      series: perfSeries(fixed([
        ["perf.host.psi.cpu.some_avg10", "CPU", PALETTE[0]],
        ["perf.host.psi.io.some_avg10", "I/O", PALETTE[2]],
        ["perf.host.psi.memory.some_avg10", "memory", PALETTE[3]],
      ])),
    },
    {
      id: "test-load",
      title: "Test slots and ungated load",
      unit: "count",
      series: perfSeries(fixed([
        ["perf.host.test_slots.used", "used", PALETTE[0]],
        ["perf.host.test_slots.total", "total", PALETTE[6]],
        ["perf.host.test_slots.waiting", "waiting", PALETTE[2]],
        ["perf.host.test_slots.orphaned", "orphaned", PALETTE[3]],
        ["perf.host.ungated.unattributed", "unattributed pytest processes", PALETTE[4]],
      ])),
    },
    {
      id: "memory",
      title: "Memory",
      unit: "MB",
      series: fixed([
        ["machine.mem_available_mb", "available", PALETTE[1]],
        ["machine.mem_free_mb", "free", PALETTE[0]],
        ["machine.mem_total_mb", "total", PALETTE[6]],
      ]),
    },
    {
      id: "daemon",
      title: "Daemon uptime and sampler cost",
      unit: "minutes / ms",
      series: [
        {
          key: "daemon.uptime_minutes",
          label: "uptime (min)",
          color: PALETTE[0],
          value: (sample: Sample) => {
            const seconds = pick(sample, "daemon.uptime_seconds");
            return seconds == null ? null : seconds / 60;
          },
        },
        ...fixed([
          ["daemon.restarts", "restarts", PALETTE[3]],
          ["sampler.collect_ms", "sampler tick (ms)", PALETTE[2]],
        ]),
      ],
    },
  ];
}
