import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewsInbox from "../ReviewsInbox";
import { askPage } from "../../../testUtils/asks";

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


vi.mock("../../../api/asks", () => ({ useAsks: (filters: Record<string, string | undefined>) => {
  reviewsApi.filters.push(filters);
  const data = askPage();
  data.items = reviewsApi.reviews.map(row => ({ ...data.items[0]!, id: String(row.id), context: { title: String(row.title) }, held_while_paused: Boolean(row.held_while_paused) }));
  return { data: { ...data, count: data.items.length } };
} }));

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
  expect(screen.getByText("Held while paused")).toBeInTheDocument();
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

  it("uses the shared document filter and canonical links", () => {
    renderInbox();
    expect(screen.getByRole("link", { name: "Architecture proposal" })).toHaveAttribute("href", "/focus/asks/review-waiting");
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({ view: "needs_you", source_kind: "review", cursor: undefined });
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(reviewsApi.filters[reviewsApi.filters.length - 1]).toEqual({ view: "history", source_kind: "review", cursor: undefined });
  });
});
