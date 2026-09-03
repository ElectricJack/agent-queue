import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AdvancedNodeDetail from "../AdvancedNodeDetail";
import { classifyRisk, ensureReviewTask, listDownstream } from "./fixtures";

afterEach(cleanup);

/** Both nodes below are projected, not hand-authored: `ensure-review-task`
 *  invokes a command the backend's stub registry models (and declares one
 *  sensitive argument on), `list-downstream` invokes one it deliberately does
 *  not, so the panel's contract branch and its no-contract branch are both
 *  asserted against bytes `project_graph` actually wrote. */
describe("AdvancedNodeDetail", () => {
  it("exposes the typed step, its ids and the inputs the projection resolved", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.getByText("ensure-review-task")).toBeInTheDocument();
    expect(identity.getByText("review-on-task-completed")).toBeInTheDocument();
    expect(identity.getByText("command")).toBeInTheDocument();

    expect(within(screen.getByRole("group", { name: "Typed step" })).getByText(/"ensure_task"/)).toBeInTheDocument();
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    // The contract supplies the argument labels; the projection resolves the
    // shape of each value without a run.
    expect(resolved.getByText("Project")).toBeInTheDocument();
    expect(resolved.getByText("this event's project_id")).toBeInTheDocument();
    expect(resolved.getByText("Review: this event's title")).toBeInTheDocument();
  });

  it("omits the fingerprint rows the projection had no contract to fill", () => {
    render(<AdvancedNodeDetail node={listDownstream} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.queryByText("contract fingerprint")).not.toBeInTheDocument();
    expect(identity.queryByText("execution fingerprint")).not.toBeInTheDocument();
    // Nothing is resolved without a run, and an unregistered command has no
    // contract to name its arguments either, so the projector says so in the
    // value rather than inventing one.
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getAllByText("(unresolved)")).toHaveLength(2);
    // An unregistered command claims no idempotency rather than none at all.
    const idempotency = within(screen.getByRole("group", { name: "Idempotency" }));
    expect(idempotency.getByText("no")).toBeInTheDocument();
    expect(idempotency.getByText("—")).toBeInTheDocument();
  });

  it("shows both fingerprints, the retry policy and the key template of a registered command", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.getAllByText(ensureReviewTask.explanation.contract_fingerprint!)).toHaveLength(2);

    expect(within(screen.getByRole("group", { name: "Result schema" })).getByText(/task_id/)).toBeInTheDocument();
    const retry = within(screen.getByRole("group", { name: "Retry" }));
    expect(retry.getByText("2")).toBeInTheDocument();
    expect(retry.getByText("5s")).toBeInTheDocument();
    expect(retry.getByText("runtime_error")).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "Idempotency" })).getByText(
        ensureReviewTask.advanced.idempotency!.key_template!,
      ),
    ).toBeInTheDocument();
  });

  it("lists the redaction decision for every field the contract declared", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const table = within(screen.getByRole("group", { name: "Redaction" }));
    expect(table.getByText("dedup_key")).toBeInTheDocument();
    expect(table.getByText("redacted")).toBeInTheDocument();
    expect(table.getAllByText("safe")).toHaveLength(2);
  });

  it("renders a redacted value without its canonical payload", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("(redacted)")).toBeInTheDocument();
    // A redacted row has no canonical JSON to disclose, so nothing names the
    // argument's value anywhere in the block.
    expect(resolved.queryByText(/"dedup_key"/)).not.toBeInTheDocument();
    expect(resolved.queryByText(/review-of-/)).not.toBeInTheDocument();
  });

  it("discloses the canonical payload of a value that was not redacted", () => {
    render(<AdvancedNodeDetail node={classifyRisk} />);
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("Assess the review risk of task this event's title")).toBeInTheDocument();
    expect(resolved.getByText(/"event_ref"/)).toBeInTheDocument();
  });
});
