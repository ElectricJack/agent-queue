import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AskCard } from "../AskCard";
import { askData } from "../../../testUtils/asks";
it("keeps paused requests visible with their version and canonical link", () => {
  const ask = { ...askData().ask, held_while_paused: true };
  render(<MemoryRouter><AskCard ask={ask} /></MemoryRouter>);
  expect(screen.getByText(/Held while paused/i)).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute("href", "/focus/asks/ask-one");
  expect(screen.getByText(/generation 2/)).toBeInTheDocument();
});
