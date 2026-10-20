import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AskInbox } from "../FocusInbox";
import { askPage } from "../../../testUtils/asks";
const wire = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("../../../api/asks", () => ({ useAsks: wire.list }));
beforeEach(() => wire.list.mockReturnValue({ data: askPage(), refetch: vi.fn() }));
it("uses the canonical count and links, and changes server views", () => {
  render(<MemoryRouter><AskInbox /></MemoryRouter>);
  expect(screen.getByText("Needs you: 1")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Architecture proposal/ })).toHaveAttribute("href", "/focus/asks/ask-one");
  fireEvent.click(screen.getByRole("button", { name: "Waiting on AQ" }));
  expect(wire.list).toHaveBeenLastCalledWith({ view: "waiting", source_kind: undefined, cursor: undefined });
});
it("never describes a degraded or failed read as an empty inbox", () => {
  wire.list.mockReturnValue({ data: { ...askPage(), items: [], count: 0, source_health: "degraded" } });
  const view = render(<MemoryRouter><AskInbox /></MemoryRouter>);
  expect(screen.getByText(/count may be incomplete/)).toBeInTheDocument();
  expect(screen.queryByText(/Nothing in this view/)).not.toBeInTheDocument();
  const retry = vi.fn(); wire.list.mockReturnValue({ isError: true, refetch: retry });
  view.rerender(<MemoryRouter><AskInbox /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "Retry" })); expect(retry).toHaveBeenCalled();
});
it("uses the opaque next cursor and resets it on a view change", () => {
  wire.list.mockReturnValue({ data: { ...askPage(), count: 101, next_cursor: "page-two" } });
  render(<MemoryRouter><AskInbox /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(wire.list).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "page-two" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(wire.list).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: undefined, view: "history" }));
});
