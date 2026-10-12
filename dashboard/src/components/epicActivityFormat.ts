import type { GraphTaskNode } from "@aq/ts-client";

/** A container's displayed activity, rolled up from its descendants on the server. */
export type EpicActivity = Partial<Pick<GraphTaskNode, "display_state" | "display_reason" | "child_state_counts" | "descendant_state_counts">>;
export type DisplayState = NonNullable<GraphTaskNode["display_state"]>;

/** The card tone (a TaskNode status key) each displayed state reads as. */
export const DISPLAY_STATUS: Record<DisplayState, string> = {
  active: "IN_PROGRESS", waiting: "WAITING", blocked: "BLOCKED",
};

export const DISPLAY_LABEL: Record<DisplayState, string> = {
  active: "Active", waiting: "Waiting", blocked: "Blocked",
};

/** Stages a batch is not yet carrying; any other (queued, integrating,
 * verifying, ...) outranks the rollup, so collecting and delivering stay visible. */
const QUIET_BATCH_STATES = new Set(["implementing", "not_tracked"]);

/** The rolled-up state a surface shows, or null to keep its own status. */
export function rolledState(task: EpicActivity, batch?: { state: string } | null): DisplayState | null {
  return task.display_state && (!batch || QUIET_BATCH_STATES.has(batch.state)) ? task.display_state : null;
}

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
