import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusInbox from "../FocusInbox";

const wire = vi.hoisted(() => ({ escalations: vi.fn(), reviews: vi.fn() }));

vi.mock("../../../api/messaging", () => ({ useEscalations: wire.escalations }));
vi.mock("../../../api/reviews", () => ({ useReviews: wire.reviews }));

import { useReviews } from "../../../api/reviews";

function escalationRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "escalation-abc",
    project_id: "agent-queue",
    task_id: null,
    source_kind: "question",
    source_identity: "q1",
    incident_key: "k",
    supervisor_owner: "session:supervisor-agent-queue",
    summary: "Keep the Opus trial or revert?",
    investigation: "",
    decision_requested: "Keep the Opus 5.5 trial or revert to Sonnet?",
    choices: null,
    severity: "high",
    state: "needs_human",
    revision: 1,
    terminal_outcome: null,
    created_at: 1790000000,
    updated_at: 1790000000,
    ...overrides,
  };
}

function reviewRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "rev-123",
    project_id: "agent-queue",
    title: "Discord as a chat extension of the supervisor",
    kind: "spec",
    state: "in_review",
    current_revision: 2,
    decider: "user",
    vault_path: "projects/agent-queue/specs/x.md",
    ...overrides,
  };
}

function renderAt(path = "/focus/inbox") {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="inbox" element={<FocusInbox />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  wire.escalations.mockReturnValue({
    data: {
      escalations: [
        escalationRow(),
        escalationRow({ id: "escalation-old", state: "stale" }),
        escalationRow({ id: "escalation-done", state: "resolved" }),
      ],
      count: 3,
    },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  });
  wire.reviews.mockReturnValue({
    data: {
      reviews: [
        reviewRow(),
        reviewRow({ id: "rev-supervised", decider: "supervisor" }),
        reviewRow({ id: "rev-approved", state: "approved", decider: "user" }),
      ],
    },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  });
});

describe("FocusInbox", () => {
  it("lists only what is waiting on a human, and links into focus", async () => {
    renderAt();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Needs you");
    expect(screen.getByText("Needs a decision · 1")).toBeInTheDocument();
    expect(screen.getByText("Reviews waiting on you · 1")).toBeInTheDocument();

    expect(screen.getByRole("link", { name: /Keep the Opus 5.5 trial/ })).toHaveAttribute(
      "href",
      "/focus/escalations/escalation-abc",
    );
    expect(screen.queryByRole("link", { name: /escalation-old/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Discord as a chat extension/ })).toHaveAttribute(
      "href",
      "/focus/reviews/rev-123",
    );
    expect(screen.queryByRole("link", { name: /rev-supervised/ })).not.toBeInTheDocument();
    // The inbox asks the list for the human-decided state only.
    expect(useReviews).toHaveBeenCalledWith({ state: "in_review" });
  });

  it("says so when nothing is waiting", async () => {
    wire.escalations.mockReturnValue({
      data: { escalations: [], count: 0 },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    });
    wire.reviews.mockReturnValue({
      data: { reviews: [] },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    });
    renderAt();
    expect(await screen.findByText("No open escalations.")).toBeInTheDocument();
    expect(screen.getByText("No reviews are waiting on you.")).toBeInTheDocument();
  });

  it("offers Retry when the escalation read fails", async () => {
    const refetch = vi.fn();
    wire.escalations.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("escalation list is unavailable"),
      refetch,
    });
    renderAt();
    expect(await screen.findByRole("alert")).toHaveTextContent("escalation list is unavailable");
    screen.getByRole("button", { name: "Retry" }).click();
    expect(refetch).toHaveBeenCalled();
  });
});