import { describe, expect, it } from "vitest";

import { headingPathAt, locateQuote, partitionComments } from "../anchoring";

describe("review comment anchoring", () => {
  const markdown = "# Document\n\n## Goal\n\nThe goal text.\n\n### Detail\n\nDetailed text.\n";

  it("finds the enclosing h2 and h3 path at a source offset", () => {
    expect(headingPathAt(markdown, markdown.indexOf("goal text"))).toEqual(["Goal"]);
    expect(headingPathAt(markdown, markdown.indexOf("Detailed text"))).toEqual(["Goal", "Detail"]);
  });

  it("locates the first instance of a quote and handles missing text", () => {
    expect(locateQuote("repeat repeat", "repeat")).toBe(0);
    expect(locateQuote("visible text", "missing")).toBeNull();
  });

  it("moves missing and resolved earlier-revision comments into earlier comments", () => {
    const comments = [
      { id: "visible", quote: "visible", revision: 2 },
      { id: "missing", quote: "gone", revision: 2 },
      { id: "resolved", quote: "visible", revision: 1, resolved_at: 1 },
    ];

    expect(partitionComments(comments, "visible content", 2)).toEqual({
      anchored: [comments[0]],
      earlier: [comments[1], comments[2]],
    });
  });
});
