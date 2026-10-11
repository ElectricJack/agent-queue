import { expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AskDetail } from "../AskDetail";
import { askData } from "../../../testUtils/asks";
const wire = vi.hoisted(() => ({ detail: vi.fn() }));
vi.mock("../../../api/asks", () => ({ useAsk: wire.detail }));
vi.mock("../../../panes/review", () => ({ ReviewPane: ({ reviewId }: { reviewId: string }) => <output aria-label="Review reader">{reviewId}</output> }));
it("reuses the reader and exposes failed operations and separate obligations", () => {
  const data = askData();
  data.decision = { id: "decision-one", option_key: "approve", actor_id: "human:operator" };
  data.operations = [{ id: "op-one", operation_kind: "revision", status: "failed", receipt: { error: "Revision unavailable" } }];
  data.obligations = [{ id: "f-one", original_text: "Document restart", progress: "pending", task_id: "task-one" }];
  wire.detail.mockReturnValue({ data });
  render(<MemoryRouter><AskDetail askId="ask-one" /></MemoryRouter>);
  expect(screen.getByLabelText("Review reader")).toHaveTextContent("rev-x");
  expect(screen.getByText(/Revision unavailable/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Assigned work/ })).toHaveAttribute("href", "/focus/tasks/task-one");
  expect(screen.getByText("Follow-up obligations (1)")).toBeInTheDocument();
});
it("makes unavailable detail retryable", () => {
  const refetch = vi.fn(); wire.detail.mockReturnValue({ isError: true, error: new Error("Source unavailable"), refetch });
  render(<AskDetail askId="ask-one" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Source unavailable");
  fireEvent.click(screen.getByRole("button", { name: "Retry" })); expect(refetch).toHaveBeenCalled();
});

it("keeps the existing reader mounted when a background refresh fails", () => {
  wire.detail.mockReturnValue({ data: askData(), isError: true, error: new Error("Offline"), refetch: vi.fn() });
  render(<MemoryRouter><AskDetail askId="ask-one" /></MemoryRouter>);
  expect(screen.getByLabelText("Review reader")).toHaveTextContent("rev-x");
  expect(screen.getByRole("alert")).toHaveTextContent("Your draft is preserved");
});
