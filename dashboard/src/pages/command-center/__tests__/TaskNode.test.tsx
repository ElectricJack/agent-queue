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

describe("card anatomy (§3.1)", () => {
  it("leaves the main card available as the node drag surface", () => {
    render(<TaskCard data={card("READY")} />);
    expect(screen.getByRole("button", { name: "Open task Ship it" })).not.toHaveClass("nodrag");
  });

  it("names the status in plain words on the pill, the stored status in its tooltip", () => {
    render(<TaskCard data={card("DEFINED", { childCount: 0, descendantCount: 0 })} />);
    expect(screen.getByText("Not started").closest("[title]")).toHaveAttribute("title", "DEFINED");
    expect(screen.queryByText("DEFINED")).not.toBeInTheDocument();
  });

  it("reads an open task the daemon reports blocked as Blocked, with a left bar", () => {
    const { container } = render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 }, {
      task: { id: "task-one", title: "Ship it", status: "READY", priority: 100, is_blocked: true },
    })} />);
    expect(screen.getByText("Blocked").closest("[title]")).toHaveAttribute("title", "READY · blocked by dependencies or gates");
    expect(container.querySelector("[data-task-card]")).toHaveClass("border-l-4", "border-l-g-blocked");
  });

  it("stripes and pulses only a running card", () => {
    const running = render(<TaskCard data={card("IN_PROGRESS", { childCount: 0, descendantCount: 0 })} />);
    expect(running.container.querySelector("[data-task-card]")).toHaveClass("aq-stripe");
    expect(running.container.querySelector(".aq-pulse")).not.toBeNull();
    running.unmount();
    const ready = render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 })} />);
    expect(ready.container.querySelector("[data-task-card]")).not.toHaveClass("aq-stripe");
    expect(ready.container.querySelector(".aq-pulse")).toBeNull();
  });

  it("writes one reason sentence from the loaded neighbours", () => {
    render(<TaskCard data={card("DEFINED", { childCount: 0, descendantCount: 0 }, {
      relations: { blockerCount: 2, blockerTitle: "Schema", dependentTitle: null, readyAhead: null },
    })} />);
    expect(screen.getByText((_, el) => el?.hasAttribute("data-reason") === true)).toHaveTextContent("After Schema and 1 more");
  });

  it("splits a worker rung into class and harness tags and counts open gates", () => {
    render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 }, {
      task: { id: "task-one", title: "Ship it", status: "READY", priority: 100, profile_id: "deep-high-codex", intelligence_class: "deep-high" },
      gates: [{ gate_type: "review", status: "OPEN" }, { gate_type: "ci", status: "resolved" }] as TaskNodeData["gates"],
    })} />);
    expect(screen.getByText("deep-high")).toBeInTheDocument();
    expect(screen.getByText("codex")).toBeInTheDocument();
    expect(screen.getByText("1 gate")).toHaveAttribute("title", "review");
  });

  it("shows priority only when it is urgent", () => {
    const urgent = render(<TaskCard data={card("READY", {}, { task: { id: "task-one", title: "Ship it", status: "READY", priority: 10 } })} />);
    expect(screen.getByText("P10")).toBeInTheDocument();
    urgent.unmount();
    render(<TaskCard data={card("READY")} />);
    expect(screen.queryByText(/^P\d+$/)).not.toBeInTheDocument();
  });

  it("marks a stub from another project and gives it no status pill", () => {
    render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 }, { stub: { foreign: true } })} />);
    expect(screen.getByText("other project")).toBeInTheDocument();
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
  });
});

