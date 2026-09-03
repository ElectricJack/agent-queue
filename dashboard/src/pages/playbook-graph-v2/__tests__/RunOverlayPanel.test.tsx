import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { PlaybookRunOverlayResponse, ReceiptDTO } from "../../../api/client";
import RunOverlayPanel from "../RunOverlayPanel";
import { artifact, runOverlay, runReceipts } from "./fixtures";

afterEach(cleanup);

const activeRun: PlaybookRunOverlayResponse = { ...runOverlay, artifact_is_active: true };

function receiptButton(name: string) {
  return screen.getByRole("button", { name });
}

describe("RunOverlayPanel", () => {
  it("pins the panel to the run's artifact and warns when it is not the active one", () => {
    render(<RunOverlayPanel overlay={runOverlay} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      `This run used an older artifact: ${runOverlay.artifact.artifact_sha256}`,
    );
    const summary = within(screen.getByRole("region", { name: "Run overlay" }).querySelector("dl")!);
    expect(summary.getByText("run-42")).toBeInTheDocument();
    expect(summary.getByText("completed")).toBeInTheDocument();
    expect(summary.getByText("sweep-on-spec-approved")).toBeInTheDocument();
    expect(
      summary.getByText(runOverlay.artifact.artifact_sha256.replace("sha256:", "").slice(0, 12)),
    ).toBeInTheDocument();
    expect(summary.getByText("5m 0s")).toBeInTheDocument();

    cleanup();
    render(<RunOverlayPanel overlay={activeRun} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("lists every step the run touched with its state, visits and last outcome", () => {
    render(<RunOverlayPanel overlay={runOverlay} />);
    const steps = within(screen.getByRole("list", { name: "Steps in this run" }));
    expect(steps.getAllByRole("listitem")).toHaveLength(runOverlay.nodes!.length);

    const gate = within(screen.getByRole("listitem", { name: "Step open-gate" }));
    expect(gate.getByText("completed")).toBeInTheDocument();
    expect(gate.getByText("3 visits")).toBeInTheDocument();
    expect(gate.getByText("last outcome: created")).toBeInTheDocument();

    // A visited step with no receipt says so rather than looking inspectable.
    const decision = within(screen.getByRole("listitem", { name: "Step check-gate" }));
    expect(decision.getByText("No receipt was returned for this step.")).toBeInTheDocument();
  });

  it("keeps both attempts of a retried loop iteration selectable", async () => {
    const user = userEvent.setup();
    render(<RunOverlayPanel overlay={runOverlay} />);

    // receipt_ids[0] is the *failed* attempt here, which is exactly the receipt
    // a panel that only ever renders the first one would still show — so the
    // assertion that matters is that the second attempt is reachable too.
    const chooser = within(screen.getByRole("group", { name: "Receipts for open-gate" }));
    expect(chooser.getAllByRole("button")).toHaveLength(4);

    await user.click(receiptButton("Receipt for open-gate, iteration 1 · attempt 1 · runtime_error"));
    const failed = within(screen.getByRole("region", { name: "Receipt detail" }));
    expect(failed.getByText("Outcome: runtime_error")).toBeInTheDocument();
    expect(failed.getByRole("alert")).toHaveTextContent("gate_create timed out talking to the daemon");
    expect(failed.getByText("1.5s")).toBeInTheDocument();
    expect(failed.getByText("gate:task-b")).toBeInTheDocument();

    await user.click(receiptButton("Receipt for open-gate, iteration 1 · attempt 2 · reused"));
    const retry = within(screen.getByRole("region", { name: "Receipt detail" }));
    expect(retry.getByText("Outcome: reused")).toBeInTheDocument();
    expect(retry.queryByRole("alert")).not.toBeInTheDocument();
    // Same idempotency key across both attempts: that is what makes the retry safe.
    expect(retry.getByText("gate:task-b")).toBeInTheDocument();
    expect(retry.getByText("gate-b")).toBeInTheDocument();
  });

  it("narrows a step's receipts to the chosen iteration and opens that iteration's first receipt", async () => {
    const user = userEvent.setup();
    render(<RunOverlayPanel overlay={runOverlay} />);

    const iterations = within(screen.getByRole("group", { name: "Iterations of for-each-task" }));
    expect(iterations.getAllByRole("button")).toHaveLength(3);
    expect(iterations.getByRole("button", { name: "Iteration 1: task-b" })).toHaveTextContent(
      "1: task-b · reused · 50s",
    );

    await user.click(iterations.getByRole("button", { name: "Iteration 2: task-c" }));
    expect(screen.getByRole("region", { name: "Receipt detail" })).toHaveTextContent("Outcome: created");
    expect(screen.getByRole("button", { name: "Iteration 2: task-c" })).toHaveAttribute("aria-pressed", "true");

    // A loop iteration's receipts are recorded against the body step, so the
    // narrowing has to reach open-gate's row rather than the foreach node's.
    const gate = within(screen.getByRole("group", { name: "Receipts for open-gate" }));
    expect(gate.getAllByRole("button")).toHaveLength(1);
    expect(gate.getByRole("button", { name: /iteration 2/ })).toHaveAttribute("aria-pressed", "true");

    // The foreach node holds no receipt of that iteration, so it is left whole
    // instead of being emptied by a filter that does not apply to it.
    const loop = within(screen.getByRole("group", { name: "Receipts for for-each-task" }));
    expect(loop.getAllByRole("button")).toHaveLength(1);

    // Choosing a receipt directly drops the narrowing and restores the row.
    await user.click(receiptButton("Receipt for open-gate, iteration 2 · attempt 1 · created"));
    expect(screen.getByRole("button", { name: "Iteration 2: task-c" })).toHaveAttribute("aria-pressed", "false");
    expect(
      within(screen.getByRole("group", { name: "Receipts for open-gate" })).getAllByRole("button"),
    ).toHaveLength(4);
  });

  it("shows a receipt's inputs, result, timing and selected edge", async () => {
    const user = userEvent.setup();
    render(<RunOverlayPanel overlay={runOverlay} />);
    await user.click(receiptButton("Receipt for list-downstream, attempt 1 · listed"));

    const detail = within(screen.getByRole("region", { name: "Receipt detail" }));
    expect(within(detail.getByRole("group", { name: "Receipt inputs" })).getByText("this event's project")).toBeInTheDocument();
    expect(within(detail.getByRole("group", { name: "Receipt result" })).getByText("3 downstream tasks")).toBeInTheDocument();
    expect(detail.getByText("10s")).toBeInTheDocument();
    expect(detail.getByText("sweep-on-spec-approved::list-downstream::listed")).toBeInTheDocument();
    expect(detail.getByText("sha256:task-list-v1")).toBeInTheDocument();
  });

  it("shows token usage, wait facts and cancellation when the receipt carries them", async () => {
    const user = userEvent.setup();
    const rich: ReceiptDTO = {
      receipt_id: "r-wait",
      step_id: "await-approval",
      rule_id: "review-on-task-completed",
      step_kind: "wait",
      attempt: 1,
      outcome: "approved",
      started_at: 1_756_000_000,
      completed_at: 1_756_000_012,
      token_usage: { input_tokens: 900, output_tokens: 120, total_tokens: 1020, estimated: true },
      wait: {
        wait_kind: "human",
        correlation_key: "review.task_id=42",
        registered_at: 1_756_000_000,
        deadline_at: 1_756_086_400,
        deadline_source: "wait",
        matched_at: 1_756_000_012,
        matched_event_id: "evt-7",
      },
      cancellation: { requested_at: 1_756_000_010, acknowledged_at: null, cancelled_child: true },
    };
    render(
      <RunOverlayPanel
        overlay={{
          ...runOverlay,
          nodes: [{ step_id: "await-approval", state: "completed", visit_count: 1, receipt_ids: ["r-wait"] }],
          receipts: [rich],
        }}
      />,
    );
    await user.click(receiptButton("Receipt for await-approval, attempt 1 · approved"));

    const detail = within(screen.getByRole("region", { name: "Receipt detail" }));
    const usage = within(detail.getByRole("group", { name: "Token usage" }));
    expect(usage.getByText("1020")).toBeInTheDocument();
    expect(usage.getByText("estimated")).toBeInTheDocument();

    const wait = within(detail.getByRole("group", { name: "Wait facts" }));
    expect(wait.getByText("review.task_id=42")).toBeInTheDocument();
    expect(wait.getByText(/by evt-7/)).toBeInTheDocument();

    const cancellation = within(detail.getByRole("group", { name: "Cancellation" }));
    expect(cancellation.getByText("not acknowledged")).toBeInTheDocument();
    expect(cancellation.getByText("yes")).toBeInTheDocument();
  });

  it("says a referenced receipt was capped instead of rendering an empty detail", async () => {
    const user = userEvent.setup();
    render(
      <RunOverlayPanel
        overlay={{
          ...runOverlay,
          receipts: runReceipts.filter((r) => r.receipt_id !== "r-b-1"),
          truncated: true,
          receipt_total: 6,
        }}
      />,
    );
    expect(screen.getByText("Showing the newest 5 of 6 receipts.")).toBeInTheDocument();

    // The iteration still knows about the dropped receipt, so selecting it must
    // name the gap rather than quietly showing the surviving attempt.
    await user.click(screen.getByRole("button", { name: "Iteration 1: task-b" }));
    expect(screen.getByText(/Receipt r-b-1 was not returned with this overlay/)).toBeInTheDocument();
  });

  it("reports an operator decision the run is blocked on", () => {
    render(
      <RunOverlayPanel
        overlay={{
          ...runOverlay,
          lifecycle: "paused",
          operator_decision: {
            step_id: "open-gate",
            attempt: 2,
            reason: "the command is not retry-safe and was interrupted",
            options: ["accept_outcome", "retry", "fail"],
            raised_at: 1_756_000_250,
          },
        }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Waiting on an operator decision for open-gate (attempt 2): the command is not retry-safe and was interrupted. Options: accept_outcome, retry, fail.",
    );
  });

  it("prompts rather than guessing when nothing is selected, and says so with no run", () => {
    const { rerender } = render(<RunOverlayPanel />);
    expect(screen.getByText("Select a run to inspect its exact artifact overlay.")).toBeInTheDocument();

    rerender(<RunOverlayPanel overlay={runOverlay} />);
    expect(screen.getByText("Select a receipt to see what the step actually did.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Receipt detail" })).not.toBeInTheDocument();

    rerender(
      <RunOverlayPanel overlay={{ ...runOverlay, artifact, nodes: [], receipts: [] }} />,
    );
    expect(screen.getByText("This run recorded no step state.")).toBeInTheDocument();
  });

  it("drops the receipt selection when the picker moves to another run", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<RunOverlayPanel overlay={runOverlay} />);
    await user.click(receiptButton("Receipt for list-downstream, attempt 1 · listed"));
    expect(screen.getByRole("region", { name: "Receipt detail" })).toBeInTheDocument();

    // A receipt id from one run means nothing in another; carrying it over
    // would report the next run as missing a receipt it never had.
    rerender(<RunOverlayPanel overlay={{ ...runOverlay, run_id: "run-99" }} />);
    expect(screen.queryByRole("region", { name: "Receipt detail" })).not.toBeInTheDocument();
    expect(screen.getByText("Select a receipt to see what the step actually did.")).toBeInTheDocument();
  });
});
