import { describe, expect, it } from "vitest";
import { isTaskBlocked } from "../hierarchy";
import type { GraphTaskNode } from "../types";

const task = (patch: Partial<GraphTaskNode>): GraphTaskNode =>
  ({ id: "t", title: "Task t", status: "READY", priority: 100, ...patch });

describe("isTaskBlocked", () => {
  it("reads the blocked flag and the BLOCKED status on a live task", () => {
    expect(isTaskBlocked(task({ status: "READY" }))).toBe(false);
    expect(isTaskBlocked(task({ status: "READY", is_blocked: true }))).toBe(true);
    expect(isTaskBlocked(task({ status: "BLOCKED" }))).toBe(true);
  });

  it("ignores a stale flag once the task has reached a terminal state", () => {
    for (const status of ["COMPLETED", "FAILED", "CANCELED", "CANCELLED", "SKIPPED"]) {
      expect(isTaskBlocked(task({ status, is_blocked: true }))).toBe(false);
    }
  });
});
