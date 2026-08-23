import { describe, expect, it } from "vitest";
import { manifest, consoleStreamArgsSchema } from "../manifest";

describe("console-stream manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("console-stream");
  });

  it("has no open_shortcut and no palette_label — agent-push primary", () => {
    expect(manifest.open_shortcut).toBeUndefined();
    expect(manifest.palette_label).toBeNull();
  });

  it("is agent-pushable and cross-route", () => {
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.route_scope).toBe("cross-route");
  });

  it("args_schema accepts a valid args object", () => {
    const result = consoleStreamArgsSchema.safeParse({ streamId: "abc" });
    expect(result.success).toBe(true);
  });

  it("args_schema accepts optional title and sessionId", () => {
    const result = consoleStreamArgsSchema.safeParse({
      streamId: "abc",
      title: "Running pytest",
      sessionId: "supervisor-global",
    });
    expect(result.success).toBe(true);
  });

  it("args_schema rejects missing streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({}).success).toBe(false);
  });

  it("args_schema rejects non-string streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({ streamId: 123 }).success).toBe(false);
  });

  it("args_schema rejects empty streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({ streamId: "" }).success).toBe(false);
  });
});
