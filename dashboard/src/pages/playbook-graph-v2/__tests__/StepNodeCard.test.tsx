import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphNodeDTO } from "../../../api/client";
import { StepNodeCard, outgoingOutcomes } from "../StepNodeCard";
import { projectedGraph, projectedNode } from "./projected";

afterEach(cleanup);

/** Every node here is the backend's own projection of the §10.1 artifact, not
 *  a hand-authored stand-in: the compact card's job is to render what
 *  `project_graph` emits, and a fixture written to match the component cannot
 *  fail when the two disagree. */
const ensureReviewTask = projectedNode("ensure-review-task");
const classifyRisk = projectedNode("classify-risk");
const escalateNode = projectedNode("escalate");
const awaitApproval = projectedNode("await-approval");
const forEachTask = projectedNode("for-each-task");
const checkGate = projectedNode("check-gate");
const doneNode = projectedNode("done");

function card(node: GraphNodeDTO, selected = false) {
  return render(<StepNodeCard data={{ node }} selected={selected} />);
}

function keyData() {
  return within(screen.getByRole("list", { name: "Key data" })).getAllByRole("listitem");
}

describe("StepNodeCard", () => {
  it("leaves the card available as the node drag surface", () => {
    card(ensureReviewTask);
    expect(screen.getByRole("button", { name: /Inspect step Ensure a review task/ })).not.toHaveClass(
      "nodrag",
    );
  });

  it("renders the compact contract for a command step", () => {
    card(ensureReviewTask);
    // The authored title names this use of ``ensure_task``; the contract still
    // supplies the command's effect summary and presentation details.
    expect(screen.getByText("Ensure a review task")).toBeInTheDocument();
    expect(
      screen.getByText("Create the task, or reuse the one already keyed by this deduplication key."),
    ).toBeInTheDocument();
    expect(screen.getByText("command")).toBeInTheDocument();
  });

  it("renders an AI step's declared outcome choices and profile badge", () => {
    card(classifyRisk);
    expect(screen.getByText("Low, High")).toBeInTheDocument();
    expect(screen.getByTitle("Profile: reviewer")).toBeInTheDocument();
    expect(screen.getByTitle("Budget: 2 call(s), 8000 tokens")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
  });

  it("renders an agent task's objective and delegation badges", () => {
    card(escalateNode);
    expect(screen.getByText("Delegate a task to the reviewer profile and wait for it")).toBeInTheDocument();
    expect(screen.getByTitle("Waits: for completion")).toBeInTheDocument();
    // Whether a cancelled rule takes the child agent down with it (§6.2's
    // cancel_child column) is a fleet an operator has to clean up otherwise.
    expect(screen.getByTitle("On cancel: leaves the child running")).toBeInTheDocument();
  });

  it("renders a decision's condition summary and case count", () => {
    card(checkGate);
    expect(
      screen.getByText(
        "Take the first of 1 matching branch(es), otherwise For each downstream task — 2 cases",
      ),
    ).toBeInTheDocument();
  });

  it("renders a wait step's kind and what it awaits", () => {
    card(awaitApproval);
    expect(screen.getByText("human: Approve the review")).toBeInTheDocument();
    expect(screen.getByTitle("Timeout: 86400s")).toBeInTheDocument();
  });

  it("renders a loop's collection, item binding and failure policy", () => {
    card(forEachTask);
    expect(screen.getByText("downstream.tasks → task")).toBeInTheDocument();
    expect(screen.getByTitle("Failure policy: collect")).toBeInTheDocument();
  });

  it("renders a terminal step's outcome", () => {
    card(doneNode);
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("terminal")).toBeInTheDocument();
  });

  it("names the key inputs a step reads, from the projected explanation", () => {
    card(awaitApproval);
    expect(keyData().map((item) => item.getAttribute("data-input"))).toEqual([
      "Awaited",
      "Correlation key",
      null, // the output binding, which is not an input
    ]);
    expect(screen.getByTitle("Awaited: Approve the review")).toBeInTheDocument();
    expect(screen.getByTitle("Correlation key: review.task_id")).toBeInTheDocument();
  });

  it("bounds the inputs it prints and keeps the rest reachable", () => {
    // ``ensure-review-task`` reads three arguments; a card that printed all of
    // them would push its outcome ports off a fixed-height node.
    expect(ensureReviewTask.explanation.inputs).toHaveLength(3);
    card(ensureReviewTask);
    // Required rows come first, so the contract's one *optional* argument is
    // what overflows even though the step declares it first.
    expect(keyData().map((item) => item.getAttribute("data-input"))).toEqual([
      "Title",
      "Deduplication key",
      null, // "+1 more"
      null, // the output binding
    ]);
    const overflow = screen.getByText("+1 more");
    expect(overflow).toHaveAttribute("title", "Project: this event's project_id");
  });

  it("names the binding each step writes, and nothing when it writes none", () => {
    for (const [node, binding] of [
      [ensureReviewTask, "review"],
      [classifyRisk, "risk"],
      [escalateNode, "escalation"],
      [awaitApproval, "approval"],
    ] as const) {
      cleanup();
      card(node);
      const written = keyData().filter((item) => item.hasAttribute("data-binding"));
      expect(written.map((item) => item.getAttribute("data-binding"))).toEqual([binding]);
      expect(written[0]).toHaveAttribute("title", `Binds ${binding} (object)`);
    }
    cleanup();
    card(forEachTask);
    expect(keyData().some((item) => item.hasAttribute("data-binding"))).toBe(false);
  });

  it("shows no key-data line for a step that reads and writes nothing", () => {
    card(doneNode);
    expect(screen.queryByRole("list", { name: "Key data" })).not.toBeInTheDocument();
  });

  it("never renders the canonical typed step on the card", () => {
    card(ensureReviewTask);
    expect(screen.queryByText(/save_result_as/)).not.toBeInTheDocument();
    expect(screen.queryByText(/binding_ref/)).not.toBeInTheDocument();
  });

  it("renders one labelled outcome port per outgoing edge", () => {
    for (const node of [ensureReviewTask, classifyRisk, escalateNode, awaitApproval, forEachTask, checkGate]) {
      cleanup();
      card(node);
      const ports = within(screen.getByRole("list", { name: "Outcome ports" })).getAllByRole("listitem");
      expect(ports).toHaveLength(node.out_degree!);
      expect(ports.map((p) => p.getAttribute("data-port"))).toEqual(
        outgoingOutcomes(node).map((o) => o.outcome),
      );
      expect(ports.map((p) => p.textContent)).toEqual(
        outgoingOutcomes(node).map((o) => o.label || o.outcome),
      );
    }
  });

  it("gives a terminal step no outcome ports", () => {
    card(doneNode);
    expect(
      within(screen.getByRole("list", { name: "Outcome ports" })).queryAllByRole("listitem"),
    ).toHaveLength(0);
  });

  it("renders every badge the projector emits, for every projected node", () => {
    // The projector owns which chips a card carries (§6.2); the card owns none
    // of that vocabulary, so a chip it silently dropped would be invisible to
    // every per-kind test above.
    for (const node of projectedGraph.nodes ?? []) {
      cleanup();
      card(node);
      for (const badge of node.badges ?? []) {
        expect(screen.getByTitle(`${badge.label}: ${badge.value}`)).toBeInTheDocument();
      }
    }
  });

  it("is one button carrying an accessible name and pressed state", async () => {
    const onSelect = vi.fn();
    render(<StepNodeCard data={{ node: ensureReviewTask, onSelect }} selected />);
    const button = screen.getByRole("button", { name: "Inspect step Ensure a review task (command)" });
    expect(button).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(button);
    expect(onSelect).toHaveBeenCalledWith("ensure-review-task");
  });
});
