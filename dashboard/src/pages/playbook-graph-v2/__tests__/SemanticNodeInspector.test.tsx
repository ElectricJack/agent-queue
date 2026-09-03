import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import SemanticNodeInspector from "../SemanticNodeInspector";
import {
  awaitApproval,
  checkGate,
  classifyRisk,
  ensureReviewTask,
  escalateNode,
  forEachTask,
  openGate,
  runOverlay,
  runReceipts,
} from "./fixtures";

const overlayFor = (stepId: string) => runOverlay.nodes!.find((node) => node.step_id === stepId)!;

afterEach(cleanup);

describe("SemanticNodeInspector", () => {
  it("shows inputs, outputs, outcomes and their targets for a command node", () => {
    render(<SemanticNodeInspector node={ensureReviewTask} />);
    const inputs = within(screen.getByRole("group", { name: "Inputs" }));
    expect(inputs.getByText("this event's project")).toBeInTheDocument();
    expect(inputs.getByText("Review: <event.title>")).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Result" })).getByText("review")).toBeInTheDocument();

    const outcomes = within(screen.getByRole("group", { name: "Outcomes" }));
    for (const outcome of ensureReviewTask.explanation.outcomes!) {
      expect(outcomes.getByText(outcome.label)).toBeInTheDocument();
      expect(outcomes.getAllByText(outcome.target_title!).length).toBeGreaterThan(0);
    }
    expect(within(screen.getByRole("group", { name: "Effects" })).getByText(/Creates a review task/)).toBeInTheDocument();
  });

  it("shows profile, capabilities, budget and output schema for an AI node", () => {
    render(<SemanticNodeInspector node={classifyRisk} />);
    const ai = within(screen.getByRole("group", { name: "AI" }));
    expect(ai.getByText("reviewer")).toBeInTheDocument();
    expect(ai.getByText("intelligence class")).toBeInTheDocument();
    expect(ai.getByText("deep")).toBeInTheDocument();
    expect(ai.getByText("provider")).toBeInTheDocument();
    expect(ai.getByText("anthropic")).toBeInTheDocument();
    expect(ai.getByText("claude-opus-5")).toBeInTheDocument();
    expect(ai.getByText("Read, Grep")).toBeInTheDocument();
    expect(ai.getByText("task_show")).toBeInTheDocument();
    expect(ai.getByText("none (deny-all)")).toBeInTheDocument();
    expect(ai.getByText("sha256:cap-reviewer-1")).toBeInTheDocument();
    expect(ai.getByText("8000")).toBeInTheDocument();
    expect(ai.getByText(/"risk"/)).toBeInTheDocument();
  });

  it("shows the delegation policy and its capability narrowing for an agent task node", () => {
    render(<SemanticNodeInspector node={escalateNode} />);
    const block = within(screen.getByRole("group", { name: "Delegated agent" }));
    expect(block.getByText("Delegation policy")).toBeInTheDocument();
    expect(block.getByText("supervisor principal")).toBeInTheDocument();
    expect(block.getByText(/parent ∩ child profile ∩ this narrowing/)).toBeInTheDocument();
    // `null` for a namespace means "not narrowed here"; `[]` means deny-all.
    expect(block.getByText("not narrowed here")).toBeInTheDocument();
    expect(block.getAllByText("none (deny-all)").length).toBeGreaterThan(0);
  });

  it("omits the routing rows when the backend resolved no provider or model", () => {
    render(<SemanticNodeInspector node={escalateNode} />);
    const block = within(screen.getByRole("group", { name: "Delegated agent" }));
    expect(block.queryByText("intelligence class")).not.toBeInTheDocument();
    expect(block.queryByText("provider")).not.toBeInTheDocument();
    expect(block.queryByText("model")).not.toBeInTheDocument();
  });

  it("shows wait kind, correlation key and timeout", () => {
    render(<SemanticNodeInspector node={awaitApproval} />);
    const wait = within(screen.getByRole("group", { name: "Wait" }));
    expect(wait.getByText("human")).toBeInTheDocument();
    expect(wait.getByText("Approve the review")).toBeInTheDocument();
    expect(wait.getByText("review.task_id")).toBeInTheDocument();
    expect(wait.getByText("86400s")).toBeInTheDocument();
    expect(wait.getByText("review-unavailable")).toBeInTheDocument();
  });

  it("shows loop collection, item binding and failure policy", () => {
    render(<SemanticNodeInspector node={forEachTask} />);
    const loop = within(screen.getByRole("group", { name: "Loop" }));
    expect(loop.getByText("downstream.tasks")).toBeInTheDocument();
    expect(loop.getByText("task")).toBeInTheDocument();
    expect(loop.getByText("collect")).toBeInTheDocument();
    expect(loop.getByText("open-gate")).toBeInTheDocument();
    expect(loop.getByText("sweep-done")).toBeInTheDocument();
  });

  it("links to the authoring markdown with path and line range", () => {
    render(<SemanticNodeInspector node={ensureReviewTask} />);
    const source = within(screen.getByRole("group", { name: "Source" }));
    expect(source.getByText("system/playbooks/default-pipeline.md:20-27")).toBeInTheDocument();
    expect(source.getByText(/Open review for a completed task/)).toBeInTheDocument();
  });

  it("surfaces the node's own diagnostics without hiding its intent", () => {
    render(<SemanticNodeInspector node={checkGate} />);
    expect(
      within(screen.getByRole("group", { name: "Diagnostics" })).getByText(/is the case intentional/),
    ).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Outcomes" })).toBeInTheDocument();
  });

  it("renders no inspector when nothing is selected", () => {
    const { container } = render(<SemanticNodeInspector node={null} />);
    expect(screen.queryByRole("complementary", { name: "Node inspector" })).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps Advanced out of the default view and behind a toggle", async () => {
    render(<SemanticNodeInspector node={ensureReviewTask} />);
    expect(screen.queryByTestId("advanced-detail")).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Advanced" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(screen.getByTestId("advanced-detail")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide advanced" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows no run block at all when no run is overlaid", () => {
    render(<SemanticNodeInspector node={openGate} />);
    expect(screen.queryByRole("group", { name: "Run" })).not.toBeInTheDocument();
  });

  it("shows the step's run state and keeps every attempt of a retry selectable", async () => {
    const user = userEvent.setup();
    render(<SemanticNodeInspector node={openGate} overlay={overlayFor("open-gate")} receipts={runReceipts} />);
    const run = within(screen.getByRole("group", { name: "Run" }));
    expect(run.getByText("completed")).toBeInTheDocument();
    expect(run.getByText("3")).toBeInTheDocument();
    expect(run.getByText("created")).toBeInTheDocument();

    // One definition node on the canvas; four receipts under it here.
    const chooser = within(run.getByRole("group", { name: "Receipts" }));
    expect(chooser.getAllByRole("button")).toHaveLength(4);

    await user.click(run.getByRole("button", { name: "Receipt for open-gate, iteration 1 · attempt 1 · runtime_error" }));
    expect(screen.getByRole("region", { name: "Receipt detail" })).toHaveTextContent("Outcome: runtime_error");
    expect(screen.getByRole("alert")).toHaveTextContent("gate_create timed out talking to the daemon");

    await user.click(run.getByRole("button", { name: "Receipt for open-gate, iteration 1 · attempt 2 · reused" }));
    expect(screen.getByRole("region", { name: "Receipt detail" })).toHaveTextContent("Outcome: reused");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("selects a loop iteration and opens the receipt its body step recorded", async () => {
    const user = userEvent.setup();
    render(
      <SemanticNodeInspector node={forEachTask} overlay={overlayFor("for-each-task")} receipts={runReceipts} />,
    );
    const iterations = within(screen.getByRole("group", { name: "Iterations" }));
    expect(iterations.getAllByRole("button")).toHaveLength(3);
    expect(iterations.getByRole("button", { name: "Iteration 1: task-b" })).toHaveTextContent(
      "1: task-b · reused · 50s",
    );

    await user.click(iterations.getByRole("button", { name: "Iteration 1: task-b" }));
    const detail = within(screen.getByRole("region", { name: "Receipt detail" }));
    // The iteration's first receipt is the failed attempt on the body step —
    // a different step id from the foreach node being inspected.
    expect(detail.getByText("Outcome: runtime_error")).toBeInTheDocument();
    expect(detail.getByText("open-gate")).toBeInTheDocument();
  });

  it("starts the receipt selection over when a different step is inspected", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <SemanticNodeInspector node={openGate} overlay={overlayFor("open-gate")} receipts={runReceipts} />,
    );
    await user.click(screen.getByRole("button", { name: "Receipt for open-gate, iteration 0 · attempt 1 · created" }));
    expect(screen.getByRole("region", { name: "Receipt detail" })).toBeInTheDocument();

    rerender(
      <SemanticNodeInspector node={forEachTask} overlay={overlayFor("for-each-task")} receipts={runReceipts} />,
    );
    expect(screen.queryByRole("region", { name: "Receipt detail" })).not.toBeInTheDocument();
    expect(screen.getByText("Select a receipt to see what the step actually did.")).toBeInTheDocument();
  });

  it("says the overlay returned no receipt for a step it did visit", () => {
    render(<SemanticNodeInspector node={checkGate} overlay={overlayFor("check-gate")} receipts={runReceipts} />);
    const run = within(screen.getByRole("group", { name: "Run" }));
    expect(run.getByText("No receipt for this step came back with the overlay.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Receipt detail" })).not.toBeInTheDocument();
  });

  it("keeps the chosen receipt across a live run's re-fetch of the same run", async () => {
    const user = userEvent.setup();
    const props = { node: openGate, receipts: runReceipts, runId: runOverlay.run_id };
    const { rerender } = render(<SemanticNodeInspector {...props} overlay={overlayFor("open-gate")} />);
    await user.click(
      screen.getByRole("button", { name: "Receipt for open-gate, iteration 1 · attempt 2 · reused" }),
    );
    expect(screen.getByRole("region", { name: "Receipt detail" })).toHaveTextContent("Outcome: reused");

    // A polled overlay is a new object every few seconds; only a new run id is
    // a reason to throw away what the operator is reading.
    rerender(<SemanticNodeInspector {...props} overlay={{ ...overlayFor("open-gate") }} />);
    expect(screen.getByRole("region", { name: "Receipt detail" })).toHaveTextContent("Outcome: reused");

    rerender(
      <SemanticNodeInspector {...props} runId="run-99" overlay={{ ...overlayFor("open-gate") }} />,
    );
    expect(screen.queryByRole("region", { name: "Receipt detail" })).not.toBeInTheDocument();
  });
});
