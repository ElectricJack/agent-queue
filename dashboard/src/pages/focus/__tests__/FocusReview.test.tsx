import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import FocusReview from "../FocusReview";
import { askData } from "../../../testUtils/asks";
vi.mock("../../../api/asks", () => ({ useAskAlias: () => ({ data: askData() }) }));
vi.mock("../focusChrome", () => ({ useFocusChrome: () => {} }));
function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}{location.hash}</output>; }
it("resolves the legacy review alias without dropping revision or anchor", () => {
  render(<MemoryRouter initialEntries={["/focus/reviews/rev-x?revision=1#goal"]}><Routes>
    <Route path="/focus/reviews/:reviewId" element={<FocusReview />} /><Route path="/focus/asks/:askId" element={<Location />} />
  </Routes></MemoryRouter>);
  expect(screen.getByText("/focus/asks/ask-one?revision=1#goal")).toBeInTheDocument();
});
it("reports a missing reference", () => {
  render(<MemoryRouter><FocusReview /></MemoryRouter>);
  expect(screen.getByRole("alert")).toHaveTextContent("Request id is missing");
});
