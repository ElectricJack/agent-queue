import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";

const list = vi.hoisted(() => vi.fn());
const layoutNode = vi.hoisted(() => ({ data: undefined as unknown }));
vi.mock("../../../../api/graphLayout", () => ({ fetchList: list, useLayoutNode: () => layoutNode }));

import MobileLayoutList, { MobileLayoutLists } from "../MobileLayoutList";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}{location.search}</output>;
}

const n = (id: string) => ({
  id, title: `Task ${id}`, status: "READY", priority: 100, is_blocked: false, x: 0, y: 0, w: 1, h: 1,
  depth: 0, container_id: null, kind: "card", context_only: false, agg_children: 0,
  agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
});

const filters = { query: "", status: "", showCompleted: false, focus: "", window: "", held: false };
const props = {
  projectId: "p1", variant: "active" as const, filters, onTaskClick: () => {},
};

beforeEach(() => { list.mockReset(); layoutNode.data = undefined; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

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

  it("shows the same progress bars and phase header as the canvas cards", async () => {
    list.mockResolvedValueOnce({
      nodes: [
        { ...n("a"), agg_descendants: 4, agg_completed: 2, agg_running: 1, agg_blocked: 0 },
        { ...n("b"), subtasks_total: 3, subtasks_settled: 1 },
        { ...n("c"), phase_order: 2, phase_label: "Build" },
      ],
      next_cursor: null, layout_version: 1,
    });
    render(<MemoryRouter><MobileLayoutList {...props} /></MemoryRouter>);

    expect(await screen.findByText("2/4 descendants completed")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "2 of 4 done" })).toBeInTheDocument();
    expect(screen.getByText("1/3 subtasks")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "1 of 3 done" })).toBeInTheDocument();
    expect(screen.getByText("Phase 2 · Build")).toBeInTheDocument();
  });

  it("waits for a building layout instead of claiming the project is empty", async () => {
    vi.useFakeTimers();
    list
      .mockResolvedValueOnce({ pending: true })
      .mockResolvedValueOnce({ nodes: [n("a")], next_cursor: null, layout_version: 1 });
    render(<MemoryRouter><MobileLayoutList {...props} /></MemoryRouter>);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("status")).toHaveTextContent("Laying out…");
    expect(screen.queryByText("No tasks match these filters.")).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText("Task a")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("entering a container on a phone", () => {
  it("offers an enter control on a container card and no expand toggle", async () => {
    list.mockResolvedValue({
      nodes: [{ ...n("a"), agg_children: 2, agg_descendants: 3 }], next_cursor: null, layout_version: 1,
    });
    const onFocus = vi.fn();
    render(<MemoryRouter><MobileLayoutList {...props} onFocus={onFocus} /></MemoryRouter>);

    expect(await screen.findByText("Task a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /children of/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Enter Task a" }));
    expect(onFocus).toHaveBeenCalledWith("a");
  });

  it("asks the server for the entered container's scope, and pages it whole", async () => {
    layoutNode.data = {
      node: { ...n("pkg"), title: "Package", kind: "container" },
      ancestors: [{ id: "e", title: "Epic" }],
      layout_version: 1,
    };
    list.mockResolvedValue({
      nodes: [{ ...n("g0"), container_id: "pkg" }, { ...n("g1"), container_id: "pkg" }],
      next_cursor: null, layout_version: 1,
    });
    render(<MemoryRouter><MobileLayoutList {...props} focusId="pkg" projectName="P1" onFocus={() => {}} /></MemoryRouter>);

    expect(await screen.findByText("Task g0")).toBeInTheDocument();
    expect(screen.getByText("Task g1")).toBeInTheDocument();
    // `root`, not an `expanded` emulation: paging the project and narrowing
    // afterwards could return a page with none of this container's children.
    expect(list).toHaveBeenLastCalledWith("p1", expect.objectContaining({ root: "pkg", expanded: [] }));
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("Package");
  });
});

describe("MobileLayoutLists", () => {
  it("stacks one list per project under its own heading", async () => {
    list.mockResolvedValue({ nodes: [n("a")], next_cursor: null, layout_version: 1 });
    render(<MemoryRouter><MobileLayoutLists
      projectIds={["p1", "p2"]} projectNames={new Map([["p1", "Alpha"], ["p2", "Beta"]])}
      variant="active" filters={filters} onTaskClick={() => {}} /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Beta" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("Task a")).toHaveLength(2));
  });

  it("scopes entering to the section that owns the container", async () => {
    list.mockResolvedValue({
      nodes: [{ ...n("a"), agg_children: 2, agg_descendants: 3 }], next_cursor: null, layout_version: 1,
    });
    render(<MemoryRouter initialEntries={["/command-center/graph"]}><MobileLayoutLists
      projectIds={["p1", "p2"]} projectNames={new Map([["p1", "Alpha"], ["p2", "Beta"]])}
      variant="active" filters={filters} onTaskClick={() => {}} onFocus={() => {}} />
      <LocationProbe /></MemoryRouter>);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Enter Task a" })).toHaveLength(2));
    // The second section's Enter must scope THAT project, not write a global
    // focus that narrows the first section to nothing.
    fireEvent.click(screen.getAllByRole("button", { name: "Enter Task a" })[1]!);
    expect(screen.getByTestId("where")).toHaveTextContent("/projects/p2/graph");
    expect(screen.getByTestId("where")).toHaveTextContent("focus=a");
  });

  it("omits the headings for a single project", async () => {
    list.mockResolvedValue({ nodes: [n("a")], next_cursor: null, layout_version: 1 });
    render(<MemoryRouter><MobileLayoutLists
      projectIds={["p1"]} projectNames={new Map([["p1", "Alpha"]])}
      variant="active" filters={filters} onTaskClick={() => {}} /></MemoryRouter>);

    expect(await screen.findByText("Task a")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Alpha" })).toBeNull();
  });
});
