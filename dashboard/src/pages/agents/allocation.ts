/**
 * Pure readers and request builders behind the Providers view.
 *
 * Every state here arrives from ``provider_allocation_status`` /
 * ``_preview`` / ``_apply``; these helpers only decide how it reads and turn
 * the editor's draft into the spec's request.  They never derive a busy set,
 * a ceiling or an outcome of their own: a browser that recomputed those would
 * eventually disagree with the token the daemon checks.
 */

import type {
  ProviderAllocationApplyResponse,
  ProviderAllocationCeiling,
  ProviderAllocationGroup,
  ProviderAllocationPreviewBody,
  ProviderAllocationPreviewResponse,
  ProviderAllocationPreviewSession,
  ProviderAllocationProfile,
  ProviderAllocationSupply,
  ProviderAllocationWarning,
} from "../../api/client";
import { providerName } from "../metrics/providerAvailabilityFormat";
import { validateBounds, type BoundsDraft } from "./PoolScaleFields";

const VENDOR_NAME: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  google: "Google",
};

/** "OpenAI" for ``openai``; an unknown vendor is printed as the server named it. */
export function vendorName(vendor: string | null | undefined): string {
  if (!vendor) return "";
  return VENDOR_NAME[vendor.toLowerCase()] ?? vendor;
}

/**
 * "OpenAI (Codex)": the vendor beside the harness provider key the allocation
 * is grouped by (spec invariant 1), never the session transport.
 */
export function providerLabel(provider: string, vendor?: string | null): string {
  const vendorText = vendorName(vendor);
  const name = providerName(provider);
  return vendorText && vendorText !== name ? `${vendorText} (${name})` : name;
}

/** A pool bound for reading: ``null`` is an unbounded pool. */
export function boundText(value: number | null | undefined): string {
  return value == null ? "∞" : String(value);
}

/** "[1–4]", the way ``PoolSupplyRow`` prints a pool's bounds. */
export function profileBounds(profile: Pick<ProviderAllocationProfile, "min_active" | "max_active">): string {
  return `[${profile.min_active ?? 0}–${boundText(profile.max_active)}]`;
}

/**
 * "1–6 across 2 pool profiles" — the provider-wide configured ceiling.
 *
 * Read-only: bounds stay per profile, and this sum is there so ``max 2`` over
 * three profiles is never read as two workers in all.
 */
export function ceilingText(ceiling: ProviderAllocationCeiling): string {
  const count = ceiling.pool_profiles ?? 0;
  const across = `across ${count} pool profile${count === 1 ? "" : "s"}`;
  if (count === 0) return "none: no pool profiles";
  if (ceiling.unbounded || ceiling.max_active == null) return `unbounded ${across}`;
  return `${ceiling.min_active ?? 0}–${ceiling.max_active} ${across}`;
}

/** The maximum alone, for a before → after line. */
export function ceilingMax(ceiling: ProviderAllocationCeiling): string {
  return ceiling.unbounded || ceiling.max_active == null ? "unbounded" : String(ceiling.max_active);
}

/** Live sessions of every kind the supply counts. */
export function liveCount(supply: ProviderAllocationSupply): number {
  return (supply.idle ?? 0) + (supply.busy ?? 0) + (supply.starting ?? 0)
    + (supply.draining ?? 0) + (supply.unresponsive ?? 0);
}

export function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** A project's preference for unpinned work, as seen from one provider's card. */
export function preferenceText(preferred: string | null | undefined, provider: string): string {
  if (!preferred) return "no preference";
  if (preferred === provider) return "prefers this provider";
  return "prefers " + providerName(preferred);
}

// -- the editor's draft ----------------------------------------------------------

export type Participation = "" | "pool" | "task";
export type PreferenceMode = "" | "prefer" | "clear";
export type DrainMode = "graceful" | "idle-now" | "interrupt-busy";

export interface AllocationDraft {
  /** The checked profiles; every eligible profile checked means ``profile_ids: null``. */
  profileIds: string[];
  participation: Participation;
  changeBounds: boolean;
  bounds: BoundsDraft;
  preferenceProject: string;
  preferenceMode: PreferenceMode;
  drain: DrainMode;
}

export function initialDraft(group: ProviderAllocationGroup): AllocationDraft {
  return {
    profileIds: (group.profiles ?? []).map((profile) => profile.profile_id),
    participation: "",
    changeBounds: false,
    bounds: { min: "0", max: "" },
    preferenceProject: "",
    preferenceMode: "",
    drain: "graceful",
  };
}

