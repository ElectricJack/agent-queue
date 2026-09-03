import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AdvancedNodeDetail from "../AdvancedNodeDetail";
import { ensureReviewTask } from "./fixtures";

afterEach(cleanup);

describe("AdvancedNodeDetail", () => {
  it("exposes the typed step, resolved inputs, ids, fingerprints and schema", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const identity = within(screen.getByRole("group", { name: "Identity" }));
    expect(identity.getByText("ensure-review-task")).toBeInTheDocument();
    expect(identity.getByText("review-on-task-completed")).toBeInTheDocument();
    expect(identity.getByText("sha256:c0ffee")).toBeInTheDocument();
    expect(identity.getByText("sha256:ensure-task-v3")).toBeInTheDocument();

    expect(within(screen.getByRole("group", { name: "Typed step" })).getByText(/"ensure_task"/)).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Result schema" })).getByText(/task_id/)).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Retry" })).getByText("2")).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Idempotency" })).getByText("<run_id>:<step_id>:<attempt>")).toBeInTheDocument();
  });

  it("lists the redaction decision for every field the contract declared", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const table = within(screen.getByRole("group", { name: "Redaction" }));
    expect(table.getByText("auth_token")).toBeInTheDocument();
    expect(table.getByText("redacted")).toBeInTheDocument();
    expect(table.getByText("safe")).toBeInTheDocument();
  });

  it("renders a redacted value without its canonical payload", () => {
    render(<AdvancedNodeDetail node={ensureReviewTask} />);
    const resolved = within(screen.getByRole("group", { name: "Resolved inputs" }));
    expect(resolved.getByText("«redacted»")).toBeInTheDocument();
    // The safe value keeps its canonical JSON; the redacted one has none to show.
    expect(resolved.getAllByRole("generic").some((n) => n.textContent?.includes("proj-7"))).toBe(true);
    expect(resolved.queryByText(/auth/)).not.toBeInTheDocument();
  });
});
