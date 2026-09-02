import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PlaybookStepCard } from "../PlaybookStepNode";
import { explanationNode, graph, node, uncontractedNode } from "./fixtures";

afterEach(cleanup);

describe("PlaybookStepCard preview", () => {
  it("previews a contracted node by its intent, not its command name", () => {
    render(<PlaybookStepCard data={{ node: explanationNode }} />);
    expect(screen.getByText("Ensure a review task exists")).toBeInTheDocument();
    expect(screen.queryByText("ensure_task")).not.toBeInTheDocument();
  });

  it("adds the first effect as a second preview line", () => {
    render(<PlaybookStepCard data={{ node: explanationNode }} />);
    expect(screen.getByText('Create or reuse a task keyed by "dedup_key"')).toBeInTheDocument();
  });

  it("falls back to the compiled command for an uncontracted action node", () => {
    render(<PlaybookStepCard data={{ node: uncontractedNode }} />);
    expect(screen.getByText("ensure_task")).toBeInTheDocument();
    expect(screen.queryByText("Ensure a review task exists")).not.toBeInTheDocument();
  });

  it("falls back to the prompt preview when there is no action at all", () => {
    render(<PlaybookStepCard data={{ node: graph.nodes![0]! }} />);
    expect(screen.getByText("Classify the incoming task")).toBeInTheDocument();
  });

  it("shows no preview line when the node offers neither intent, command nor prompt", () => {
    render(<PlaybookStepCard data={{ node: node("bare", { details: {} }) }} />);
    expect(screen.getByRole("button", { name: "Inspect node bare" }).textContent).not.toContain("undefined");
  });
});
