import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type React from "react";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import type { ReviewWait } from "@aq/ts-client";

vi.mock("@xyflow/react", () => ({
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));

import { TaskCard } from "../TaskNode";
import { reviewHref, reviewStateLabel } from "../reviewWaitFormat";
import type { TaskNodeData } from "../types";
import { EPIC_DELIVERY } from "../../../testUtils/epicDelivery";

afterEach(cleanup);

const card = (status: string, over: Partial<TaskNodeData["hierarchy"]> = {}, extra: Partial<TaskNodeData> = {}): TaskNodeData => ({
  task: { id: "task-one", title: "Ship it", status, priority: 100 },
  gates: [],
  projectId: "p1",
  hierarchy: {
    parentId: null, parentTitle: null, depth: 0, childCount: 2,
    descendantCount: 4, completedCount: 3, runningCount: 0, blockedCount: 0,
    contextOnly: false, ...over,
  },
  ...extra,
});

describe("finished cards", () => {
  it("leaves the main card available as the node drag surface", () => {
    render(<TaskCard data={card("READY")} />);
    expect(screen.getByRole("button", { name: "Open task Ship it" })).not.toHaveClass("nodrag");
  });

  it("still shows a (full) progress bar once a card is settled", () => {
    render(<TaskCard data={card("COMPLETED")} />);
    expect(screen.getByText("3/4 descendants completed")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "3 of 4 done" })).toBeInTheDocument();
  });

  it("keeps the live variant while work is still running underneath", () => {
    render(<TaskCard data={card("COMPLETED", { runningCount: 1 })} />);
    expect(screen.getByRole("progressbar", { name: "3 of 4 done" })).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
  });

  it("keeps the live variant for a card that has not finished", () => {
    render(<TaskCard data={card("IN_PROGRESS")} />);
    expect(screen.getByRole("progressbar", { name: "3 of 4 done" })).toBeInTheDocument();
  });
});

describe("subtask progress", () => {
  it("renders a bar for a card with subtasks", () => {
    render(<TaskCard data={card("READY", {}, { subtasks: { total: 3, settled: 1 } })} />);
    expect(screen.getByText("1/3 subtasks")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "1 of 3 done" })).toBeInTheDocument();
  });

  it("renders no subtask bar for a card without subtasks", () => {
    render(<TaskCard data={card("READY", { descendantCount: 0 }, { subtasks: { total: 0, settled: 0 } })} />);
    expect(screen.queryByText(/subtasks/)).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});

describe("a container card is compact: you enter it, you never expand it", () => {
  it("offers an enter control and no expand/collapse toggle", async () => {
    const onFocus = vi.fn();
    render(<TaskCard data={card("READY", {}, { onFocus })} />);
    expect(screen.queryByRole("button", { name: /children of/i })).not.toBeInTheDocument();
    const enter = screen.getByRole("button", { name: "Enter Ship it" });
    await userEvent.click(enter);
    expect(onFocus).toHaveBeenCalledWith("task-one");
  });

  it("keeps the hidden count as information", () => {
    render(<TaskCard data={card("READY")} />);
    expect(screen.getByText("4 hidden")).toBeInTheDocument();
  });

  it("shows no enter control on a leaf card", () => {
    render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 })} />);
    expect(screen.queryByRole("button", { name: /^Enter/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open task Ship it" })).toBeInTheDocument();
  });
});

describe("phase header", () => {
  it("shows the phase order and label", () => {
    render(<TaskCard data={card("DEFINED", {}, { phase: { order: 2, label: "Build" } })} />);
    expect(screen.getByText("Phase 2 · Build")).toBeInTheDocument();
  });

  it("shows a lock glyph while the phase is gated", () => {
    render(<TaskCard data={card("DEFINED", {}, {
      task: { id: "task-one", title: "Ship it", status: "DEFINED", priority: 100, is_blocked: true },
      phase: { order: 1, label: "Foundation" },
    })} />);
    expect(screen.getByLabelText("Phase gated")).toBeInTheDocument();
  });

  it("omits the header for a non-phase task", () => {
    render(<TaskCard data={card("READY")} />);
    expect(screen.queryByText(/^Phase /)).not.toBeInTheDocument();
  });
});

describe("epic cards", () => {
  const epic = (status: string, delivery: TaskNodeData["delivery"]) =>
    card(status, { descendantCount: 5, completedCount: 5, childCount: 5 }, { delivery });

  it("separates implementation progress from a blocked delivery", () => {
    render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.missingReceipt)} />);
    expect(screen.getByText("5/5 tasks complete")).toBeInTheDocument();
    expect(screen.getByText("Integration blocked - final fix not collected")).toBeInTheDocument();
    expect(screen.getByText("Delivery blocked")).toBeInTheDocument();
    expect(screen.queryByText("PAUSED")).not.toBeInTheDocument();
    expect(screen.queryByText(/descendants completed/)).not.toBeInTheDocument();
  });

  it("explains the stored status of an integration hold in the status tooltip", () => {
    render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.strandedReservation)} />);
    expect(screen.getByText("Delivery blocked").closest("[title]")).toHaveAttribute(
      "title",
      "Delivery blocked · task status PAUSED (held by integration, not paused by anyone)",
    );
  });

  it("reads Paused only for an operator hold", () => {
    render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.manualPause)} />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Paused by operator")).toBeInTheDocument();
  });

  it("pulses only with evidence of active work", () => {
    const { container, unmount } = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.activeIntegration)} />);
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
    unmount();
    const queued = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.queuedVerifier)} />);
    expect(queued.container.querySelector(".animate-pulse")).toBeNull();
    expect(screen.getByText("Verification queued - waiting for a worker")).toBeInTheDocument();
  });

  it.each([
    ["approvalHold", "Awaiting approval"],
    ["delivered", "Delivered"],
    ["staleEvidence", "Verification status stale"],
    ["unavailable", "Delivery evidence unavailable"],
  ] as const)("%s shows its delivery label", (name, label) => {
    render(<TaskCard data={epic("COMPLETED", EPIC_DELIVERY[name])} />);
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });

  it("keeps the plain card for a node without a delivery projection", () => {
    render(<TaskCard data={card("PAUSED")} />);
    expect(screen.getByText("PAUSED")).toBeInTheDocument();
    expect(screen.getByText("3/4 descendants completed")).toBeInTheDocument();
  });
});

