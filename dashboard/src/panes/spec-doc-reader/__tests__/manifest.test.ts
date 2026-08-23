import { describe, expect, it } from "vitest";
import { manifest, type SpecDocReaderArgs } from "../manifest";

describe("spec-doc-reader manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("spec-doc-reader");
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("registers a palette action under Docs", () => {
    expect(manifest.palette_label).toBe("Read spec");
    expect(manifest.palette_section).toBe("Docs");
  });

  it("is cross-route and agent-pushable", () => {
    expect(manifest.route_scope).toBe("cross-route");
    expect(manifest.agent_pushable).toBe(true);
  });

  describe("args_schema", () => {
    const parse = (v: unknown) => manifest.args_schema!.safeParse(v);

    it("accepts workspaceId + path", () => {
      const r = parse({ workspaceId: "ws-1", path: "docs/x.md" });
      expect(r.success).toBe(true);
    });

    it("accepts url alone", () => {
      const r = parse({ url: "/api/specs/x.md" });
      expect(r.success).toBe(true);
    });

    it("rejects an empty object", () => {
      expect(parse({}).success).toBe(false);
    });

    it("rejects workspaceId alone", () => {
      expect(parse({ workspaceId: "ws-1" }).success).toBe(false);
    });

    it("rejects path alone", () => {
      expect(parse({ path: "docs/x.md" }).success).toBe(false);
    });

    it("rejects all three present at once", () => {
      const r = parse({ workspaceId: "ws-1", path: "docs/x.md", url: "/api/x.md" });
      expect(r.success).toBe(false);
    });

    it("satisfies SpecDocReaderArgs typing on success", () => {
      const r = parse({ url: "/api/specs/x.md" });
      if (r.success) {
        const args: SpecDocReaderArgs = r.data;
        expect(args.url).toBe("/api/specs/x.md");
      }
    });
  });
});
