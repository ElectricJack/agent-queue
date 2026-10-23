/**
 * Pure formatting and state rules for the delivery latency panel
 * (``DeliveryLatency.tsx``), kept apart so the component module exports only
 * components.
 */

import type { DeliveryLatencyGeneration, DeploymentEvent } from "../../api/client";

/** A report older than this (by the server's clock) is labelled stale. */
export const STALE_AFTER_SECONDS = 180;

const DEPLOYED = new Set(["updated", "recovered"]);

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "No data";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function formatPercent(fraction: number | null | undefined): string {
  if (fraction == null || !Number.isFinite(fraction)) return "No data";
  return `${Math.round(fraction * 100)}%`;
}

export type DeliveryState =
  | "on_dev" | "current_blocker" | "pending" | "slow" | "stuck"
  | "unknown" | "historical_unknown" | "left_censored" | "excluded";

/** The one state a generation's badge shows, most urgent first. */
export function deliveryState(g: DeliveryLatencyGeneration): DeliveryState {
  if (g.status === "sample") return g.current_blocker ? "current_blocker" : "on_dev";
  if (g.status === "pending") {
    if (g.watchdog === "stuck") return "stuck";
    if (g.watchdog === "slow") return "slow";
    return "pending";
  }
  if (g.status === "left_censored") return g.current_blocker ? "current_blocker" : "left_censored";
  return g.status as DeliveryState;
}

export type DeployState = "deployed_after" | "awaiting" | "unknown" | null;

/**
 * Whether the daemon deployed after a generation first reached dev.
 *
 * Deployment history records the operator's daemon install, not any one
 * project, and names commits the history alone cannot relate to this
 * generation — so this is time order only.  ``null`` means not on dev.
 */
export function deployState(
  g: DeliveryLatencyGeneration,
  history: DeploymentEvent[] | undefined,
): DeployState {
  if (g.on_dev_at == null || g.current_blocker) return null;
  const deployed = (history ?? []).filter((e) => DEPLOYED.has(e.outcome));
  if (deployed.length === 0) return "unknown";
  const last = Math.max(...deployed.map((e) => e.timestamp));
  return last >= g.on_dev_at ? "deployed_after" : "awaiting";
}
