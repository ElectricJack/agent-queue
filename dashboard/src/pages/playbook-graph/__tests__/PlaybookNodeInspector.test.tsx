import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import PlaybookNodeInspector from "../PlaybookNodeInspector";
import {
  REVIEW_PROMPT,
  explanationNode,
  graph,
  loopExplanationNode,
  pipelineGraph,
  uncontractedNode,
} from "./fixtures";

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

  it("renders a pipeline action payload in its labelled inspector section", () => {
    render(<PlaybookNodeInspector node={pipelineGraph.nodes![0]!} />);
    const action = section("Action");
    expect(within(action).getByText(/"command": "ensure_task"/)).toBeInTheDocument();
    expect(within(action).getByText(/"on_success": "review-ready"/)).toBeInTheDocument();
    expect(within(action).getByText(/"on_failure": "review-failed"/)).toBeInTheDocument();
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

describe("PlaybookNodeInspector contract intent", () => {
  function advanced(): HTMLDetailsElement {
    const disclosure = screen.getByText("Advanced").closest("details");
    expect(disclosure).not.toBeNull();
    return disclosure as HTMLDetailsElement;
  }

  it("shows the contract's intent instead of the raw action for a contracted node", () => {
    render(<PlaybookNodeInspector node={explanationNode} />);
    const intent = section("Intent");
    expect(within(intent).getByRole("heading", { name: "Ensure a task exists" })).toBeInTheDocument();
    expect(within(intent).getByText('Create or reuse a task keyed by "dedup_key"')).toBeInTheDocument();
    expect(within(intent).getByText("this event's project")).toBeInTheDocument();
  });

  it("keeps the raw compiled payloads reachable under a collapsed Advanced disclosure", () => {
    render(<PlaybookNodeInspector node={explanationNode} />);
    const disclosure = advanced();
    expect(disclosure.open).toBe(false);
    expect(within(disclosure).getByText(/"command": "ensure_task"/)).toBeInTheDocument();
    // The template expressions are Advanced material and appear nowhere else.
    expect(within(disclosure).getByText(/\{\{event\.project_id\}\}/)).toBeInTheDocument();
    expect(within(section("Intent")).queryByText(/\{\{event\.project_id\}\}/)).not.toBeInTheDocument();
  });

  it("renders the loop and result intent for a for_each node", () => {
    render(<PlaybookNodeInspector node={loopExplanationNode} />);
    const intent = section("Intent");
    expect(within(intent).getByText("each item in downstream.tasks")).toBeInTheDocument();
    expect(within(intent).getByText("when waiter_task_ids is provided")).toBeInTheDocument();
  });

  it("opens the Advanced disclosure for an uncontracted node so nothing is hidden", () => {
    render(<PlaybookNodeInspector node={uncontractedNode} />);
    expect(screen.queryByRole("group", { name: "Intent" })).not.toBeInTheDocument();
    const disclosure = advanced();
    expect(disclosure.open).toBe(true);
    expect(within(disclosure).getByText(/"command": "ensure_task"/)).toBeInTheDocument();
  });

  it("shows no Advanced disclosure for a node with no compiled payloads at all", () => {
    render(<PlaybookNodeInspector node={byId.escalate!} />);
    expect(screen.queryByText("Advanced")).not.toBeInTheDocument();
  });
});
