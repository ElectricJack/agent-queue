import { describe, expect, it } from "vitest";
import { manifest } from "../manifest";

describe("contextual-settings manifest", () => {
  it("has id matching the directory name", () => {
    expect(manifest.id).toBe("contextual-settings");
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is cross-route and agent-pushable with a palette entry", () => {
    expect(manifest.route_scope).toBe("cross-route");
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.palette_label).toBe("Open settings for…");
    expect(manifest.palette_section).toBe("Settings");
  });
});
