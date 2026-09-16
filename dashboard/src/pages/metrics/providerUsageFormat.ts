/**
 * Pure readers behind the provider quota cards, kept out of the component file
 * so a test can exercise the formatting rules without a render (and so React
 * Fast Refresh keeps working for the card itself) — the same split as
 * ``series.ts`` beside ``TimeSeriesChart``.
 */

import type { ProviderUsageSnapshot } from "../../api/hooks";

/** Percentages at or above these get a warmer bar. */
export const AMBER_AT = 75;
export const RED_AT = 90;

const PROVIDER_LABEL: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
};

/**
 * How a provider names its own window, title-cased and nothing more.
 *
 * Deliberately not a semantic rename: Codex calls its windows ``primary`` and
 * ``secondary`` and does not tell us how long either one is, so printing
 * "weekly" would be a guess that silently goes wrong the day the provider
 * re-scopes them.  The reset clause beside it is the honest answer to "how
 * long".
 */
export function windowLabel(window: string): string {
  if (!window) return "limit";
  return window.replace(/_/g, " ");
}

/** "Claude · week (Fable)" — the card's identity, scope included when set. */
export function seriesLabel(row: ProviderUsageSnapshot): string {
  const provider = PROVIDER_LABEL[row.provider] ?? row.provider;
  const scope = (row.scope ?? "").trim();
  const window = windowLabel(row.window);
  return scope ? `${provider} · ${window} (${scope})` : `${provider} · ${window}`;
}

/** A coarse "4h ago" — the reader wants an order of magnitude, not seconds. */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86_400)}d ago`;
}

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/**
 * "resets today 15:59" / "resets Thu 14:09" / "resets Sep 21 09:00".
 *
 * A reset already in the past is reported as such rather than as a future
 * time: the provider rolled the window over and nobody has read it since, and
 * "resets Thu 14:09" on a Friday reads as a live clock.
 */
export function formatReset(resetsAt: number | null | undefined, now: number): string | null {
  if (resetsAt == null || !Number.isFinite(resetsAt)) return null;
  const then = new Date(resetsAt * 1000);
  if (Number.isNaN(then.getTime())) return null;
  const today = new Date(now * 1000);
  const days = Math.round(
    (new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) /
      86_400_000,
  );
  if (resetsAt <= now) return `reset ${days === 0 ? "today" : DAY_NAMES[then.getDay()]} ${hhmm(then)}`;
  if (days === 0) return `resets today ${hhmm(then)}`;
  if (days === 1) return `resets tomorrow ${hhmm(then)}`;
  if (days < 7) return `resets ${DAY_NAMES[then.getDay()]} ${hhmm(then)}`;
  return `resets ${then.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${hhmm(then)}`;
}

/**
 * Bar and percentage colours for a reading.
 *
 * A stale reading is grey whatever its number: threshold colour is a claim
 * about *now*, and we do not know what now looks like.
 */
export function toneFor(percent: number, stale: boolean): { bar: string; text: string } {
  if (stale) return { bar: "bg-gray-600", text: "text-gray-400" };
  if (percent >= RED_AT) return { bar: "bg-red-500", text: "text-red-300" };
  if (percent >= AMBER_AT) return { bar: "bg-amber-500", text: "text-amber-300" };
  return { bar: "bg-indigo-400", text: "text-gray-100" };
}

/**
 * Sorted so the cards do not reshuffle between polls: the response's order is
 * whatever the newest-per-series query produced, and a card that swaps places
 * because a percentage ticked is a card you cannot glance at.
 */
export function sortSnapshots(rows: ProviderUsageSnapshot[]): ProviderUsageSnapshot[] {
  return [...rows].sort(
    (a, b) =>
      a.provider.localeCompare(b.provider) ||
      a.window.localeCompare(b.window) ||
      (a.scope ?? "").localeCompare(b.scope ?? ""),
  );
}
