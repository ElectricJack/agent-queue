import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewsInbox from "../ReviewsInbox";

const reviewsApi = vi.hoisted(() => ({
  reviews: [] as Array<Record<string, unknown>>,
  filters: [] as Array<Record<string, string | undefined>>,
  pullRequests: [] as Array<Record<string, unknown>>,
  canApprove: false,
}));

const withdrawApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), isPending: false }));
const approveApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), isPending: false, error: null }));

vi.mock("../../../api/reviews", () => ({
  useReviews: (filters: Record<string, string | undefined>) => {
    reviewsApi.filters.push(filters);
    return { data: { reviews: reviewsApi.reviews }, isLoading: false, error: null };
  },
  useWithdrawReview: () => withdrawApi,
}));

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
    {
      id: "review-approved",
      title: "Already approved",
      kind: "other",
      project_id: "other",
      state: "approved",
      decider: "user",
      current_revision: 1,
    },
    {
      id: "review-supervisor",
      title: "Supervisor only",
      kind: "spec",
      project_id: "agent-queue",
      state: "in_review",
      decider: "supervisor",
      current_revision: 1,
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
    expect(screen.getAllByText("pending")).toHaveLength(2);
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

  it("defaults to waiting reviews for the user and links delegated rows", () => {
    renderInbox();

    expect(screen.getByText("Architecture proposal")).toBeInTheDocument();
    expect(screen.getByText("Delegated design")).toBeInTheDocument();
    expect(screen.getByText("delegated")).toBeInTheDocument();
    expect(screen.queryByText("Already approved")).not.toBeInTheDocument();
    expect(screen.queryByText("Supervisor only")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Architecture proposal" }))
      .toHaveAttribute("href", "/reviews/review-waiting");
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({ state: "in_review" });
  });

  it("stores state, kind and project filters in the URL", () => {
    renderInbox();

    fireEvent.change(screen.getByLabelText("Review state"), { target: { value: "approved" } });
    fireEvent.change(screen.getByLabelText("Review kind"), { target: { value: "plan" } });
    fireEvent.change(screen.getByLabelText("Review project"), { target: { value: "agent-queue" } });

    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/reviews?state=approved&kind=plan&project=agent-queue",
    );
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({
      state: "approved",
      kind: "plan",
      projectId: "agent-queue",
    });
  });

  it("offers Close only on open reviews", () => {
    reviewsApi.reviews = [
      ...reviewsApi.reviews,
      { id: "review-changes", title: "Needs changes", kind: "spec", project_id: "p", state: "changes_requested", current_revision: 1 },
      { id: "review-rejected", title: "Rejected once", kind: "spec", project_id: "p", state: "rejected", current_revision: 1 },
      { id: "review-withdrawn", title: "Pulled", kind: "spec", project_id: "p", state: "withdrawn", current_revision: 1 },
    ];
    // An explicit state skips the waiting-for-you filter, so every mocked row renders.
    renderInbox("/reviews?state=in_review");

    for (const title of ["Architecture proposal", "Supervisor only", "Needs changes", "Rejected once"]) {
      expect(screen.getByRole("button", { name: `Close review ${title}` })).toBeInTheDocument();
    }
    for (const title of ["Already approved", "Pulled"]) {
      expect(screen.getByText(title)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: `Close review ${title}` })).not.toBeInTheDocument();
    }
  });

  it("confirms with an optional reason, then shows the row withdrawn in place", async () => {
    const { rerender } = renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Close review Architecture proposal" }));

    const dialog = screen.getByRole("dialog", { name: "Close review" });
    expect(within(dialog).getByText("Architecture proposal")).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText("Reason"), { target: { value: "  superseded by v2 " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close review" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(withdrawApi.mutateAsync).toHaveBeenCalledWith({
      review_id: "review-waiting", reason: "superseded by v2",
    });
    const row = screen.getByRole("link", { name: "Architecture proposal" }).closest("tr")!;
    expect(within(row).getByText("withdrawn")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close review Architecture proposal" }))
      .not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close review Delegated design" })).toBeInTheDocument();

    // The refetch drops it from the waiting list; the row stays where it was.
    reviewsApi.reviews = reviewsApi.reviews.filter((review) => review.id !== "review-waiting");
    rerender(inbox());
    const [, first, second] = screen.getAllByRole("row");
    expect(within(first!).getByText("Architecture proposal")).toBeInTheDocument();
    expect(within(first!).getByText("withdrawn")).toBeInTheDocument();
    expect(within(second!).getByText("Delegated design")).toBeInTheDocument();

    // A filter change clears the kept row: withdrawn reviews live under their own filter.
    fireEvent.change(screen.getByLabelText("Review state"), { target: { value: "withdrawn" } });
    expect(screen.queryByText("Architecture proposal")).not.toBeInTheDocument();
  });

  it("sends an empty reason, and keeps the row open when the withdrawal fails", async () => {
    withdrawApi.mutateAsync.mockRejectedValueOnce(new Error("API 422: review is already approved"));
    renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Close review Architecture proposal" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close review" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("review is already approved");
    expect(withdrawApi.mutateAsync).toHaveBeenCalledWith({ review_id: "review-waiting", reason: "" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const row = screen.getByRole("link", { name: "Architecture proposal" }).closest("tr")!;
    expect(within(row).getByText("in review")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Close review Architecture proposal" }))
      .toBeInTheDocument();
  });
});
