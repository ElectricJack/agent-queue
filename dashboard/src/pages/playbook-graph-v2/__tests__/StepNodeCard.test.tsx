import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphNodeDTO } from "../../../api/client";
import { StepNodeCard, outgoingOutcomes } from "../StepNodeCard";
import {
  awaitApproval,
  checkGate,
  classifyRisk,
  doneNode,
  ensureReviewTask,
  escalateNode,
  forEachTask,
} from "./fixtures";

afterEach(cleanup);

function card(node: GraphNodeDTO, selected = false) {
  return render(<StepNodeCard data={{ node }} selected={selected} />);
}

describe("StepNodeCard", () => {
  it("renders the compact contract for a command step", () => {
    card(ensureReviewTask);
    expect(screen.getByText("Ensure a review task")).toBeInTheDocument();
    expect(screen.getByText("Create or reuse the matching review task")).toBeInTheDocument();
    expect(screen.getByText("command")).toBeInTheDocument();
    expect(screen.getByTitle("idempotent: dedup_key")).toBeInTheDocument();
    expect(screen.getByTitle("retry: 2 attempts")).toBeInTheDocument();
  });

  it("renders an AI step's declared outcome choices and profile badge", () => {
    card(classifyRisk);
    expect(screen.getByText("low, high")).toBeInTheDocument();
    expect(screen.getByTitle("profile: reviewer")).toBeInTheDocument();
    expect(screen.getByTitle("budget: 8000 tokens")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
  });

  it("renders an agent task's objective and delegation badges", () => {
    card(escalateNode);
    expect(screen.getByText("Re-review the change and record the riskiest line")).toBeInTheDocument();
    expect(screen.getByTitle("wait: waits for completion")).toBeInTheDocument();
  });

  it("renders a decision's condition summary and case count", () => {
    card(checkGate);
    expect(screen.getByText("Branch on whether the gate was newly created — 2 cases")).toBeInTheDocument();
  });

  it("renders a wait step's kind and what it awaits", () => {
    card(awaitApproval);
    expect(screen.getByText("human: Approve the review")).toBeInTheDocument();
    expect(screen.getByTitle("timeout: 86400s")).toBeInTheDocument();
  });

  it("renders a loop's collection, item binding and failure policy", () => {
    card(forEachTask);
    expect(screen.getByText("downstream.tasks → task")).toBeInTheDocument();
    expect(screen.getByTitle("failure policy: collect")).toBeInTheDocument();
  });

  it("renders a terminal step's outcome", () => {
    card(doneNode);
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("terminal")).toBeInTheDocument();
  });

  it("never renders the canonical typed step on the card", () => {
    card(ensureReviewTask);
    expect(screen.queryByText(/ensure_task/)).not.toBeInTheDocument();
    expect(screen.queryByText(/save_result_as/)).not.toBeInTheDocument();
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

  it("is one button carrying an accessible name and pressed state", async () => {
    const onSelect = vi.fn();
    render(<StepNodeCard data={{ node: ensureReviewTask, onSelect }} selected />);
    const button = screen.getByRole("button", { name: "Inspect step Ensure a review task (command)" });
    expect(button).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(button);
    expect(onSelect).toHaveBeenCalledWith("ensure-review-task");
  });
});
