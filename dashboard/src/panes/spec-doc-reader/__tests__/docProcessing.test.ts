import { describe, expect, it } from "vitest";
import { extractToc, parseFrontmatter, resolveTitle } from "../docProcessing";

describe("extractToc", () => {
  it("extracts only ## and ### headings", () => {
    const md = ["# Title", "## Goal", "### Sub Goal", "#### Too Deep", "## Non-goals"].join(
      "\n\n",
    );
    const toc = extractToc(md);
    expect(toc.map((e) => e.text)).toEqual(["Goal", "Sub Goal", "Non-goals"]);
    expect(toc.map((e) => e.depth)).toEqual([2, 3, 2]);
  });

  it("produces ids matching github-slugger's default algorithm", () => {
    const toc = extractToc("## Hello World\n");
    expect(toc[0]?.id).toBe("hello-world");
  });

  it("keeps dedup counters in sync with h1/h4+ headings in between (slug parity)", () => {
    // "Overview" appears as h1, then h2, then h4 — a slugger that only
    // sees the h2/h3 subset would slug both non-h1 occurrences as
    // "overview" (since it never saw the h1 consume "overview" first).
    // The real MarkdownPreview render slugs ALL headings in document
    // order, so the h2 here must come out as "overview-1", matching what
    // rehype-slug would assign to the same h2 node.
    const md = "# Overview\n\n## Overview\n\n#### Overview\n";
    const toc = extractToc(md);
    expect(toc).toEqual([{ id: "overview-1", text: "Overview", depth: 2 }]);
  });
});

describe("parseFrontmatter", () => {
  it("parses a fenced YAML block", () => {
    const raw = "---\nstatus: design\ndate: 2026-08-22\n---\n\n## Body\n";
    const { data, content } = parseFrontmatter(raw);
    expect(data).toEqual({ status: "design", date: "2026-08-22" });
    expect(content.trim()).toBe("## Body");
  });

  it("returns null data when there is no frontmatter of either kind", () => {
    const { data, content } = parseFrontmatter("## Just a body\n");
    expect(data).toBeNull();
    expect(content).toBe("## Just a body\n");
  });

  it("falls back to bold-label preamble parsing when no fenced block exists", () => {
    const raw = [
      "# Title",
      "",
      "**Status:** design (approved).",
      "**Depends on:** other-spec.md",
      "",
      "## Body",
    ].join("\n");
    const { data } = parseFrontmatter(raw);
    expect(data).toEqual({
      Status: "design (approved).",
      "Depends on": "other-spec.md",
    });
  });

  it("only scans the preamble before the first ## heading for the bold-label fallback", () => {
    const raw = [
      "# Title",
      "**Status:** design",
      "",
      "## Body",
      "**Not:** a frontmatter field",
    ].join("\n");
    const { data } = parseFrontmatter(raw);
    expect(data).toEqual({ Status: "design" });
  });
});

describe("resolveTitle", () => {
  it("prefers frontmatter title over name", () => {
    const t = resolveTitle({
      frontmatter: { title: "Real Title", name: "Other" },
      body: "# H1 Title\n",
      fallbackName: "fallback.md",
    });
    expect(t).toBe("Real Title");
  });

  it("falls back to name when title is absent", () => {
    const t = resolveTitle({
      frontmatter: { name: "Named Doc" },
      body: "# H1 Title\n",
      fallbackName: "fallback.md",
    });
    expect(t).toBe("Named Doc");
  });

  it("falls back to the first h1 when there is no frontmatter title/name", () => {
    const t = resolveTitle({ frontmatter: null, body: "# H1 Title\n\nbody", fallbackName: "fallback.md" });
    expect(t).toBe("H1 Title");
  });

  it("falls back to a humanized filename when there is no frontmatter or h1", () => {
    const t = resolveTitle({ frontmatter: null, body: "no heading here", fallbackName: "my-spec-doc.md" });
    expect(t).toBe("My Spec Doc");
  });

  it("humanizes the last path segment of a URL fallback", () => {
    const t = resolveTitle({ frontmatter: null, body: "", fallbackName: "/api/specs/checkout_flow.md" });
    expect(t).toBe("Checkout Flow");
  });
});
