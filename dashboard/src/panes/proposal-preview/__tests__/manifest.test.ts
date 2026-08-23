import { describe, it, expect } from "vitest";
import { manifest, argsSchema } from "../manifest";

describe("proposal-preview manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("proposal-preview");
  });

  it("accepts a valid proposalId", () => {
    expect(argsSchema.safeParse({ proposalId: "prop-abc" }).success).toBe(true);
  });

  it("rejects a missing or empty proposalId", () => {
    expect(argsSchema.safeParse({}).success).toBe(false);
    expect(argsSchema.safeParse({ proposalId: "" }).success).toBe(false);
  });

  it("has no open_shortcut (declared by omission, not null)", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is agent-pushable", () => {
    expect(manifest.agent_pushable).toBe(true);
  });

  it("carries the palette label and section", () => {
    expect(manifest.palette_label).toBe("Preview proposal");
    expect(manifest.palette_section).toBe("Proposals");
  });

  it("route_scope is cross-route", () => {
    expect(manifest.route_scope).toBe("cross-route");
  });
});
