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
