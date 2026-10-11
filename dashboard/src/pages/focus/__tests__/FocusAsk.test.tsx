import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import FocusAsk from "../FocusAsk";
vi.mock("../../../components/asks/AskDetail", () => ({ AskDetail: ({ askId }: { askId: string }) => <output>{askId}</output> }));
vi.mock("../focusChrome", () => ({ useFocusChrome: () => {} }));
it("opens the shared detail for the canonical route", () => {
  render(<MemoryRouter initialEntries={["/focus/asks/ask-one"]}><Routes><Route path="/focus/asks/:askId" element={<FocusAsk />} /></Routes></MemoryRouter>);
  expect(screen.getByText("ask-one")).toBeInTheDocument();
});
