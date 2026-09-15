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

  it("links registered inline commands without changing other code", () => {
    const docsUrl = "https://docs.example.test/playbook-commands/gate_create";
    const { container, getByRole, getByText } = render(
      <MarkdownPreview
        source={"Known `gate_create`, unknown `not_a_command`:\n\n```yaml\ngate_create\n```"}
        inlineCodeLinks={new Map([["gate_create", docsUrl]])}
      />,
    );

    const link = getByRole("link", { name: "gate_create" });
    expect(link.getAttribute("href")).toBe(docsUrl);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.querySelector("code")).not.toBeNull();
    expect(getByText("not_a_command").closest("a")).toBeNull();
    expect(container.querySelector("pre code")?.textContent).toBe("gate_create\n");
    expect(container.querySelector("pre code")?.closest("a")).toBeNull();
  });

  it("keeps inline code unlinked when command links are not enabled", () => {
    const { container } = render(<MarkdownPreview source={"`gate_create`"} />);

    expect(container.querySelector("code")?.textContent).toBe("gate_create");
    expect(container.querySelector("a")).toBeNull();
  });
});
