/**
 * What the Metrics tab graphs, and how each line is read out of a sample.
 *
 * Kept apart from the page so the chart definitions can be unit-tested
 * against a fixture without mounting uPlot, and so adding a series is a
 * one-line change rather than a component edit.
 */

import type { Series } from "./chartData";
import type { MetricsSample } from "../../api/metrics";

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
      id: "subagents",
      title: "Sub-agents in flight",
      unit: "children",
      series: fixed([
        ["subagents.total", "total", PALETTE[0]],
        ["subagents.native", "native", PALETTE[1]],
        ["subagents.aq", "AQ-delegated", PALETTE[2]],
      ]),
    },
    {
      id: "tokens",
      title: "Tokens per minute",
      unit: "tokens / min",
      series: [
        ...fixed([
          ["tokens.total_per_min", "total", PALETTE[0]],
          ["tokens.input_per_min", "input", PALETTE[1]],
          ["tokens.output_per_min", "output", PALETTE[2]],
          ["tokens.unattributed_per_min", "unattributed", PALETTE[3]],
        ]),
        ...breakdown(rows, "tokens.by_model", 4).map((series) => ({
          ...series,
          // The per-model buckets are objects; graph their input rate.
          value: (sample: Sample) => pick(sample, `${series.key}.input_per_min`),
          label: `${series.label} in`,
        })),
      ],
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
