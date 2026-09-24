/**
 * Pure readers behind the provider availability header, the app-wide outage
 * banner and the task hold strip (provider-failover D20).
 *
 * The rule every function here keeps: **map a server-derived value to words
 * or colour, never derive a state**.  Which half a provider is in, whether an
 * override is active, why a task is held and how many were moved all arrive
 * on the response; these helpers only decide how they read.  The one piece of
 * arithmetic is the distance from the server's own ``now`` to a server-given
 * ``until``/``since``, which is presentation, not judgement.
 */

import type { ProviderAvailabilityStatus } from "../../api/providers";

/** The two halves, as the server names them (D1). */
export const UNAVAILABLE_HALF = "unavailable";

export type StateTone = "green" | "amber" | "red" | "grey";

/**
 * Pill colour per effective state: green available, amber degraded, red for
 * the three states that stopped launches on their own, grey for ``disabled``
 * because an operator chose it — it is intentional, not an alarm.
 */
const STATE_TONE: Record<string, StateTone> = {
  available: "green",
  degraded: "amber",
  exhausted: "red",
  unauthenticated: "red",
  failing: "red",
  disabled: "grey",
};

export function stateTone(state: string): StateTone {
  return STATE_TONE[state] ?? "grey";
}

export const TONE_CLASSES: Record<StateTone, { pill: string; dot: string; banner: string }> = {
  green: {
    pill: "border-emerald-700/60 bg-emerald-500/10 text-emerald-300",
    dot: "bg-emerald-400",
    banner: "border-emerald-800/60 bg-emerald-950/40 text-emerald-100",
  },
  amber: {
    pill: "border-amber-700/60 bg-amber-500/10 text-amber-300",
    dot: "bg-amber-400",
    banner: "border-amber-800/60 bg-amber-950/40 text-amber-100",
  },
  red: {
    pill: "border-red-700/60 bg-red-500/10 text-red-300",
    dot: "bg-red-400",
    banner: "border-red-900/60 bg-red-950/50 text-red-100",
  },
  grey: {
    pill: "border-gray-700 bg-gray-800/60 text-gray-300",
    dot: "bg-gray-400",
    banner: "border-gray-800 bg-gray-900/80 text-gray-200",
  },
};

const STATE_LABEL: Record<string, string> = {
  available: "Available",
  degraded: "Degraded",
  exhausted: "Exhausted",
  unauthenticated: "Logged out",
  failing: "Failing",
  disabled: "Disabled",
};

/** The pill's word.  An unknown state is printed as the server named it. */
export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? (state || "unknown");
}

const STATE_WORDS: Record<string, string> = {
  available: "available",
  degraded: "degraded",
  exhausted: "out of usage",
  unauthenticated: "logged out",
  failing: "launches failing",
  disabled: "disabled by an operator",
};

/** The banner's phrase: "Codex unavailable — logged out since 12:04". */
export function stateWords(state: string): string {
  return STATE_WORDS[state] ?? (state || "unknown");
}

const PROVIDER_NAME: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  gemini: "Gemini",
  llm: "Direct LLM",
};

/** "Codex" for ``codex``; an unknown key is title-cased rather than dropped. */
export function providerName(key: string): string {
  if (!key) return "unknown";
  return PROVIDER_NAME[key] ?? key.charAt(0).toUpperCase() + key.slice(1);
}

export function isUnavailable(status: Pick<ProviderAvailabilityStatus, "half">): boolean {
  return status.half === UNAVAILABLE_HALF;
}

/** An operator disable with no deadline stays in force until auto clears it. */
export function isIndefinitelyDisabled(
  status: Pick<ProviderAvailabilityStatus, "state" | "override">,
): boolean {
  return status.state === "disabled" && status.override?.state === "disabled" && status.override.until == null;
}

/** Stable order so a card never swaps places between polls. */
export function sortStatuses(statuses: ProviderAvailabilityStatus[]): ProviderAvailabilityStatus[] {
  return [...statuses].sort((a, b) => a.provider.localeCompare(b.provider));
}

/** "2h 14m", "14m", "3d 4h", "<1m" — a countdown, read at a glance. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return "<1m";
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}d ${restHours}h` : `${days}d`;
}

function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** "12:04" today, "Sep 20 12:04" any other day — a wall clock, local time. */
export function formatClock(ts: number, now: number): string {
  const then = new Date(ts * 1000);
  const today = new Date(now * 1000);
  const sameDay =
    then.getFullYear() === today.getFullYear() &&
    then.getMonth() === today.getMonth() &&
    then.getDate() === today.getDate();
  if (sameDay) return hhmm(then);
  return `${then.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${hhmm(then)}`;
}

