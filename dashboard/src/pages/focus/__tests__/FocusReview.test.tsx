import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusReview from "../FocusReview";

vi.mock("../../../panes/review", () => ({
  ReviewPane: ({ reviewId }: { reviewId: string }) => (
    <div>
      <output aria-label="Review reader">{reviewId}</output>
    </div>
  ),
}));

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="reviews/:reviewId" element={<FocusReview />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("FocusReview", () => {
  it("renders the shared reader for the route's review", () => {
    renderAt("/focus/reviews/rev-123");
    expect(screen.getByLabelText("Review reader")).toHaveTextContent("rev-123");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Review");
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute(
      "href",
      "/reviews/rev-123",
    );
  });

  it("says so when the id is missing", () => {
    render(
      <QueryClientProvider client={testQueryClient()}>
        <MemoryRouter initialEntries={["/focus/reviews"]}>
          <Routes>
            <Route path="/focus/reviews" element={<FocusReview />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Review id is missing.");
  });
});