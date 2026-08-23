import { describe, expect, it } from "vitest";
import { manifest, taskDetailArgsSchema } from "../manifest";

describe("task-detail manifest", () => {
  it("has id matching the directory name", () => {
    expect(manifest.id).toBe("task-detail");
  });

  it("accepts a valid taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: "abc-123" });
    expect(result.success).toBe(true);
  });

  it("rejects an empty taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: "" });
    expect(result.success).toBe(false);
  });

  it("rejects missing taskId", () => {
    const result = taskDetailArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is agent-pushable with the Task palette section", () => {
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.palette_label).toBe("Open task");
    expect(manifest.palette_section).toBe("Task");
  });

  it("has a non-empty description and an icon component", () => {
    expect(manifest.description.length).toBeGreaterThan(0);
    expect(manifest.icon).toBeDefined();
  });

  it("is cross-route scoped", () => {
    expect(manifest.route_scope).toBe("cross-route");
  });

  it("rejects a non-string taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: 123 });
    expect(result.success).toBe(false);
  });
});