describe("review waits", () => {
  const wait = (over: Partial<ReviewWait> = {}): ReviewWait => ({
    review_id: "brisk-lantern-7", review_state: "in_review", review_kind: "spec",
    review_title: "Graph review badges", gate_id: "g-1", gate_type: "review", gate_status: "open",
    blocking: true, ...over,
  });
  const reviewCard = (waits: ReviewWait[], extra: Partial<TaskNodeData> = {}) =>
    card("DEFINED", { childCount: 0, descendantCount: 0 }, { reviewWaits: waits, ...extra });
  const inRouter = (ui: React.ReactElement) => render(
    <MemoryRouter initialEntries={["/graph"]}>
      <Routes>
        <Route path="/graph" element={ui} />
        <Route path="/reviews/:reviewId" element={<ReviewProbe />} />
      </Routes>
    </MemoryRouter>,
  );

  it("links a blocking review by id and state to its review page", () => {
    inRouter(<TaskCard data={reviewCard([wait()])} />);
    const link = screen.getByRole("link", { name: /Waiting on spec review brisk-lantern-7 \(pending\)/ });
    expect(link).toHaveAttribute("href", "/reviews/brisk-lantern-7");
    expect(link).toHaveTextContent("Awaiting review");
    expect(link).toHaveTextContent("brisk-lantern-7");
    expect(link).toHaveTextContent("pending");
    expect(link).toHaveAttribute("data-review-wait", "blocking");
  });

  it("marks the card itself as held by a review, not as a generic block", () => {
    const { container } = inRouter(<TaskCard data={reviewCard([wait()])} />);
    const shell = container.querySelector("[data-task-card]")!;
    expect(shell).toHaveAttribute("data-review-blocked");
    expect(shell).toHaveClass("border-violet-400");
    expect(shell).not.toHaveClass("border-yellow-300");
    expect(screen.getByTitle("DEFINED · waiting on review brisk-lantern-7 (pending)")).toBeInTheDocument();
  });

  it.each([
    ["in_review", "pending"],
    ["changes_requested", "changes requested"],
    ["rejected", "rejected"],
    ["withdrawn", "withdrawn"],
  ])("names a blocking %s review %s", (state, label) => {
    inRouter(<TaskCard data={reviewCard([wait({ review_state: state })])} />);
    expect(screen.getByRole("link", { name: new RegExp(`\\(${label}\\)`) })).toHaveTextContent(label);
  });

  it("still links an approved review that released the task, without the review border", () => {
    const { container } = inRouter(<TaskCard data={reviewCard([
      wait({ review_state: "approved", gate_status: "resolved", blocking: false }),
    ])} />);
    const link = screen.getByRole("link", { name: /Gated on spec review brisk-lantern-7 \(approved\)/ });
    expect(link).toHaveTextContent("Reviewed");
    expect(link).toHaveTextContent("approved");
    expect(link).toHaveAttribute("data-review-wait", "released");
    const shell = container.querySelector("[data-task-card]")!;
    expect(shell).not.toHaveAttribute("data-review-blocked");
    expect(shell).not.toHaveClass("border-violet-400");
  });

  it("follows the link without opening the task or reaching the canvas node", async () => {
    const onOpenTask = vi.fn();
    const nodeClick = vi.fn();
    inRouter(
      <div onClick={nodeClick}>
        <TaskCard data={reviewCard([wait()], { onOpenTask })} />
      </div>,
    );
    await userEvent.click(screen.getByRole("link", { name: /brisk-lantern-7/ }));
    expect(await screen.findByTestId("review-page")).toHaveTextContent("brisk-lantern-7");
    expect(onOpenTask).not.toHaveBeenCalled();
    expect(nodeClick).not.toHaveBeenCalled();
  });

  it("sits beside the card's open button, never inside it, and never starts a canvas drag", () => {
    inRouter(<TaskCard data={reviewCard([wait()])} />);
    const link = screen.getByRole("link", { name: /brisk-lantern-7/ });
    expect(link.closest("[role=button]")).toBeNull();
    expect(link).toHaveClass("nodrag", "nopan");
  });

  it("links the first wait and counts the rest", () => {
    inRouter(<TaskCard data={reviewCard([
      wait(),
      wait({ review_id: "calm-harbor-2", review_kind: "plan", review_state: "changes_requested", gate_id: "g-2" }),
    ])} />);
    const link = screen.getByRole("link", { name: /brisk-lantern-7 \(pending\).*\(1 more\)/ });
    expect(link).toHaveAttribute("href", "/reviews/brisk-lantern-7");
    expect(link).toHaveTextContent("+1");
    expect(link.getAttribute("title")).toContain("plan review calm-harbor-2 (changes requested)");
  });

  it("renders no review strip for a card with no review waits", () => {
    inRouter(<TaskCard data={reviewCard([])} />);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("encodes the review id into the route", () => {
    expect(reviewHref("odd id/1")).toBe("/reviews/odd%20id%2F1");
    expect(reviewStateLabel("some_new_state")).toBe("some new state");
  });
});

function ReviewProbe() {
  const { reviewId } = useParams();
  return <p data-testid="review-page">{reviewId}</p>;
}
