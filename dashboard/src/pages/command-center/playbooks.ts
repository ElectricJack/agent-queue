import type { PlaybookSummary } from "../../api/hooks";

export function projectPlaybooks(definitions: PlaybookSummary[], projectIds: string[], query = "") {
  if (!projectIds.length) return [];
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return definitions.filter(p => {
    if (p.scope === "project" && !projectIds.includes(p.scope_identifier ?? "")) return false;
    const text = [p.id, p.scope, p.scope_identifier, p.agent_type, ...(p.triggers ?? [])].join(" ").toLowerCase();
    return words.every(word => text.includes(word));
  }).sort((a, b) => Number(b.scope === "project") - Number(a.scope === "project") || a.id.localeCompare(b.id));
}

export function playbookRunning(p: PlaybookSummary) {
  return (p.running_count ?? 0) > 0 || ["running", "paused"].includes(p.last_run?.status ?? "");
}

/** A definition never completes: only its individual runs do. */
export function playbookState(p: PlaybookSummary): string {
  if (p.last_run?.status === "paused") return "Run paused";
  if (playbookRunning(p)) return (p.running_count ?? 0) > 1 ? `Running · ${p.running_count}` : "Running";
  if (p.enabled === false) return "Triggers paused";
  if ((p.cooldown_remaining ?? 0) > 0) return "Waiting · cooldown";
  return p.triggers?.length ? "Waiting for trigger" : "Ready to run";
}

/** Lifecycles a run can still leave on its own — everything else is history. */
const LIVE_STATUSES = new Set(["running", "paused", "cancelling"]);

/**
 * "completed · 5m ago", or "running · 12s" while a run is still in flight.
 *
 * A finished run is dated from when it ended; a live one counts up from its
 * start, which is the difference between "something happened here once" and
 * "work is happening right now".
 */
export function lastRunLabel(p: PlaybookSummary, now = Date.now()): string {
  const run = p.last_run;
  if (!run) return "never";
  const status = run.status.replace(/_/g, " ");
  const at = run.completed_at ?? run.started_at;
  if (!at) return status;
  const seconds = Math.max(0, Math.round(now / 1000 - at));
  if (LIVE_STATUSES.has(run.status)) return `${status} · ${elapsed(seconds)}`;
  const since = seconds < 60 ? "just now"
    : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago`
    : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago`
    : `${Math.floor(seconds / 86400)}d ago`;
  return `${status} · ${since}`;
}

function elapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function playbookScope(p: PlaybookSummary) {
  return p.scope === "project" ? `Project · ${p.scope_identifier}`
    : p.scope === "system" ? "System · shared" : `${p.scope} · shared`;
}

export function manualPlaybookEvent(p: PlaybookSummary, text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Event must be a JSON object.");
  const event = { type: "manual", ...value } as Record<string, unknown>;
  if (p.scope === "project") {
    if (!p.scope_identifier) throw new Error("This playbook has no project scope.");
    if (event.project_id && event.project_id !== p.scope_identifier) throw new Error("Event project must match the playbook's project.");
    event.project_id = p.scope_identifier;
  }
  return event;
}
