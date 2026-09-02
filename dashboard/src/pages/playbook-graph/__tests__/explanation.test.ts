import { describe, expect, it } from "vitest";
import { effectLine, inputLine } from "../explanation";

describe("effectLine", () => {
  it("returns the effect text unchanged when the effect is unconditional", () => {
    expect(
      effectLine({ operation: "create_or_reuse", text: 'Create or reuse a task keyed by "dedup_key"' }),
    ).toBe('Create or reuse a task keyed by "dedup_key"');
  });

  it("appends the condition as a dashed suffix when one is present", () => {
    expect(
      effectLine({
        operation: "link",
        text: "Link the gate to its waiters",
        condition: "when waiter_task_ids is provided",
      }),
    ).toBe("Link the gate to its waiters — when waiter_task_ids is provided");
  });

  it("treats a null condition as unconditional", () => {
    expect(effectLine({ operation: "read", text: "Read the downstream tasks", condition: null })).toBe(
      "Read the downstream tasks",
    );
  });
});

describe("inputLine", () => {
  it("renders the label and the human-facing value text", () => {
    expect(
      inputLine({
        field: "project_id",
        label: "Project",
        value: { kind: "event_ref", text: "this event's project", raw: "{{event.project_id}}" },
      }),
    ).toBe("Project → this event's project");
  });

  it("never leaks the raw expression of a redacted value", () => {
    const line = inputLine({
      field: "token",
      label: "Token",
      value: { kind: "literal", text: "[redacted]", raw: null, redacted: true },
    });
    expect(line).toBe("Token → [redacted]");
    expect(line).not.toContain("hunter2");
  });
});
