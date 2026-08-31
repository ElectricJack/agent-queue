import { describe, expect, it } from "vitest";
import type { PlaybookSummary } from "../../../api/hooks";
import { manualPlaybookEvent, playbookState, projectPlaybooks } from "../playbooks";

const book = (id: string, patch: Partial<PlaybookSummary> = {}): PlaybookSummary => ({ id, scope: "system", triggers: ["timer.24h"], ...patch });
describe("persistent playbook definitions", () => {
  it("includes shared definitions and only the selected project's definitions, even after completion", () => {
    const definitions = [book("system", { last_run: { run_id: "run", status: "completed" } }),
      book("other", { scope: "project", scope_identifier: "beta" }),
      book("local", { scope: "project", scope_identifier: "alpha" }),
      book("agent", { scope: "agent-type:supervisor" })];
    expect(projectPlaybooks(definitions, ["alpha"]).map(p => p.id)).toEqual(["local", "agent", "system"]);
    expect(projectPlaybooks(definitions, ["beta"]).map(p => p.id)).toEqual(["other", "agent", "system"]);
    expect(projectPlaybooks(definitions, []).length).toBe(0);
  });
  it("searches names, hooks, and scopes without consulting task completion filters", () => {
    expect(projectPlaybooks([book("audit"), book("manual", { triggers: [] })], ["alpha"], "TIMER.24H").map(p => p.id)).toEqual(["audit"]);
  });
  it.each(["completed", "failed", "timed_out", "cancelled"])("returns to waiting after %s without losing the last result", status => {
    const p = book("audit", { last_run: { run_id: "run", status } });
    expect(playbookState(p)).toBe("Waiting for trigger");
    expect(p.last_run?.status).toBe(status);
  });
  it("distinguishes running, human input, disabled triggers, cooldown, and manual-only definitions", () => {
    expect(playbookState(book("a", { running_count: 2 }))).toBe("Running · 2");
    expect(playbookState(book("a", { last_run: { run_id: "r", status: "running" } }))).toBe("Running");
    expect(playbookState(book("a", { running_count: 1, last_run: { run_id: "r", status: "paused" } }))).toBe("Run paused");
    expect(playbookState(book("a", { enabled: false }))).toBe("Triggers paused");
    expect(playbookState(book("a", { enabled: false, running_count: 1 }))).toBe("Running");
    expect(playbookState(book("a", { cooldown_remaining: 300 }))).toBe("Waiting · cooldown");
    expect(playbookState(book("a", { triggers: [] }))).toBe("Ready to run");
  });
});

it("manual launches retain definition scope and require an object event", () => {
  expect(manualPlaybookEvent(book("system"), '{}')).toEqual({ type: "manual" });
  const project = book("audit", { scope: "project", scope_identifier: "alpha" });
  expect(manualPlaybookEvent(project, '{"task_id":"t"}')).toEqual({ type: "manual", project_id: "alpha", task_id: "t" });
  expect(() => manualPlaybookEvent(project, '{"project_id":"beta"}')).toThrow("match");
  expect(() => manualPlaybookEvent(project, '[]')).toThrow("object");
});
