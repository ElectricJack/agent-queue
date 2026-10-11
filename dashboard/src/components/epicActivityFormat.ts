import type { GraphTaskNode } from "@aq/ts-client";

export type EpicActivity = Pick<GraphTaskNode, "display_state" | "display_reason" | "child_state_counts" | "descendant_state_counts">;

export const DISPLAY_STATUS = {
  active: "IN_PROGRESS", waiting: "WAITING", blocked: "BLOCKED",
  collecting: "IN_PROGRESS", delivering: "IN_PROGRESS", completed: "COMPLETED",
} as const;

export const DISPLAY_LABEL = {
  active: "Active", waiting: "Waiting", blocked: "Blocked",
  collecting: "Collecting", delivering: "Delivering", completed: "Done",
} as const;

export function childCountText(counts: EpicActivity["child_state_counts"]): string {
  return Object.entries(counts ?? {}).filter(([, count]) => count > 0)
    .map(([state, count]) => `${count} ${state.toLowerCase().replace(/_/g, " ")}`).join(" · ");
}

/** Filter activity using the same projection as the pills. Leaf lifecycle
 * filters remain unchanged; waiting epics do not inflate the running count. */
export function displayedTaskStatus(task: EpicActivity & { status?: string | null }): string {
  return task.display_state ? DISPLAY_STATUS[task.display_state] : (task.status ?? "").toUpperCase();
}

/** Count executable descendants without intermediate container lifecycles. */
export function epicLeafProgress(counts: EpicActivity["descendant_state_counts"]) {
  if (!counts) return null;
  return {
    total: Object.values(counts).reduce((total, count) => total + count, 0),
    done: (counts.COMPLETED ?? 0) + (counts.CANCELLED ?? 0) + (counts.CANCELED ?? 0) + (counts.SKIPPED ?? 0),
    running: (counts.IN_PROGRESS ?? 0) + (counts.ASSIGNED ?? 0),
    blocked: (counts.BLOCKED ?? 0) + (counts.FAILED ?? 0),
  };
}
