import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DecisionPreview } from "../DecisionPreview";
import { preview } from "../../../testUtils/asks";
it("shows exact revision, hashes and complete per-comment disposition", () => {
  render(<DecisionPreview preview={preview()} stale={false} pending={false} onConfirm={vi.fn()} />);
  for (const text of ["Exact revision 2", "Document the restart case", "Acceptance: Add restart coverage"]) expect(screen.getByText(text)).toBeInTheDocument();
  expect(screen.getByText(/required before release/)).toBeInTheDocument();
  expect(screen.getByText(/content-sha/)).toHaveTextContent("packet-sha");
});
it("disables confirmation for a stale packet", () => {
  render(<DecisionPreview preview={preview()} stale pending={false} onConfirm={vi.fn()} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Preview is stale");
  expect(screen.getByRole("button", { name: "Confirm prepared decision" })).toBeDisabled();
});
