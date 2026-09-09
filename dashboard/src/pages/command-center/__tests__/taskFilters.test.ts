import { describe, expect, it } from "vitest";
import { activityWindowHours, activityWindowLabel, matchesTask, readTaskFilters, writeTaskFilters } from "../taskFilters";

const base = { query: "", status: "", showCompleted: false, focus: "", window: "" };

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
    const result = writeTaskFilters(params, { query: "new task", status: "READY", showCompleted: true, focus: "", window: "" });
    expect(readTaskFilters(result)).toEqual({ query: "new task", status: "READY", showCompleted: true, focus: "", window: "" });
    expect(result.get("openDrawer")).toBe("events");
    expect(params.get("q")).toBe("old");
    expect(writeTaskFilters(result, base).toString()).toBe("openDrawer=events");
  });

  it("retains unknown statuses from links rather than silently broadening a filter", () => {
    expect(readTaskFilters(new URLSearchParams("status=custom&completed=1"))).toEqual({ query: "", status: "CUSTOM", showCompleted: true, focus: "", window: "" });
  });

  it("resolves the offered time ranges and rejects anything else", () => {
    expect(activityWindowHours("24h")).toBe(24);
    expect(activityWindowLabel("24h")).toBe("Last 24 hours");
    expect(activityWindowHours("7d")).toBe(24 * 7);
    expect(activityWindowHours("")).toBeNull();
    expect(activityWindowHours("since-the-dawn-of-time")).toBeNull();
  });

  it("drops an unknown window from a link instead of mislabelling the range", () => {
    expect(readTaskFilters(new URLSearchParams("window=all-time")).window).toBe("");
    expect(readTaskFilters(new URLSearchParams("window=7d")).window).toBe("7d");
  });

  it("keeps completed work visible inside a time range", () => {
    const completed = { id: "b", status: "COMPLETED" };
    expect(matchesTask(completed, base)).toBe(false);
    expect(matchesTask(completed, { ...base, window: "24h" })).toBe(true);
    // The range narrows the rows, it does not widen the status filter.
    expect(matchesTask(completed, { ...base, window: "24h", status: "READY" })).toBe(false);
  });

  it("round-trips the focus param", () => {
    const p = writeTaskFilters(new URLSearchParams(), { query: "", status: "", showCompleted: false, focus: "e1", window: "" });
    expect(p.get("focus")).toBe("e1");
    expect(readTaskFilters(p).focus).toBe("e1");
    expect(writeTaskFilters(p, { query: "", status: "", showCompleted: false, focus: "", window: "" }).has("focus")).toBe(false);
  });
});
