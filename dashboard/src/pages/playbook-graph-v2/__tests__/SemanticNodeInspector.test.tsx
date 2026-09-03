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
  listDownstream,
  openGate,
  runOverlay,
  runReceipts,
  unroutedEscalateNode,
} from "./fixtures";

const overlayFor = (stepId: string) => runOverlay.nodes!.find((node) => node.step_id === stepId)!;

afterEach(cleanup);

describe("SemanticNodeInspector", () => {
  it("shows inputs, outputs, outcomes and their targets for a command node", () => {
    render(<SemanticNodeInspector node={ensureReviewTask} />);
    const inputs = within(screen.getByRole("group", { name: "Inputs" }));
    // ``ensure_task`` is registered, so the rows carry the contract's argument
    // labels rather than the titled argument names.
    expect(inputs.getByText("Project")).toBeInTheDocument();
    expect(inputs.getByText("Title")).toBeInTheDocument();
    // The projector labels the result row with the binding it writes and
    // displays the same binding as the value, so both cells read "review".
    expect(within(screen.getByRole("group", { name: "Result" })).getAllByText("review")).toHaveLength(2);

    const outcomes = within(screen.getByRole("group", { name: "Outcomes" }));
    for (const outcome of ensureReviewTask.explanation.outcomes!) {
      expect(outcomes.getAllByText(outcome.label).length).toBeGreaterThan(0);
      // An outcome that ends the rule names the ending, not the step it lands on.
      const destination = outcome.terminal_outcome
        ? `ends the rule as ${outcome.terminal_outcome}`
        : outcome.target_title!;
      expect(outcomes.getAllByText(destination).length).toBeGreaterThan(0);
    }
  });

  it("says when a step has no presentation metadata rather than inventing an effect", () => {
    // The stub registry deliberately does not know ``list_tasks``, so the
    // projector renders that step canonically and declares no effect clauses.
    render(<SemanticNodeInspector node={listDownstream} />);
    expect(screen.getByRole("status")).toHaveTextContent(/No presentation metadata for this step/);
    expect(screen.queryByRole("group", { name: "Effects" })).not.toBeInTheDocument();
  });

  it("lists the effect clauses a registered command's contract declares", () => {
    render(<SemanticNodeInspector node={ensureReviewTask} />);
    const effects = within(screen.getByRole("group", { name: "Effects" }));
    expect(effects.getByText(/a task/)).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("lists the effects the projection derived for a non-command step", () => {
    render(<SemanticNodeInspector node={classifyRisk} />);
    const effects = within(screen.getByRole("group", { name: "Effects" }));
    expect(effects.getByText(/Invokes the reviewer profile/)).toBeInTheDocument();
    expect(effects.getByText(/Binds this step's result as risk/)).toBeInTheDocument();
  });

  it("shows profile, capabilities, budget and output schema for an AI node", () => {
    render(<SemanticNodeInspector node={classifyRisk} />);
    const ai = within(screen.getByRole("group", { name: "AI" }));
    expect(ai.getByText("reviewer")).toBeInTheDocument();
    expect(ai.getByText("intelligence class")).toBeInTheDocument();
    expect(ai.getByText("deep-high")).toBeInTheDocument();
    expect(ai.getByText("provider")).toBeInTheDocument();
    expect(ai.getByText("anthropic")).toBeInTheDocument();
    expect(ai.getByText("claude-opus-5")).toBeInTheDocument();
    expect(ai.getByText("demo_command")).toBeInTheDocument();
    // `harness_tools` and `plugin_tools` are both empty lists: deny-all, not
    // "unspecified", and the inspector must not collapse the two.
    expect(ai.getAllByText("none (deny-all)")).toHaveLength(2);
    expect(ai.getByText(classifyRisk.ai!.capability_fingerprint)).toBeInTheDocument();
    expect(ai.getByText(String(classifyRisk.ai!.budget.max_total_tokens))).toBeInTheDocument();
    expect(ai.getByText(/"risk"/)).toBeInTheDocument();
  });

  it("shows the delegation policy of an agent task node", () => {
    render(<SemanticNodeInspector node={escalateNode} />);
    const block = within(screen.getByRole("group", { name: "Delegated agent" }));
    expect(block.getByText("Delegation policy")).toBeInTheDocument();
    expect(block.getByText("child profile")).toBeInTheDocument();
    expect(block.getByText("wait for completion")).toBeInTheDocument();
    expect(block.getByText("yes")).toBeInTheDocument();
    expect(block.getByText(/parent ∩ child profile ∩ this narrowing/)).toBeInTheDocument();
    // This step declares no `capability_narrowing`, which is a different claim
    // from narrowing everything away, and the panel has to say which.
    expect(block.getByText("This step narrows nothing beyond the profile.")).toBeInTheDocument();
  });

  it("omits the routing rows when the backend resolved no provider or model", () => {
    render(<SemanticNodeInspector node={unroutedEscalateNode} />);
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
  });

  it("surfaces the node's own diagnostics without hiding its intent", () => {
    render(<SemanticNodeInspector node={listDownstream} />);
    expect(
      within(screen.getByRole("group", { name: "Diagnostics" })).getByText(/is not registered/),
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
