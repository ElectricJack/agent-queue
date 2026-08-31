import { describe, expect, it } from "vitest";
import { matchesTask, readTaskFilters, writeTaskFilters } from "../taskFilters";

const base = { query: "", status: "", showCompleted: false };

describe("shared task filters", () => {
  it("searches words across title, ID, project, and agent without case sensitivity", () => {
    const task = { id: "bright-fox", title: "Fix checkout", status: "READY", assigned_agent_id: "worker-sol" };
    expect(matchesTask(task, { ...base, query: "CHECKOUT sol moss" }, "moss-and-spade")).toBe(true);
    expect(matchesTask(task, { ...base, query: "checkout missing" }, "moss-and-spade")).toBe(false);
    expect(matchesTask(task, { ...base, query: "bright-fox" })).toBe(true);
  });

  it("keeps failures visible, excludes finished tasks by default, and supports exact status", () => {
    expect(matchesTask({ id: "a", status: "FAILED" }, base)).toBe(true);
    expect(matchesTask({ id: "b", status: "COMPLETED" }, base)).toBe(false);
    expect(matchesTask({ id: "c", status: "CANCELED" }, base)).toBe(false);
    expect(matchesTask({ id: "b", status: "COMPLETED" }, { ...base, status: "COMPLETED" })).toBe(true);
    expect(matchesTask({ id: "b", status: "READY" }, { ...base, status: "IN_PROGRESS" })).toBe(false);
  });

  it("round-trips filters while preserving unrelated deep-link parameters", () => {
    const params = new URLSearchParams("openDrawer=events&q=old");
    const result = writeTaskFilters(params, { query: "new task", status: "READY", showCompleted: true });
    expect(readTaskFilters(result)).toEqual({ query: "new task", status: "READY", showCompleted: true });
    expect(result.get("openDrawer")).toBe("events");
    expect(params.get("q")).toBe("old");
    expect(writeTaskFilters(result, base).toString()).toBe("openDrawer=events");
  });

  it("retains unknown statuses from links rather than silently broadening a filter", () => {
    expect(readTaskFilters(new URLSearchParams("status=custom&completed=1"))).toEqual({ query: "", status: "CUSTOM", showCompleted: true });
  });
});
