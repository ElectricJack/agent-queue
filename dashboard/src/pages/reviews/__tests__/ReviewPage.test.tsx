import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ReviewPage from "../ReviewPage";
import { askData } from "../../../testUtils/asks";
vi.mock("../../../api/asks", () => ({ useAskAlias: () => ({ data: askData() }) }));
function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}{location.hash}</output>; }
it("resolves the legacy review alias without dropping revision or anchor", () => {
  render(<MemoryRouter initialEntries={["/reviews/rev-x?revision=1#goal"]}><Routes>
    <Route path="/reviews/:reviewId" element={<ReviewPage />} /><Route path="/asks/:askId" element={<Location />} />
  </Routes></MemoryRouter>);
  expect(screen.getByText("/asks/ask-one?revision=1#goal")).toBeInTheDocument();
});
it("reports a missing reference", () => {
  render(<MemoryRouter><ReviewPage /></MemoryRouter>);
  expect(screen.getByRole("alert")).toHaveTextContent("Request id is missing");
});
