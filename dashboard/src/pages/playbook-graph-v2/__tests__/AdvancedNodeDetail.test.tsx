import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AdvancedNodeDetail from "../AdvancedNodeDetail";
import { classifyRisk, contractedEnsureReviewTask, ensureReviewTask } from "./fixtures";

afterEach(cleanup);

describe("AdvancedNodeDetail", () => {
  it("exposes the typed step, its ids and the inputs the projection resolved", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.getByText("ensure-review-task")).toBeInTheDocument();
    expect(identity.getByText("review-on-task-completed")).toBeInTheDocument();
    expect(identity.getByText("command")).toBeInTheDocument();

    expect(within(screen.getByRole("group", { name: "Typed step" })).getByText(/"ensure_task"/)).toBeInTheDocument();
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("Project Id")).toBeInTheDocument();
    // Nothing is resolved without a run, and the projector says so in the value
    // rather than inventing one.
    expect(resolved.getAllByText("(unresolved)")).toHaveLength(3);
  });

  it("omits the fingerprint rows the projection had no contract to fill", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.queryByText("contract fingerprint")).not.toBeInTheDocument();
    expect(identity.queryByText("execution fingerprint")).not.toBeInTheDocument();
    // An unregistered command claims no idempotency rather than none at all.
    const idempotency = within(screen.getByRole("group", { name: "Idempotency" }));
    expect(idempotency.getByText("no")).toBeInTheDocument();
    expect(idempotency.getByText("—")).toBeInTheDocument();
  });

  it("shows both fingerprints, the retry policy and the key template of a registered command", () => {
    render(<AdvancedNodeDetail node={contractedEnsureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.getAllByText(`sha256:${"11".repeat(32)}`)).toHaveLength(2);

    expect(within(screen.getByRole("group", { name: "Result schema" })).getByText(/task_id/)).toBeInTheDocument();
    const retry = within(screen.getByRole("group", { name: "Retry" }));
    expect(retry.getByText("2")).toBeInTheDocument();
    expect(retry.getByText("5s")).toBeInTheDocument();
    expect(retry.getByText("runtime_error")).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "Idempotency" })).getByText('"review-of-<event.task_id>"'),
    ).toBeInTheDocument();
  });

  it("lists the redaction decision for every field the contract declared", () => {
    render(<AdvancedNodeDetail node={contractedEnsureReviewTask} />);
    const table = within(screen.getByRole("group", { name: "Redaction" }));
    expect(table.getByText("auth_token")).toBeInTheDocument();
    expect(table.getByText("redacted")).toBeInTheDocument();
    expect(table.getAllByText("safe")).toHaveLength(3);
  });

  it("renders a redacted value without its canonical payload", () => {
    render(<AdvancedNodeDetail node={contractedEnsureReviewTask} />);
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("(redacted)")).toBeInTheDocument();
    // A redacted row has no canonical JSON to disclose, so nothing names the
    // argument's value anywhere in the block.
    expect(resolved.queryByText(/"auth/)).not.toBeInTheDocument();
  });

  it("discloses the canonical payload of a value that was not redacted", () => {
    render(<AdvancedNodeDetail node={classifyRisk} />);
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("Assess the review risk of task this event's title")).toBeInTheDocument();
    expect(resolved.getByText(/"event_ref"/)).toBeInTheDocument();
  });
});
