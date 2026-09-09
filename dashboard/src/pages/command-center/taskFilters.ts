export interface TaskFilters {
  query: string;
  status: string;
  showCompleted: boolean;
  focus: string;
  /** Key into ACTIVITY_WINDOWS, or "" for the whole backlog. */
  window: string;
}

/** Recent-work windows offered in the toolbar; "24h" is the labelled default. */
export const ACTIVITY_WINDOWS = [
  { key: "24h", hours: 24, label: "Last 24 hours" },
  { key: "7d", hours: 24 * 7, label: "Last 7 days" },
] as const;

export function activityWindowHours(key: string): number | null {
  return ACTIVITY_WINDOWS.find((w) => w.key === key)?.hours ?? null;
}

export function activityWindowLabel(key: string): string {
  return ACTIVITY_WINDOWS.find((w) => w.key === key)?.label ?? "";
}

// These are the engine's states; legacy terminal spellings remain readable.
export const TASK_STATUSES = [
  "DEFINED", "READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT", "PAUSED",
  "BLOCKED", "FAILED", "COMPLETED",
];
export const FINISHED_STATUSES = new Set(["COMPLETED", "CANCELED", "CANCELLED", "SKIPPED"]);

export function readTaskFilters(params: URLSearchParams): TaskFilters {
  return {
    query: params.get("q") ?? "",
    status: (params.get("status") ?? "").toUpperCase(),
    showCompleted: params.get("completed") === "1",
    focus: params.get("focus") ?? "",
    // An unknown value would silently show the full backlog under a
    // "last 24 hours" heading, so only known keys survive the round trip.
    window: activityWindowHours(params.get("window") ?? "") ? params.get("window")! : "",
  };
}

export function writeTaskFilters(params: URLSearchParams, filters: TaskFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const [key, value] of [["q", filters.query], ["status", filters.status], ["completed", filters.showCompleted ? "1" : ""], ["focus", filters.focus], ["window", filters.window]] as const) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return next;
}

interface SearchableTask {
  id: string;
  title?: string | null;
  status?: string | null;
  project_id?: string | null;
  assigned_agent?: string | null;
  assigned_agent_id?: string | null;
  profile_id?: string | null;
  intelligence_class?: string | null;
}

/** Every search word may match a different field, just like a command palette. */
export function matchesTask(task: SearchableTask, filters: TaskFilters, project = ""): boolean {
  const status = (task.status ?? "").toUpperCase();
  if (filters.status && status !== filters.status) return false;
  // A time range is a claim about work done in it, so it carries completed
  // tasks whether or not the (implied, disabled) checkbox is ticked.
  if (!filters.showCompleted && !filters.status && !filters.window
    && FINISHED_STATUSES.has(status)) return false;
  const words = filters.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const haystack = [task.id, task.title, status, project, task.project_id,
    task.assigned_agent, task.assigned_agent_id, task.profile_id, task.intelligence_class]
    .filter(Boolean).join(" ").toLocaleLowerCase();
  return words.every((word) => haystack.includes(word));
}

export function taskStatusLabel(status: string): string {
  return status.toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