/** "since 12:04 (3h ago)", or null when the server did not say. */
export function formatSince(since: number | null | undefined, now: number): string | null {
  if (since == null || !Number.isFinite(since)) return null;
  const ago = now - since;
  return ago < 60
    ? `since ${formatClock(since, now)} (just now)`
    : `since ${formatClock(since, now)} (${formatDuration(ago)} ago)`;
}

/**
 * The countdown to the server's expected recovery.
 *
 * A deadline already behind the server clock is said so — "was due 15:59" —
 * rather than rendered as "in 0m": the provider has not come back, and the
 * recovery probe or the next sweep is what moves it, not this clock.
 */
export function formatRecovery(until: number | null | undefined, now: number): string | null {
  if (until == null || !Number.isFinite(until)) return null;
  if (until <= now) return `recovery was due ${formatClock(until, now)}`;
  return `expected back in ${formatDuration(until - now)} (${formatClock(until, now)})`;
}

/** "until 15:00 (in 2h)" / "no expiry" for an operator override (D6). */
export function formatOverrideExpiry(until: number | null | undefined, now: number): string {
  if (until == null || !Number.isFinite(until)) return "no expiry";
  if (until <= now) return `expired ${formatClock(until, now)}`;
  return `until ${formatClock(until, now)} (in ${formatDuration(until - now)})`;
}

const PROBE_LABEL: Record<string, string> = {
  authenticated: "logged in",
  not_authenticated: "not logged in",
  cannot_tell: "could not tell",
  not_probeable: "this provider has no login probe",
};

/** What ``provider_recheck``'s login probe answered, in words. */
export function probeLabel(probe: string | null | undefined): string {
  return PROBE_LABEL[probe ?? ""] ?? (probe || "no answer");
}

/** Durations offered by *Disable for…*; the keys are what ``for`` accepts. */
export const DISABLE_DURATIONS = [
  { key: "30m", label: "30 minutes" },
  { key: "4h", label: "4 hours" },
  { key: "1d", label: "1 day" },
] as const;

/**
 * A hold kind (D18) in plain words.  ``ahead`` only means something for
 * ``awaiting_failover_capacity`` — the task's place in the trickle queue.
 */
export function holdKindLabel(kind: string, ahead?: number | null): string {
  switch (kind) {
    case "provider_pinned":
      return "Pinned to this provider — waiting for it to recover";
    case "class_policy_hold":
      return "This intelligence class is set to hold rather than fail over";
    case "no_equivalent_rung":
      return "No equivalent worker on another provider";
    case "no_available_target":
      return "No available provider to move it to";
    case "awaiting_failover_capacity":
      return ahead != null && ahead > 0
        ? `Waiting for failover capacity (${ahead} ahead)`
        : "Waiting for failover capacity";
    case "reroute_limit_reached":
      return "Re-route limit reached — a human decides now";
    case "all_providers_unavailable":
      return "Every provider is unavailable";
    case "failover_inactive":
      return "Failover is not active, so the task waits";
    case "priority_policy_hold":
      return "Priority is outside the failover policy";
    default:
      return kind ? kind.replace(/_/g, " ") : "Held";
  }
}

export const PROVIDER_INTENTS = ["pinned", "preferred", "class_only"] as const;
export type ProviderIntent = (typeof PROVIDER_INTENTS)[number];

const INTENT_LABEL: Record<string, string> = {
  pinned: "Pinned",
  preferred: "Preferred",
  class_only: "Class only",
};

export function intentLabel(intent: string | null | undefined): string {
  const key = intent || "class_only";
  return INTENT_LABEL[key] ?? key.replace(/_/g, " ");
}

const INTENT_HINT: Record<string, string> = {
  pinned: "Pinned: runs only on this profile's provider; holds while that provider is unavailable.",
  preferred:
    "Preferred: runs on this profile's provider, but fails over to the same class elsewhere when it is unavailable.",
  class_only:
    "Class only: any provider that runs this intelligence class will do; routing picked the profile.",
};

export function intentHint(intent: string | null | undefined): string {
  return INTENT_HINT[intent || "class_only"] ?? "";
}