describe("progress", () => {
  it("still shows a (full) progress bar once a card is settled", () => {
    render(<TaskCard data={card("COMPLETED")} />);
    expect(screen.getByRole("progressbar", { name: "3 of 4 tasks done" })).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("counts running work underneath an epic on its reason line", () => {
    render(<TaskCard data={card("COMPLETED", { runningCount: 1, blockedCount: 1 })} />);
    expect(screen.getByRole("progressbar", { name: "3 of 4 tasks done" })).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.hasAttribute("data-reason") === true)).toHaveTextContent("3 of 4 done · 1 running · 1 blocked");
  });

  it("renders a subtask bar and puts the share on a running card's pill", () => {
    render(<TaskCard data={card("IN_PROGRESS", { childCount: 0, descendantCount: 0 }, { subtasks: { total: 3, settled: 1 } })} />);
    expect(screen.getByRole("progressbar", { name: "1 of 3 subtasks settled" })).toBeInTheDocument();
    expect(screen.getByText("In progress · 33%")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.hasAttribute("data-reason") === true)).toHaveTextContent("1 of 3 subtasks");
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
    expect(enter.closest("[role=button][aria-label^='Open task']")).toBeNull();
    await userEvent.click(enter);
    expect(onFocus).toHaveBeenCalledWith("task-one");
  });

  it("draws an epic as a labelled stack of sheets", () => {
    const { container } = render(<TaskCard data={card("READY")} />);
    expect(screen.getByText("Epic")).toBeInTheDocument();
    const face = container.querySelector("[data-task-card]")!;
    expect(face.previousElementSibling).toHaveAttribute("aria-hidden");
  });

  it("shows no enter control, sheet or kind label on a leaf card", () => {
    const { container } = render(<TaskCard data={card("READY", { childCount: 0, descendantCount: 0 })} />);
    expect(screen.queryByRole("button", { name: /^Enter/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Epic")).not.toBeInTheDocument();
    expect(container.querySelector("[data-task-card]")!.previousElementSibling).toBeNull();
    expect(screen.getByRole("button", { name: "Open task Ship it" })).toBeInTheDocument();
  });
});

describe("phase label", () => {
  it("shows the phase order and label as the card's kind", () => {
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

  it("omits the label for a non-phase task", () => {
    render(<TaskCard data={card("READY")} />);
    expect(screen.queryByText(/^Phase /)).not.toBeInTheDocument();
  });
});

describe("epic cards", () => {
  const epic = (status: string, delivery: TaskNodeData["delivery"]) =>
    card(status, { descendantCount: 5, completedCount: 5, childCount: 5 }, { delivery });

  it("separates implementation progress from a blocked delivery", () => {
    const { container } = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.missingReceipt)} />);
    expect(screen.getByRole("progressbar", { name: "5 of 5 tasks done" })).toBeInTheDocument();
    expect(screen.getByText("Integration blocked - final fix not collected")).toBeInTheDocument();
    expect(container.querySelector("[data-task-card]")).toHaveAttribute("data-status", "BLOCKED");
    expect(screen.queryByText("Paused")).not.toBeInTheDocument();
    expect(screen.queryByText("PAUSED")).not.toBeInTheDocument();
  });

  it("explains the stored status of an integration hold in the status tooltip", () => {
    render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.strandedReservation)} />);
    const pill = screen.getByText(EPIC_DELIVERY.strandedReservation.label).closest("[title]")!;
    expect(pill.getAttribute("title")).toMatch(/^Verification blocked - branch handoff required\. /);
    expect(pill.getAttribute("title")).toMatch(/ · task status PAUSED \(held by integration, not paused by anyone\)$/);
  });

  it("reads Paused only for an operator hold", () => {
    const { container } = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.manualPause)} />);
    expect(screen.getByText("Paused by operator")).toBeInTheDocument();
    expect(container.querySelector("[data-task-card]")).toHaveAttribute("data-status", "PAUSED");
  });

  it("pulses only with evidence of active work", () => {
    const { container, unmount } = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.activeIntegration)} />);
    expect(container.querySelector(".aq-pulse")).not.toBeNull();
    unmount();
    const queued = render(<TaskCard data={epic("PAUSED", EPIC_DELIVERY.queuedVerifier)} />);
    expect(queued.container.querySelector(".aq-pulse")).toBeNull();
    expect(screen.getByText("Verification queued - waiting for a worker")).toBeInTheDocument();
  });

  it.each([
    ["approvalHold", "Awaiting approval"],
    ["delivered", "Delivered"],
    ["staleEvidence", "Verification status stale"],
    ["unavailable", "Delivery evidence unavailable"],
  ] as const)("%s shows its delivery headline", (name, label) => {
    render(<TaskCard data={epic("COMPLETED", EPIC_DELIVERY[name])} />);
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });

  it("keeps the plain status pill for a node without a delivery projection", () => {
    render(<TaskCard data={card("PAUSED")} />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.hasAttribute("data-reason") === true)).toHaveTextContent("3 of 4 done");
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
    expect(link).toHaveTextContent("Review brisk-lantern-7 · pending");
    expect(link).toHaveAttribute("data-review-wait", "blocking");
  });

  it("adds a second pill and a reason rather than recolouring the card (§3.3)", () => {
    const { container } = inRouter(<TaskCard data={reviewCard([wait()])} />);
    const shell = container.querySelector("[data-task-card]")!;
    expect(shell).toHaveAttribute("data-review-blocked");
    expect(shell).not.toHaveClass("border-g-accent");
    expect(screen.getByTitle("DEFINED · waiting on review brisk-lantern-7 (pending)")).toHaveTextContent("Not started");
    expect(screen.getByText((_, el) => el?.hasAttribute("data-reason") === true))
      .toHaveTextContent("Waiting on your review of brisk-lantern-7");
  });

  it.each([
    ["in_review", "pending", "pending", "bg-g-accent-soft"],
    ["changes_requested", "changes requested", "changes", "bg-g-blocked-soft"],
    ["rejected", "rejected", "rejected", "bg-g-failed-soft"],
    ["withdrawn", "withdrawn", "withdrawn", "bg-g-pending-soft"],
  ])("names a blocking %s review %s in its tone", (state, label, short, tone) => {
    inRouter(<TaskCard data={reviewCard([wait({ review_state: state })])} />);
    const link = screen.getByRole("link", { name: new RegExp(`\\(${label}\\)`) });
    expect(link).toHaveTextContent(`· ${short}`);
    expect(link).toHaveClass(tone);
  });

  it("still links an approved review that released the task, in the done tone", () => {
    const { container } = inRouter(<TaskCard data={reviewCard([
      wait({ review_state: "approved", gate_status: "resolved", blocking: false }),
    ])} />);
    const link = screen.getByRole("link", { name: /Gated on spec review brisk-lantern-7 \(approved\)/ });
    expect(link).toHaveTextContent("Review brisk-lantern-7 · approved");
    expect(link).toHaveClass("bg-g-done-soft");
    expect(link).toHaveAttribute("data-review-wait", "released");
    const shell = container.querySelector("[data-task-card]")!;
    expect(shell).not.toHaveAttribute("data-review-blocked");
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