/** Why the draft cannot be previewed yet, in the daemon's own terms; null when it can. */
export function draftProblem(draft: AllocationDraft): string | null {
  const structural = draft.participation !== "" || draft.changeBounds;
  const preference = draft.preferenceMode !== "";
  if (!structural && !preference) {
    return "Choose a lifecycle, per-profile bounds or a project preference to preview.";
  }
  if (structural && draft.profileIds.length === 0) return "Select at least one profile.";
  if (draft.changeBounds && draft.participation === "task") {
    return "Bounds apply only to pool profiles; a switch to task clears them.";
  }
  if (draft.changeBounds) {
    const invalid = validateBounds(draft.bounds);
    if (invalid) return invalid;
  }
  if (preference && !draft.preferenceProject) return "Choose the project whose preference changes.";
  return null;
}

/**
 * The spec's request for a validated draft.
 *
 * Only what the operator changed is sent: an omitted key leaves that part of
 * the fleet alone, ``profile_ids`` is omitted when every eligible profile is
 * checked (the daemon then selects the provider's whole set), and an empty
 * maximum is an explicit ``max: null`` (unbounded), never an omitted one.
 */
export function previewBody(draft: AllocationDraft, group: ProviderAllocationGroup): ProviderAllocationPreviewBody {
  const body: ProviderAllocationPreviewBody = { provider: group.provider };
  const eligible = (group.profiles ?? []).map((profile) => profile.profile_id);
  const structural = draft.participation !== "" || draft.changeBounds;
  if (structural && !eligible.every((id) => draft.profileIds.includes(id))) {
    body.profile_ids = [...draft.profileIds].sort();
  }
  if (draft.participation) body.participation = draft.participation;
  if (draft.changeBounds) {
    const max = draft.bounds.max.trim();
    body.bounds = { min: Number(draft.bounds.min.trim()), max: max === "" ? null : Number(max) };
  }
  if (draft.preferenceMode && draft.preferenceProject) {
    body.receive_new_work = { project_id: draft.preferenceProject, mode: draft.preferenceMode };
  }
  body.drain = draft.drain;
  return body;
}

// -- the confirmation ------------------------------------------------------------

const SESSION_ACTION: Record<string, string> = {
  none: "unchanged",
  stop: "marked stopped; the reconciler tears it down",
  terminate: "stopped now",
  stop_after_task: "finishes its task, then stops",
  interrupt: "interrupted now",
};

export function sessionActionText(action: string): string {
  return SESSION_ACTION[action] ?? action;
}

/** Sessions the request touches at all. */
export function affectedSessions(preview: ProviderAllocationPreviewResponse): ProviderAllocationPreviewSession[] {
  return (preview.sessions ?? []).filter((session) => session.action !== "none");
}

/**
 * The exact set an ``interrupt-busy`` apply must authorize: the sessions the
 * preview marked ``interrupt`` (``busy_authorization_error`` checks the same).
 */
export function interruptSet(preview: ProviderAllocationPreviewResponse): ProviderAllocationPreviewSession[] {
  return (preview.sessions ?? []).filter((session) => session.action === "interrupt");
}

/** True when any worker the request stops is mid-task: the confirmation opens its detail. */
export function anyBusy(preview: ProviderAllocationPreviewResponse): boolean {
  return (preview.busy?.session_ids ?? []).length > 0 || interruptSet(preview).length > 0;
}

/** Blocking warnings the preview itself did not acknowledge (``pinned_ready_wait``). */
export function unacknowledged(preview: ProviderAllocationPreviewResponse): ProviderAllocationWarning[] {
  return (preview.warnings ?? []).filter((warning) => warning.blocking && !warning.acknowledged);
}

// -- the apply result ------------------------------------------------------------

/**
 * What an apply answer means for the drawer.
 *
 * ``applied`` / ``partial`` / ``rolled_back`` are the daemon's ``status``; an
 * answer with only an ``error_code`` is a refusal before anything changed.  A
 * status the daemon adds later is treated as a failure, never as success.
 */
export type ApplyOutcome = "applied" | "partial" | "rolled_back" | "refused";

export function applyOutcome(result: ProviderAllocationApplyResponse): ApplyOutcome {
  if (result.status === "applied" && result.success !== false) return "applied";
  if (result.status === "rolled_back") return "rolled_back";
  if (result.status) return "partial";
  return "refused";
}

const PROFILE_STATUS: Record<string, string> = {
  applied: "applied",
  failed: "failed",
  rolled_back: "rolled back",
  rollback_failed: "rollback failed",
  skipped: "not reached",
};

export function profileStatusText(status: string): string {
  return PROFILE_STATUS[status] ?? status;
}

/**
 * The URL search that opens the Events drawer on one allocation's events,
 * keeping whatever the current page addresses (``view=providers``).
 */
export function eventsSearch(currentSearch: string, requestId: string): string {
  const params = new URLSearchParams(currentSearch);
  params.set("openDrawer", "events");
  params.set("eventRequest", requestId);
  return "?" + params.toString();
}
