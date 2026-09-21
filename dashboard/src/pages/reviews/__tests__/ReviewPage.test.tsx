import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../../../panes/review", () => ({ ReviewPane: ({ reviewId }: { reviewId: string }) => <div>Review route: {reviewId}</div> }));

import ReviewPage from "../ReviewPage";

describe("ReviewPage", () => {
  it("renders the review pane from the review deep-link route", () => {
    render(<MemoryRouter initialEntries={["/reviews/rev-x"]}><Routes><Route path="/reviews/:reviewId" element={<ReviewPage />} /></Routes></MemoryRouter>);
    expect(screen.getByText("Review route: rev-x")).toBeInTheDocument();
  });
});
