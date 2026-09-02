import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import NodeExplanationCard from "../NodeExplanationCard";
import {
  createReviewExplanation,
  gateDownstreamExplanation,
  redactedExplanation,
} from "./fixtures";

afterEach(cleanup);

describe("NodeExplanationCard", () => {
  it("renders nothing when the node carries no explanation", () => {
    const { container } = render(<NodeExplanationCard explanation={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("leads with the contract's title", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    expect(screen.getByRole("heading", { name: "Ensure a review task exists" })).toBeInTheDocument();
  });

  it("renders each declared effect as a sentence", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    expect(
      within(screen.getByRole("group", { name: "Effects" })).getByText(
        'Create or reuse a task keyed by "dedup_key"',
      ),
    ).toBeInTheDocument();
  });

  it("shows a conditional effect's predicate alongside it", () => {
    render(<NodeExplanationCard explanation={gateDownstreamExplanation} />);
    const effects = screen.getByRole("group", { name: "Effects" });
    expect(within(effects).getByText("Block the waiting tasks until the gate resolves")).toBeInTheDocument();
    expect(within(effects).getByText("when waiter_task_ids is provided")).toBeInTheDocument();
  });

  it("renders an argument as its human-facing value, not its raw expression", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    const inputs = screen.getByRole("group", { name: "Inputs" });
    expect(within(inputs).getByText("Project")).toBeInTheDocument();
    expect(within(inputs).getByText("this event's project")).toBeInTheDocument();
    expect(screen.queryByText("{{event.project_id}}")).not.toBeInTheDocument();
  });

  it("marks required arguments", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    const inputs = screen.getByRole("group", { name: "Inputs" });
    expect(within(inputs).getAllByText("required").length).toBe(2);
  });

  it("shows the redaction placeholder and never the raw value for a sensitive argument", () => {
    render(<NodeExplanationCard explanation={redactedExplanation} />);
    expect(screen.getByText("[redacted]")).toBeInTheDocument();
    expect(screen.queryByText("Webhook token", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(/hunter2/)).not.toBeInTheDocument();
  });

  it("names the result binding and the fields it exposes", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    const result = screen.getByRole("group", { name: "Result" });
    expect(within(result).getByText('Save as "review"')).toBeInTheDocument();
    expect(within(result).getByText("task_id")).toBeInTheDocument();
    expect(within(result).getByText("created")).toBeInTheDocument();
  });

  it("renders every outcome with the node it leads to", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    const outcomes = screen.getByRole("group", { name: "Outcomes" });
    expect(within(outcomes).getByText("Success")).toBeInTheDocument();
    expect(within(outcomes).getByText("per-task-review-link-discovered-from")).toBeInTheDocument();
    expect(within(outcomes).getByText("Failure")).toBeInTheDocument();
    expect(within(outcomes).getByText("per-task-review-done")).toBeInTheDocument();
  });

  it("renders the loop source and its item binding for a for_each node", () => {
    render(<NodeExplanationCard explanation={gateDownstreamExplanation} />);
    const loop = screen.getByRole("group", { name: "Repeats for" });
    expect(within(loop).getByText("each item in downstream.tasks")).toBeInTheDocument();
    expect(within(loop).getByText("dep")).toBeInTheDocument();
  });

  it("omits the loop section when the node does not loop", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    expect(screen.queryByRole("group", { name: "Repeats for" })).not.toBeInTheDocument();
  });

  it("states the idempotency and retry guarantees in a footer", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    const footer = screen.getByRole("group", { name: "Guarantees" });
    expect(
      within(footer).getByText("Repeating with the same deduplication key reuses the existing task"),
    ).toBeInTheDocument();
    expect(within(footer).getByText("Safe to retry")).toBeInTheDocument();
  });

  it("lists every executable argument the contract could not render richly", () => {
    render(<NodeExplanationCard explanation={gateDownstreamExplanation} />);
    expect(within(screen.getByRole("group", { name: "Other fields" })).getByText("reason")).toBeInTheDocument();
  });

  it("omits the other-fields section when nothing was left unrendered", () => {
    render(<NodeExplanationCard explanation={createReviewExplanation} />);
    expect(screen.queryByRole("group", { name: "Other fields" })).not.toBeInTheDocument();
  });
});
