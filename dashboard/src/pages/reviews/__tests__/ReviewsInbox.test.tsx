import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewsInbox from "../ReviewsInbox";

const reviewsApi = vi.hoisted(() => ({
  reviews: [] as Array<Record<string, unknown>>,
  filters: [] as Array<Record<string, string | undefined>>,
}));

vi.mock("../../../api/reviews", () => ({
  useReviews: (filters: Record<string, string | undefined>) => {
    reviewsApi.filters.push(filters);
    return { data: { reviews: reviewsApi.reviews }, isLoading: false, error: null };
  },
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderInbox(path = "/reviews") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ReviewsInbox />
      <Location />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  reviewsApi.filters = [];
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
});
