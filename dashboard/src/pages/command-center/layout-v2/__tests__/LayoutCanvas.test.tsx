import type { ReactNode } from "react";
import type { NodeChange } from "@xyflow/react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { TilesResponse } from "@aq/ts-client";

/** The subset of a React Flow node the canvas builds and these tests read back. */
interface FlowNode {
  id: string;
  type?: string;
  position: { x: number; y: number };
  selected?: boolean;
  draggable?: boolean;
  data: Record<string, unknown>;
}

interface FlowProps {
  nodes: FlowNode[];
  children: ReactNode;
  onlyRenderVisibleElements?: boolean;
  onMove?: (event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
  onNodeClick?: (event: unknown, node: FlowNode) => void;
  onNodesChange?: (changes: NodeChange[]) => void;
  onNodeDragStop?: (event: unknown, node: FlowNode) => void;
  nodesDraggable?: boolean;
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null }));
const fitBounds = vi.hoisted(() => vi.fn());
const setCenter = vi.hoisted(() => vi.fn());
const setViewport = vi.hoisted(() => vi.fn());
const getViewport = vi.hoisted(() => vi.fn(() => ({ x: 0, y: 0, zoom: 1 })));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  useReactFlow: () => ({ fitBounds, setCenter, setViewport, getViewport }),
  ReactFlow: (props: FlowProps) => {
    flow.current = props;
    return <div>
      {props.nodes.map((node) => <div key={node.id} data-testid={`node-${node.id}`} />)}
      {props.children}
    </div>;
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => null,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
  ViewportPortal: ({ children }: { children: ReactNode }) => <>{children}</>,
  useStore: (selector: (s: { nodeLookup: Map<string, unknown> }) => unknown) => selector({ nodeLookup: new Map() }),
}));

const tiles = vi.hoisted(() => ({
  store: null as unknown,
  /** Every rect the canvas has handed the tiles hook, newest last. */
  rects: [] as unknown[],
  pending: false,
  error: null as Error | null,
  refetchVisible: vi.fn(),
  loaded: true,
  params: null as unknown,
}));
const extents = vi.hoisted(() => ({ pending: false }));
vi.mock("../useLayoutTiles", () => ({
  useLayoutTiles: (_projectId: string, params: unknown, rect: unknown) => {
    tiles.params = params;
    tiles.rects.push(rect);
    return tiles;
  },
}));
const layoutNode = vi.hoisted(() => ({ data: undefined as unknown }));
vi.mock("../../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) => ids.map(() => extents.pending
    ? { pending: true }
    : { layout_version: 1, extent_w: 10, extent_h: 10, node_count: 3 }),
  useLayoutNode: () => layoutNode,
  locate: vi.fn(),
  useTidyLayout: () => ({ mutate: vi.fn() }),
}));

import { emptyStore, mergeTiles } from "../layoutStore";
import { sizePx, toPx } from "../units";
import LayoutCanvas from "../LayoutCanvas";
import { setExpandedTaskIds } from "../../useGraphHierarchy";

const n = (id: string, kind: string, x: number, y: number, extra: Record<string, unknown> = {}) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0,
  container_id: null, kind, context_only: false, agg_children: 1, agg_descendants: 1,
  agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 1, ...extra,
});
const filters = { query: "", status: "", showCompleted: false, focus: "" };
const base = {
  projectIds: ["p1"],
  projectNames: new Map([["p1", "P1"]]),
  variant: "active" as const,
  filters,
  focusId: null,
  setFocus: vi.fn(),
  onTaskClick: vi.fn(),
};

beforeEach(() => {
  tiles.store = mergeTiles(emptyStore(), ["0:0"], {
    nodes: [n("e", "collapsed", 0, 0), n("z", "card", 2, 0)],
    edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
  } as unknown as TilesResponse);
  tiles.params = null;
  tiles.rects.length = 0;
  tiles.loaded = true;
  tiles.refetchVisible = vi.fn();
  extents.pending = false;
  fitBounds.mockReset();
  setCenter.mockReset();
  setViewport.mockReset();
  getViewport.mockReset();
  getViewport.mockReturnValue({ x: 0, y: 0, zoom: 1 });
  tiles.error = null;
  layoutNode.data = undefined;
  localStorage.clear();
  // The expanded set is one live store, not per-component state.
  setExpandedTaskIds(new Set());
});
afterEach(cleanup);

