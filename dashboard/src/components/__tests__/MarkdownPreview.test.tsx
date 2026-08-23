import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import MarkdownPreview from "../MarkdownPreview";

describe("MarkdownPreview", () => {
  it("assigns slug ids to headings", () => {
    const { container } = render(
      <MarkdownPreview source={"# Title\n\n## Goal\n\n### Sub Goal\n"} />,
    );
    const h2 = container.querySelector("h2");
    const h3 = container.querySelector("h3");
    expect(h2?.id).toBe("goal");
    expect(h3?.id).toBe("sub-goal");
  });

  it("dedupes repeated heading text the same way github-slugger does", () => {
    const { container } = render(<MarkdownPreview source={"## Overview\n\n## Overview\n"} />);
    const headings = container.querySelectorAll("h2");
    expect(headings[0]?.id).toBe("overview");
    expect(headings[1]?.id).toBe("overview-1");
  });

  it("still renders GFM tables (existing behavior, unaffected)", () => {
    const { container } = render(
      <MarkdownPreview source={"| a | b |\n|---|---|\n| 1 | 2 |\n"} />,
    );
    expect(container.querySelector("table")).not.toBeNull();
  });
});
