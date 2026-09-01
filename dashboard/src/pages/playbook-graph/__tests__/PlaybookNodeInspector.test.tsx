import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import PlaybookNodeInspector from "../PlaybookNodeInspector";
import { REVIEW_PROMPT, graph } from "./fixtures";

const byId = Object.fromEntries(graph.nodes!.map((n) => [n.id, n]));

function section(name: string) {
  return screen.getByRole("group", { name });
}

afterEach(cleanup);

describe("PlaybookNodeInspector", () => {
  it("instructs the user to pick a node when nothing is selected", () => {
    render(<PlaybookNodeInspector node={null} />);
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Prompt" })).not.toBeInTheDocument();
  });

  it("shows the node id and its classified type", () => {
    render(<PlaybookNodeInspector node={byId.approve!} />);
    const identity = section("Identity");
    expect(within(identity).getByRole("heading", { name: "approve" })).toBeInTheDocument();
    expect(within(identity).getByText("human checkpoint")).toBeInTheDocument();
  });

  it("shows the full untruncated prompt", () => {
    render(<PlaybookNodeInspector node={byId.review!} />);
    const prompt = within(section("Prompt")).getByText(REVIEW_PROMPT.split("\n")[0]!, { exact: false });
    expect(prompt.textContent).toBe(REVIEW_PROMPT);
    expect(prompt.textContent).toContain("Finish by stating the single riskiest line in the diff.");
  });

  it("shows only the flags that are true", () => {
    render(<PlaybookNodeInspector node={byId.triage!} />);
    const flags = section("Flags");
    expect(within(flags).getByText("entry")).toBeInTheDocument();
    expect(within(flags).queryByText("terminal")).not.toBeInTheDocument();
    expect(within(flags).queryByText("human checkpoint")).not.toBeInTheDocument();

    cleanup();
    render(<PlaybookNodeInspector node={byId.approve!} />);
    expect(within(section("Flags")).getByText("human checkpoint")).toBeInTheDocument();
  });

  it("lists every conditional transition with its condition and target", () => {
    render(<PlaybookNodeInspector node={byId.review!} />);
    const transitions = section("Transitions");
    expect(within(transitions).getByText("diff_is_clean")).toBeInTheDocument();
    expect(within(transitions).getByText("approve")).toBeInTheDocument();
    expect(within(transitions).getByText("otherwise")).toBeInTheDocument();
    expect(within(transitions).getAllByText("escalate").length).toBeGreaterThan(0);
  });

  it("shows an unconditional goto as its own section", () => {
    render(<PlaybookNodeInspector node={byId.approve!} />);
    expect(within(section("Goto")).getByText("done")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Transitions" })).not.toBeInTheDocument();
  });

  it("renders action, for_each and output payloads inside named sections", () => {
    render(<PlaybookNodeInspector node={byId.review!} />);
    expect(within(section("Action")).getByText(/"channel": "#reviews"/)).toBeInTheDocument();
    expect(within(section("For each")).getByText(/"items": "changed_files"/)).toBeInTheDocument();
    expect(within(section("Output")).getByText(/"verdict": "string"/)).toBeInTheDocument();
  });

  it("shows execution timeout, pause timeout and timeout target", () => {
    render(<PlaybookNodeInspector node={byId.review!} />);
    const timeouts = section("Timeouts");
    expect(within(timeouts).getByText("600s")).toBeInTheDocument();
    expect(within(timeouts).getByText("120s")).toBeInTheDocument();
    expect(within(timeouts).getByText("escalate")).toBeInTheDocument();
  });

  it("shows node-level and transition LLM configuration separately", () => {
    render(<PlaybookNodeInspector node={byId.review!} />);
    const llm = section("LLM");
    expect(within(llm).getByText("claude-opus-5")).toBeInTheDocument();
    expect(within(llm).getByText("0.2")).toBeInTheDocument();
    expect(within(section("Transition LLM")).getByText("claude-haiku-4-5-20251001")).toBeInTheDocument();
  });

  it("hides every absent optional field rather than rendering null filler", () => {
    render(<PlaybookNodeInspector node={byId.done!} />);
    for (const name of ["Prompt", "Transitions", "Goto", "Action", "For each", "Output", "Timeouts", "LLM", "Transition LLM"]) {
      expect(screen.queryByRole("group", { name })).not.toBeInTheDocument();
    }
    expect(within(section("Flags")).getByText("terminal")).toBeInTheDocument();
    expect(screen.queryByText(/null/)).not.toBeInTheDocument();
  });
});
