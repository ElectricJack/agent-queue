import type { GraphTaskNode } from "./types";

const INACTIVE_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELED", "CANCELLED", "SKIPPED"]);

/** Completed/failed rows can retain a stale projection flag; their lifecycle state wins. */
export function isTaskBlocked(task: GraphTaskNode): boolean {
  return !INACTIVE_STATUSES.has(task.status) && Boolean(task.is_blocked || task.status === "BLOCKED");
}
