import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewsInbox from "../ReviewsInbox";
import { askPage } from "../../../testUtils/asks";

const reviewsApi = vi.hoisted(() => ({
  reviews: [] as Array<Record<string, unknown>>,
  filters: [] as Array<Record<string, string | undefined>>,
  pullRequests: [] as Array<Record<string, unknown>>,
  canApprove: false,
}));

const withdrawApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), isPending: false }));
const approveApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), isPending: false, error: null }));

vi.mock("../../../api/asks", () => ({ useAsks: (filters: Record<string, string | undefined>) => {
  reviewsApi.filters.push(filters);
  const data = askPage();
  data.items = reviewsApi.reviews.map(row => ({ ...data.items[0]!, id: String(row.id), context: { title: String(row.title) }, held_while_paused: Boolean(row.held_while_paused) }));
  return { data: { ...data, count: data.items.length } };
} }));

vi.mock("../../../api/pullRequests", () => ({
  usePendingPullRequests: () => ({
    data: { pull_requests: reviewsApi.pullRequests, can_approve: reviewsApi.canApprove },
    isLoading: false, error: null,
  }),
  useApprovePullRequest: () => approveApi,
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function inbox(path = "/reviews") {
  return (
    <MemoryRouter initialEntries={[path]}>
      <ReviewsInbox />
      <Location />
    </MemoryRouter>
  );
}

function renderInbox(path = "/reviews") {
  return render(inbox(path));
}

it("labels a human review held by a paused project", () => {
  reviewsApi.reviews[0]!.held_while_paused = true;
  renderInbox();
  expect(screen.getByText("Held while paused")).toBeInTheDocument();
});

beforeEach(() => {
  withdrawApi.mutateAsync.mockReset();
  withdrawApi.mutateAsync.mockResolvedValue({ success: true, flagged_task_ids: [] });
  reviewsApi.filters = [];
  reviewsApi.pullRequests = [];
  reviewsApi.canApprove = false;
  approveApi.mutateAsync.mockReset();
  approveApi.mutateAsync.mockResolvedValue({ success: true });
  reviewsApi.reviews = [
    {
      id: "review-waiting",
      title: "Architecture proposal",
      kind: "spec",
      project_id: "agent-queue",
      author_task_id: "steady-lantern.6",
      state: "in_review",
      decider: "user",
      current_revision: 2,
      created_at: 1_789_000_000,
      open_comment_count: 3,
    },
    {
      id: "review-delegated",
      title: "Delegated design",
      kind: "plan",
      project_id: "agent-queue",
      author_task_id: "steady-lantern.5",
      state: "in_review",
      decider: "user_or_supervisor",
      current_revision: 1,
      updated_at: 1_789_000_010,
      open_comment_count: 0,
    },
  ];
});

afterEach(cleanup);

describe("ReviewsInbox", () => {
  it("shows pending PRs with GitHub links in a new tab beside document reviews", () => {
    reviewsApi.pullRequests = [{
      title: "Improve queue", url: "https://github.com/acme/repo/pull/12",
      repository: "acme/repo", project_id: "agent-queue", project_name: "Agent Queue",
      task_id: "steady-lantern", state: "open", opened_at: Date.now() / 1000 - 7200,
    }];
    renderInbox();

    expect(screen.getByRole("heading", { name: "Pull requests" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Document reviews" })).toBeInTheDocument();
    expect(screen.getByText("Architecture proposal")).toBeInTheDocument();
    expect(screen.getByText("acme/repo")).toBeInTheDocument();
    expect(screen.getByText("steady-lantern")).toBeInTheDocument();
    expect(screen.getByText("2h")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Improve queue" }))
      .toHaveAttribute("href", "https://github.com/acme/repo/pull/12");
    expect(screen.getByRole("link", { name: "Improve queue" }))
      .toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "Improve queue" }))
      .toHaveAttribute("rel", "noopener noreferrer");
  });

  it("shows an empty state and a pending PR link", () => {
    const { rerender } = renderInbox();
    expect(screen.getByText("No pending pull requests.")).toBeInTheDocument();
    reviewsApi.pullRequests = [{
      title: "Fallback title", url: "https://github.com/acme/repo/pull/4",
      repository: "acme/repo", project_id: "agent-queue", project_name: "Agent Queue",
      task_id: "failed-lookup", state: "open", opened_at: null,
    }];
    rerender(<MemoryRouter><ReviewsInbox /></MemoryRouter>);
    expect(screen.getAllByText("pending").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Fallback title" })).toHaveAttribute("target", "_blank");
  });

  it("shows Approve only to operator viewers and pins the displayed head", async () => {
    reviewsApi.pullRequests = [{
      title: "Improve queue", url: "https://github.com/acme/repo/pull/12",
      repository: "acme/repo", project_id: "agent-queue", project_name: "Agent Queue",
      task_id: "steady-lantern", state: "open", opened_at: null,
      head_sha: "a".repeat(40), review_decision: "pending", ci_status: "success",
      train_state: "awaiting review",
    }];
    const { rerender } = renderInbox();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    reviewsApi.canApprove = true;
    rerender(inbox());
    expect(screen.getByText("awaiting review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approveApi.mutateAsync).toHaveBeenCalledWith({
      taskId: "steady-lantern", headSha: "a".repeat(40),
    }));
  });

  it("uses the shared document filter and canonical links", () => {
    renderInbox();
    expect(screen.getByRole("link", { name: "Architecture proposal" })).toHaveAttribute("href", "/focus/asks/review-waiting");
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({ view: "needs_you", source_kind: "review", cursor: undefined });
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({ view: "history", source_kind: "review", cursor: undefined });
  });
});
