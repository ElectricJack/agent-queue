import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewsInbox from "../ReviewsInbox";

const reviewsApi = vi.hoisted(() => ({
  reviews: [] as Array<Record<string, unknown>>,
  filters: [] as Array<Record<string, string | undefined>>,
  projects: [] as Array<Record<string, unknown>>,
  snapshotAt: 1 as number | null,
  snapshotAge: 15 as number | null,
  loading: false,
  error: null as Error | null,
}));

const withdrawApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), isPending: false }));


vi.mock("../../../api/reviews", () => ({
  useReviews: (filters: Record<string, string | undefined>) => {
    reviewsApi.filters.push(filters);
    return { data: { reviews: reviewsApi.reviews }, isLoading: false, error: null };
  },
  useWithdrawReview: () => withdrawApi,
}));

vi.mock("../../../api/pullRequests", () => ({
  usePullRequestProjectSummaries: () => ({
    data: {
      projects: reviewsApi.projects,
      snapshot_at: reviewsApi.snapshotAt,
      snapshot_age_seconds: reviewsApi.snapshotAge,
    },
    isLoading: reviewsApi.loading, error: reviewsApi.error,
  }),
  usePendingPullRequests: () => { throw new Error("Reviews must not fetch PR details"); },
  useApprovePullRequest: () => { throw new Error("Reviews must not mount PR approvals"); },
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
  expect(screen.getByText("held while paused")).toBeInTheDocument();
});

beforeEach(() => {
  withdrawApi.mutateAsync.mockReset();
  withdrawApi.mutateAsync.mockResolvedValue({ success: true, flagged_task_ids: [] });
  reviewsApi.filters = [];
  reviewsApi.projects = [];
  reviewsApi.snapshotAt = 1;
  reviewsApi.snapshotAge = 15;
  reviewsApi.loading = false;
  reviewsApi.error = null;
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
  it("shows one project row with an open PR count and repo link above document reviews", () => {
    reviewsApi.projects = [
      {
        project_id: "agent-queue", project_name: "Agent Queue", open_pr_count: 12,
        repository: "acme/repo", pulls_url: "https://github.com/acme/repo/pulls",
      },
      {
        project_id: "other", project_name: "Other project", open_pr_count: 0,
        repository: "acme/other", pulls_url: "https://github.com/acme/other/pulls",
      },
    ];
    renderInbox();

    const table = screen.getByRole("table", { name: "Project pull requests" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("Agent Queue")).toBeInTheDocument();
    expect(within(table).getByText("12")).toBeInTheDocument();
    expect(within(table).getByText("0")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "View pull requests for Agent Queue" });
    expect(link).toHaveAttribute("href", "https://github.com/acme/repo/pulls");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("link", { name: "View pull requests for Other project" }))
      .toHaveAttribute("href", "https://github.com/acme/other/pulls");
    expect(screen.getByText("GitHub snapshot 15s old")).toBeInTheDocument();
    expect(screen.queryByText("PR title")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    const heading = screen.getByRole("heading", { name: "Document reviews" });
    expect(table.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("Architecture proposal")).toBeInTheDocument();
  });

  it("shows an empty state when no projects have GitHub repositories", () => {
    renderInbox();
    expect(screen.getByText("No active projects with GitHub repositories.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Project pull requests" })).not.toBeInTheDocument();
  });

  it("shows unknown counts until the first GitHub snapshot", () => {
    reviewsApi.projects = [{
      project_id: "agent-queue", project_name: "Agent Queue", open_pr_count: 0,
      repository: "acme/repo", pulls_url: "https://github.com/acme/repo/pulls",
    }];
    reviewsApi.snapshotAt = null;
    reviewsApi.snapshotAge = null;
    renderInbox();
    const table = screen.getByRole("table", { name: "Project pull requests" });
    expect(within(table).getByText("—")).toBeInTheDocument();
    expect(within(table).queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for GitHub snapshot…")).toBeInTheDocument();
  });

  it("keeps document reviews visible while project counts load or fail", () => {
    reviewsApi.loading = true;
    const { rerender } = renderInbox();
    expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
    expect(screen.queryByText("No active projects with GitHub repositories.")).not.toBeInTheDocument();
    expect(screen.getByText("Architecture proposal")).toBeInTheDocument();
    reviewsApi.loading = false;
    reviewsApi.error = new Error("Unavailable");
    rerender(inbox());
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load pull requests.");
    expect(screen.queryByText("No active projects with GitHub repositories.")).not.toBeInTheDocument();
    expect(screen.getByText("Architecture proposal")).toBeInTheDocument();
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