describe("LayoutCanvas", () => {
  it("moves task cards and restores their saved positions", () => {
    const first = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(flow.current?.nodesDraggable).toBe(true);
    expect(flow.current?.nodes.find((node) => node.id === "z")?.draggable).toBe(true);

    act(() => flow.current!.onNodesChange!([
      { id: "z", type: "position", position: { x: 720, y: 312 }, dragging: true },
    ]));
    const moved = flow.current!.nodes.find((node) => node.id === "z")!;
    expect(moved.position).toEqual({ x: 720, y: 312 });
    act(() => flow.current!.onNodeDragStop!(null, moved));
    expect(JSON.parse(localStorage.getItem("aq.command-center.graph-positions")!)).toMatchObject({
      p1: { z: { x: 3, y: 2 } },
    });

    first.unmount();
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(flow.current!.nodes.find((node) => node.id === "z")?.position).toEqual({ x: 720, y: 312 });
  });

  it("renders server nodes with visibility culling and sends viewport-derived params", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.getByTestId("node-e")).toBeInTheDocument();
    expect(screen.getByTestId("node-z")).toBeInTheDocument();
    expect(flow.current?.onlyRenderVisibleElements).toBe(true);
    expect(tiles.params).toMatchObject({ variant: "active", expanded: [], root: null, q: "", status: "" });
  });

  it("uses comfortable density by default and persists a user-selected density", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const density = screen.getByRole("combobox", { name: "Graph density" });
    expect(density).toHaveValue("comfortable");
    fireEvent.change(density, { target: { value: "compact" } });
    expect(localStorage.getItem("aq.command-center.graph-density")).toBe("compact");
  });

  it("restores the saved density for this browser user", () => {
    localStorage.setItem("aq.command-center.graph-density", "spacious");
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.getByRole("combobox", { name: "Graph density" })).toHaveValue("spacious");
  });

  it("re-renders only the cards a live refetch actually changed", () => {
    const wire = (nodes: unknown[]) => mergeTiles(emptyStore(), ["0:0"], {
      nodes: JSON.parse(JSON.stringify(nodes)), edges: [], stubs: [], stub_overflow: [],
      workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    tiles.store = wire([n("e", "card", 0, 0), n("z", "card", 2, 0)]);
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const before = Object.fromEntries(flow.current!.nodes.map((node) => [node.id, node]));

    // A `task.updated` for `e` alone: the layer re-delivers both cards off the
    // wire, but `z` must keep the very object React Flow already adopted --
    // that identity is exactly what stops its card re-rendering.
    tiles.store = wire([n("e", "card", 0, 0, { status: "COMPLETED" }), n("z", "card", 2, 0)]);
    view.rerender(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const after = Object.fromEntries(flow.current!.nodes.map((node) => [node.id, node]));
    expect(after.z).toBe(before.z);
    expect(after.e).not.toBe(before.e);
  });

  it("leaves every other card's identity alone when the selection moves", () => {
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const before = Object.fromEntries(flow.current!.nodes.map((node) => [node.id, node]));
    view.rerender(<MemoryRouter><LayoutCanvas {...base} selectedTaskId="e" /></MemoryRouter>);
    const after = Object.fromEntries(flow.current!.nodes.map((node) => [node.id, node]));
    expect(after.e!.selected).toBe(true);
    expect(after.z).toBe(before.z);
  });

  it("only asks for tiles again when a pan changes the cells the viewport covers", () => {
    const observers = globalThis.ResizeObserver;
    const raf = globalThis.requestAnimationFrame;
    let notify: (entries: { contentRect: { width: number; height: number } }[]) => void = () => {};
    class Stub {
      constructor(cb: typeof notify) { notify = cb; }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = Stub as unknown as typeof ResizeObserver;
    // The canvas throttles pans onto the animation frame, and only the
    // leading edge of a frame lands immediately -- so the queue is drained by
    // hand between the two pans.
    const frames: FrameRequestCallback[] = [];
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => frames.push(cb)) as unknown as typeof requestAnimationFrame;
    const flushFrames = () => act(() => { for (const cb of frames.splice(0)) cb(0); });
    try {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      act(() => notify([{ contentRect: { width: 1200, height: 800 } }]));
      const settled = tiles.rects[tiles.rects.length - 1];
      expect(settled).not.toBeNull();

      // A pan of a few pixels covers the same tiles: the hook must not even
      // see a new rect, let alone issue a request.
      act(() => flow.current!.onMove!(null, { x: -12, y: -4, zoom: 1 }));
      expect(tiles.rects[tiles.rects.length - 1]).toBe(settled);
      flushFrames();

      // A pan of several tiles is a real change.
      act(() => flow.current!.onMove!(null, { x: -20000, y: -8000, zoom: 1 }));
      expect(tiles.rects[tiles.rects.length - 1]).not.toBe(settled);
    } finally {
      globalThis.ResizeObserver = observers;
      globalThis.requestAnimationFrame = raf;
    }
  });

  it("lowers max depth when zoomed out", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    act(() => flow.current!.onMove!(null, { x: 0, y: 0, zoom: 0.2 }));
    expect((tiles.params as { maxDepth: number }).maxDepth).toBe(0);
  });

  it("focus forces variant all, sets root, fits bounds, and shows breadcrumbs", () => {
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
    render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(tiles.params).toMatchObject({ variant: "all", root: "e" });
    expect(fitBounds).toHaveBeenCalledWith({ x: 0, y: 0, width: 720, height: 312 }, expect.anything());
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("P1");
  });

  it("toggling a collapsed card adds it to expanded params", async () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const node = flow.current!.nodes.find((candidate) => candidate.id === "e")!;
    act(() => (node.data as { onToggleChildren: (id: string) => void }).onToggleChildren("e"));
    await screen.findByTestId("node-e");
    expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e"]);
  });

  it("toggling a container reflows its siblings to the positions the API returns", async () => {
    // `e` is collapsed to one tile, with `z` laid out right below it.
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "collapsed", 0, 0), n("z", "card", 0, 1.2)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const node = flow.current!.nodes.find((candidate) => candidate.id === "e")!;
    act(() => (node.data as { onToggleChildren: (id: string) => void }).onToggleChildren("e"));
    expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e"]);

    // Expanded, the server answers with `e` three units tall and `z` pushed
    // down by exactly the space it took back.
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "container", 0, 0, { w: 1, h: 3 }), n("z", "card", 0, 3.2)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    view.rerender(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const z = flow.current!.nodes.find((candidate) => candidate.id === "z")!;
    expect(z.position).toEqual(toPx(0, 3.2));
    // The toggled container is a fixed point of the compaction, so nothing
    // needs to pan to keep it under the pointer.
    expect(setViewport).not.toHaveBeenCalled();
  });

  it("pans to hold a toggled container still when the reflow does move it", () => {
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "collapsed", 0, 0), n("z", "card", 0, 1.2)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    getViewport.mockReturnValue({ x: 40, y: 60, zoom: 2 });
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const before = flow.current!.nodes.find((candidate) => candidate.id === "e")!;
    const node = before;
    act(() => (node.data as { onToggleChildren: (id: string) => void }).onToggleChildren("e"));

    // A concurrent republish can land `e` somewhere else entirely; the pin is
    // what keeps the operator's eye on the container they clicked.
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "collapsed", 0, 5), n("z", "card", 0, 6.2)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    view.rerender(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const moved = flow.current!.nodes.find((candidate) => candidate.id === "e")!;
    expect(setViewport).toHaveBeenCalledWith({
      x: before.position.x * 2 + 40 - moved.position.x * 2,
      y: before.position.y * 2 + 60 - moved.position.y * 2,
      zoom: 2,
    });
  });

  it("does not claim an empty graph before the first tiles response", () => {
    tiles.store = emptyStore();
    tiles.loaded = false;
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.queryByText("No tasks or playbooks match these filters.")).toBeNull();
    expect(screen.getByRole("region", { name: "Task graph" })).toBeInTheDocument();
  });

  it("shows the empty state once every layer has loaded with nothing to draw", () => {
    tiles.store = emptyStore();
    tiles.loaded = true;
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.getByText("No tasks or playbooks match these filters.")).toBeInTheDocument();
  });

  it("loads tiles when a project's extent stops being pending", () => {
    extents.pending = true;
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    tiles.refetchVisible.mockClear();
    extents.pending = false;
    view.rerender(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(tiles.refetchVisible).toHaveBeenCalled();
  });

  it("hands the clicked card's payload to onTaskClick so run tasks keep their routing", () => {
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 0, 0, { playbook_run_id: "run-1" })],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    const onTaskClick = vi.fn();
    render(<MemoryRouter><LayoutCanvas {...base} onTaskClick={onTaskClick} /></MemoryRouter>);
    const node = flow.current!.nodes.find((candidate) => candidate.id === "z")!;
    act(() => flow.current!.onNodeClick!(null, node));
    expect(onTaskClick).toHaveBeenCalledWith("z", expect.objectContaining({ id: "z", playbook_run_id: "run-1" }));
  });

  it("routes a clicked container through its own node payload", () => {
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "container", 0, 0, { w: 3, h: 2, playbook_run_id: "run-2" })],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    const onTaskClick = vi.fn();
    render(<MemoryRouter><LayoutCanvas {...base} onTaskClick={onTaskClick} /></MemoryRouter>);
    const node = flow.current!.nodes.find((candidate) => candidate.id === "e")!;
    act(() => flow.current!.onNodeClick!(null, node));
    expect(onTaskClick).toHaveBeenCalledWith("e", expect.objectContaining({ id: "e", playbook_run_id: "run-2" }));
  });

  it("fits the viewport to a located search result", () => {
    const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    fitBounds.mockClear();
    const hit = { id: "z", x: 2, y: 1, w: 1, h: 1 };
    view.rerender(<MemoryRouter><LayoutCanvas {...base} jumpTarget={hit} /></MemoryRouter>);
    expect(fitBounds).toHaveBeenCalledWith(
      { ...toPx(hit.x, hit.y), ...sizePx(hit.w, hit.h) },
      expect.anything(),
    );
  });

  it("shows an error band with a retry instead of the empty state when tiles fail", () => {
    tiles.store = emptyStore();
    tiles.error = new Error("rect larger than 64.0 units");
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("rect larger than 64.0 units");
    expect(screen.queryByText("No tasks or playbooks match these filters.")).toBeNull();
    tiles.refetchVisible.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(tiles.refetchVisible).toHaveBeenCalled();
  });

  it("centres the viewport on a keyboard target so an off-screen node is reachable", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "ArrowRight" });
    // "z" sits two units right of "e": its centre is (2*240 + 240/2, 156/2).
    expect(setCenter).toHaveBeenCalledWith(600, 78, expect.objectContaining({ duration: 0 }));
  });

  it("treats a +N overflow marker as scenery: not clickable, not a keyboard destination", () => {
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 0, 0)],
      edges: [], stubs: [],
      stub_overflow: [{ node_id: "z", direction: "out", more: 4 }],
      workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    const onTaskClick = vi.fn();
    render(<MemoryRouter><LayoutCanvas {...base} onTaskClick={onTaskClick} /></MemoryRouter>);
    const marker = flow.current!.nodes.find((candidate) => candidate.id === "overflow:z|out")!;
    expect(marker.type).toBe("overflowMarker");
    act(() => flow.current!.onNodeClick!(null, marker));
    expect(onTaskClick).not.toHaveBeenCalled();
    // Arrowing right from the only card must not land on the pill beside it.
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "ArrowRight" });
    expect(setCenter).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "Enter" });
    expect(onTaskClick).toHaveBeenCalledWith("z", expect.objectContaining({ id: "z" }));
  });

  it("waits for the focus node's layout: a 202 fits nothing, the real response fits once", () => {
    layoutNode.data = { pending: true };
    const view = render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).not.toHaveBeenCalled();
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("e");
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
    view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).toHaveBeenCalledTimes(1);
  });
});
