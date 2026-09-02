import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const list = vi.hoisted(() => vi.fn());
vi.mock("../../../../api/graphLayout", () => ({ fetchList: list }));

import MobileLayoutList from "../MobileLayoutList";

const n = (id: string) => ({
  id, title: `Task ${id}`, status: "READY", priority: 100, is_blocked: false, x: 0, y: 0, w: 1, h: 1,
  depth: 0, container_id: null, kind: "card", context_only: false, agg_children: 0,
  agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
});

const filters = { query: "", status: "", showCompleted: false, focus: "" };
const props = {
  projectId: "p1", variant: "active" as const, filters,
  expanded: new Set<string>(), toggleExpanded: () => {}, onTaskClick: () => {},
};

beforeEach(() => list.mockReset());
afterEach(cleanup);

describe("MobileLayoutList", () => {
  it("renders the first page and loads more on demand", async () => {
    list
      .mockResolvedValueOnce({ nodes: [n("a"), n("b")], next_cursor: "c1", layout_version: 1 })
      .mockResolvedValueOnce({ nodes: [n("c")], next_cursor: null, layout_version: 1 });
    render(<MemoryRouter><MobileLayoutList {...props} /></MemoryRouter>);

    expect(await screen.findByText("Task a")).toBeInTheDocument();
    expect(screen.getByText("Task b")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Task c")).toBeInTheDocument();
    expect(screen.getByText("Task a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(list).toHaveBeenNthCalledWith(2, "p1", expect.objectContaining({ cursor: "c1" }));
  });

  it("restarts paging when the filters change", async () => {
    list.mockResolvedValue({ nodes: [n("a")], next_cursor: null, layout_version: 1 });
    const view = render(<MemoryRouter><MobileLayoutList {...props} /></MemoryRouter>);
    expect(await screen.findByText("Task a")).toBeInTheDocument();

    list.mockResolvedValue({ nodes: [n("z")], next_cursor: null, layout_version: 1 });
    view.rerender(<MemoryRouter><MobileLayoutList {...props} filters={{ ...filters, query: "z" }} /></MemoryRouter>);
    expect(await screen.findByText("Task z")).toBeInTheDocument();
    expect(screen.queryByText("Task a")).toBeNull();
    expect(list).toHaveBeenLastCalledWith("p1", expect.objectContaining({ q: "z", cursor: null }));
  });
});
