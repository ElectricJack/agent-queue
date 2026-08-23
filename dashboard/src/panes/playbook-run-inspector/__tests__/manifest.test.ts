import { describe, it, expect } from "vitest";
import { manifest, argsSchema } from "../manifest";

describe("playbook-run-inspector manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("playbook-run-inspector");
  });

  it("argsSchema accepts a valid runId", () => {
    const result = argsSchema.safeParse({ runId: "abc123" });
    expect(result.success).toBe(true);
  });

  it("argsSchema rejects an empty object", () => {
    const result = argsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("argsSchema rejects an empty runId string", () => {
    const result = argsSchema.safeParse({ runId: "" });
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is cross-route", () => {
    expect(manifest.route_scope).toBe("cross-route");
  });

  it("is agent-pushable", () => {
    expect(manifest.agent_pushable).toBe(true);
  });

  it("has the expected palette label and section", () => {
    expect(manifest.palette_label).toBe("Inspect playbook run");
    expect(manifest.palette_section).toBe("Playbooks");
  });
});
