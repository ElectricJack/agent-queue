import type { ReactNode } from "react";
import type { NodeChange } from "@xyflow/react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { TilesResponse } from "@aq/ts-client";

/** The subset of a React Flow node the canvas builds and these tests read back. */
interface FlowNode {
  id: string;
  type?: string;
  position: { x: number; y: number };
  width?: number;
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
  onNodeDoubleClick?: (event: unknown, node: FlowNode) => void;
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
  // The avatar layer positions its badges from the nodes they dock at, so the
  // fake store answers from whatever the canvas last handed React Flow.
  useStore: (selector: (s: { nodeLookup: Map<string, unknown> }) => unknown) => selector({
    nodeLookup: new Map((flow.current?.nodes ?? []).map((node) => [node.id, node])),
  }),
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
  /** Every params object the canvas has asked for, newest last. */
  paramsSeen: [] as unknown[],
  /** The last params each project's own layer asked for. A multi-project
   * canvas renders one layer per project, so `params` alone only ever shows
   * whichever rendered last. */
  paramsByProject: {} as Record<string, unknown>,
}));
const extents = vi.hoisted(() => ({ pending: false }));
vi.mock("../useLayoutTiles", () => ({
  useLayoutTiles: (projectId: string, params: unknown, rect: unknown) => {
    tiles.params = params;
    tiles.paramsSeen.push(params);
    tiles.paramsByProject[projectId] = params;
    tiles.rects.push(rect);
    return tiles;
  },
}));
const layoutNode = vi.hoisted(() => ({ data: undefined as unknown }));
/** null (default) means "not cheaply available"; a test sets a number to
 * simulate the `all` variant's extent already sitting in the query cache. */
const hiddenFinishedCount = vi.hoisted(() => ({ value: null as number | null }));
vi.mock("../../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) => ids.map(() => extents.pending
    ? { pending: true }
    : { layout_version: 1, extent_w: 10, extent_h: 10, node_count: 3 }),
  useLayoutNode: () => layoutNode,
  useHiddenFinishedCount: () => hiddenFinishedCount.value,
  locate: vi.fn(),
  useTidyLayout: () => ({ mutate: vi.fn() }),
}));

// A minimal fake of the daemon's dashboard-state network boundary: stubbing
// HERE -- `dashboardStateGet`/`dashboardStatePut`, the two SDK calls
// `fetchDashboardDocument`/`putDashboardDocument` make -- exercises the REAL
// `GraphStateProvider`, not a mock of the provider itself.
interface FakeDoc { revision: number; value: unknown }
const dashboardStateFake = vi.hoisted(() => ({
  docs: new Map<string, FakeDoc>(),
  puts: [] as { namespace: string; subject?: string | null; base_revision: number | null; value: unknown }[],
}));
function fakeDocKey(namespace: string, subject?: string | null) { return `${namespace}\u0001${subject ?? ""}`; }
/** Test helper: seed a document as if an earlier session had already written it. */
function seedDashboardDoc(namespace: string, subject: string | null, value: unknown, revision = 1) {
  dashboardStateFake.docs.set(fakeDocKey(namespace, subject), { revision, value });
}
vi.mock("../../../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../api/client")>();
  return {
    ...actual,
    dashboardStateGet: vi.fn(async ({ body }: { body: { namespace: string; subject?: string } }) => {
      const row = dashboardStateFake.docs.get(fakeDocKey(body.namespace, body.subject ?? null));
      return {
        data: {
          document: {
            namespace: body.namespace, subject: body.subject ?? null,
            revision: row?.revision ?? 0, exists: !!row, value: row?.value ?? {},
          },
        },
      };
    }),
    dashboardStatePut: vi.fn(async (
      { body }: { body: { namespace: string; subject?: string; base_revision: number | null; value: unknown } },
    ) => {
      dashboardStateFake.puts.push(body);
      const key = fakeDocKey(body.namespace, body.subject ?? null);
      const current = dashboardStateFake.docs.get(key);
      if (body.base_revision != null && body.base_revision !== (current?.revision ?? 0)) {
        const err = new Error("API 409: revision conflict") as Error & { payload?: unknown };
        err.payload = {
          success: false, error_code: "revision_conflict",
          current: {
            namespace: body.namespace, subject: body.subject ?? null,
            revision: current?.revision ?? 0, exists: !!current, value: current?.value ?? {},
          },
        };
        throw err;
      }
      const revision = (current?.revision ?? 0) + 1;
      dashboardStateFake.docs.set(key, { revision, value: body.value });
      return {
        data: {
          document: { namespace: body.namespace, subject: body.subject ?? null, revision, exists: true, value: body.value },
        },
      };
    }),
  };
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { emptyStore, mergeTiles } from "../layoutStore";
import { sizePx, toPx } from "../units";
import LayoutCanvas from "../LayoutCanvas";
import { GraphStateProvider, resetGraphStateFallback, useGraphState } from "../../useGraphHierarchy";

/** Density lives in the task toolbar, not in the canvas overlay (it floated
 * over the canvas and covered the card underneath it). These tests still own
 * the canvas half of the behaviour -- that a density change re-scales card
 * positions -- driven through the same `setDensity` call the toolbar control
 * makes. The control itself is covered by
 * `command-center/__tests__/TaskToolbar.test.tsx`. */
function DensityProbe() {
  const { setDensity } = useGraphState();
  return <button type="button" onClick={() => setDensity("compact")}>Density: compact (toolbar)</button>;
}

const n = (id: string, kind: string, x: number, y: number, extra: Record<string, unknown> = {}) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0,
  container_id: null, kind, context_only: false, agg_children: 1, agg_descendants: 1,
  agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 1, ...extra,
});
const filters = { query: "", status: "", showCompleted: false, focus: "", window: "" };
const base = {
  projectIds: ["p1"],
  projectNames: new Map([["p1", "P1"]]),
  variant: "active" as const,
  filters,
  focusId: null,
  setFocus: vi.fn(),
  setShowCompleted: vi.fn(),
  onTaskClick: vi.fn(),
};

beforeEach(() => {
  tiles.store = mergeTiles(emptyStore(), ["0:0"], {
    nodes: [n("e", "collapsed", 0, 0), n("z", "card", 2, 0)],
    edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    variant_applied: "active",
  } as unknown as TilesResponse);
  tiles.params = null;
  tiles.paramsSeen.length = 0;
  tiles.paramsByProject = {};
  tiles.rects.length = 0;
  tiles.loaded = true;
  tiles.refetchVisible = vi.fn();
  extents.pending = false;
  hiddenFinishedCount.value = null;
  base.setFocus.mockClear();
  base.setShowCompleted.mockClear();
  fitBounds.mockReset();
  setCenter.mockReset();
  setViewport.mockReset();
  getViewport.mockReset();
  getViewport.mockReturnValue({ x: 0, y: 0, zoom: 1 });
  tiles.error = null;
  layoutNode.data = undefined;
  // Density and manual positions live in one module-level store for
  // components rendered without their route provider.
  resetGraphStateFallback();
  dashboardStateFake.docs.clear();
  dashboardStateFake.puts.length = 0;
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

  it("uses server-default (comfortable) density until the toolbar changes it", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /><DensityProbe /></MemoryRouter>);
    const before = flow.current!.nodes.find((node) => node.id === "z")!.position;
    expect(before).toEqual(toPx(2, 0));

    fireEvent.click(screen.getByRole("button", { name: "Density: compact (toolbar)" }));

    const after = flow.current!.nodes.find((node) => node.id === "z")!.position;
    expect(after).toEqual(toPx(2, 0, "compact"));
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

  it("waits for the focus node's layout: a 202 fits nothing, the real response fits once", () => {
    layoutNode.data = { pending: true };
    const view = render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).not.toHaveBeenCalled();
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("e");
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
    view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds.mock.calls[0]![0]).toEqual({ x: 0, y: 0, width: 720, height: 312 });
    // The persisted box, then (the tiles having landed) the cropped frame --
    // and a re-render with nothing new fits nothing more.
    const fits = fitBounds.mock.calls.length;
    expect(fits).toBeLessThanOrEqual(2);
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
    view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).toHaveBeenCalledTimes(fits);
  });

  it("refits an entered container to its children once they land, leaving a far stub out", () => {
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 12 }), ancestors: [], layout_version: 1 };
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "container", 0, 0, { w: 3, h: 12 }),
              n("c", "card", 0.1, 0.45, { container_id: "e", depth: 1 }),
              n("d", "card", 0.1, 5.33, { container_id: "e", depth: 1 })],
      edges: [], stubs: [{ id: "far", project_id: "p1", x: 6.15, y: 12.22, w: 1, h: 1, title: "Far" }],
      stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    const last = fitBounds.mock.calls[fitBounds.mock.calls.length - 1]![0] as { x: number; y: number; width: number; height: number };
    expect(fitBounds.mock.calls[0]![0]).toEqual({ x: 0, y: 0, width: 720, height: 12 * 156 });
    expect(last.x).toBe(0); expect(last.y).toBe(0);
    // Frame 1.25 wide (card at 0.1 + 1 + inset) plus the docked stub column.
    expect(last.width).toBeCloseTo((1.25 + 0.6 + 1) * 240);
    expect(last.height).toBeCloseTo((6.33 + 0.15) * 156);
    expect(flow.current!.nodes.find((node) => node.id === "far")!.position.y).toBe(0);
  });

  it("does not claim an empty graph before the first tiles response", () => {
    tiles.store = emptyStore();
    tiles.loaded = false;
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.queryByText(/no unfinished work here/i)).toBeNull();
    expect(screen.queryByText(/no tasks yet/i)).toBeNull();
    expect(screen.getByRole("region", { name: "Task graph" })).toBeInTheDocument();
  });

  it("shows no empty state while nodes are present", () => {
    // The default beforeEach store already has nodes "e" and "z".
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.queryByText(/no unfinished work here/i)).toBeNull();
    expect(screen.queryByText(/no tasks yet/i)).toBeNull();
  });

  it("says there's no unfinished work and offers a button that turns Show completed on", () => {
    tiles.store = emptyStore();
    tiles.loaded = true;
    render(<MemoryRouter><LayoutCanvas {...base} filters={{ ...filters, showCompleted: false }} /></MemoryRouter>);
    expect(screen.getByText(/no unfinished work here/i)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Show completed" });
    fireEvent.click(button);
    expect(base.setShowCompleted).toHaveBeenCalledWith(true);
  });

  it("says there are no tasks yet when Show completed is already on and the graph is still empty", () => {
    tiles.store = emptyStore();
    tiles.loaded = true;
    render(<MemoryRouter><LayoutCanvas {...base} filters={{ ...filters, showCompleted: true }} /></MemoryRouter>);
    expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show completed" })).not.toBeInTheDocument();
  });

  it("includes a hidden-finished-tasks count in the caption when it's already cached", () => {
    hiddenFinishedCount.value = 12;
    tiles.store = emptyStore();
    tiles.loaded = true;
    render(<MemoryRouter><LayoutCanvas {...base} filters={{ ...filters, showCompleted: false }} /></MemoryRouter>);
    expect(screen.getByText(/12 finished tasks hidden/i)).toBeInTheDocument();
  });

  it("omits the hidden-finished-tasks count when it would need a new request", () => {
    tiles.store = emptyStore();
    tiles.loaded = true;
    render(<MemoryRouter><LayoutCanvas {...base} filters={{ ...filters, showCompleted: false }} /></MemoryRouter>);
    expect(screen.queryByText(/finished tasks? hidden/i)).toBeNull();
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

  it("shows an error band with a retry instead of the empty state when tiles fail", () => {
    tiles.store = emptyStore();
    tiles.error = new Error("rect larger than 64.0 units");
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("rect larger than 64.0 units");
    expect(screen.queryByText(/no unfinished work here/i)).toBeNull();
    expect(screen.queryByText(/no tasks yet/i)).toBeNull();
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

  /**
   * Operator decision (2026-09-20): the graph never expands a container in
   * place. Every container is a compact tile at every zoom, and the only way
   * into one is to enter it.
   */
  describe("containers are never expanded inline", () => {
    it("never asks for an expansion, an auto expansion or a depth, at any zoom", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      for (const zoom of [0.15, 0.2, 0.5, 0.8, 1, 2]) {
        act(() => flow.current!.onMove!(null, { x: 0, y: 0, zoom }));
      }
      expect(tiles.paramsSeen.length).toBeGreaterThan(0);
      for (const params of tiles.paramsSeen as Record<string, unknown>[]) {
        expect(params.expanded).toEqual([]);
        expect(params.autoExpand).toBeUndefined();
        expect(params.maxDepth ?? null).toBeNull();
      }
    });

    it("asks for exactly the same tiles zoomed out as zoomed in", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      const atOne = tiles.params;
      act(() => flow.current!.onMove!(null, { x: 0, y: 0, zoom: 0.2 }));
      // Same object identity: nothing about the request depends on zoom, so
      // the layer does not even re-run its fetch effect.
      expect(tiles.params).toBe(atOne);
    });

    it("draws a container tile with an enter control and no expand/collapse toggle", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      const card = flow.current!.nodes.find((node) => node.id === "e")!;
      const data = card.data as { onToggleChildren?: unknown; onFocus?: (id: string) => void };
      expect(data.onToggleChildren).toBeUndefined();
      expect(typeof data.onFocus).toBe("function");
    });

    it("ignores an expansion an earlier session stored for this project", async () => {
      seedDashboardDoc("command_center_project_view", "p1", {
        expanded_task_ids: ["e", "pkg"],
        expanded_finished_task_ids: ["pkg"],
        manual_positions: {},
        expanded_initialised: true,
      });
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <GraphStateProvider projectIds={["p1"]}>
            <MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>
          </GraphStateProvider>
        </QueryClientProvider>,
      );
      await screen.findByTestId("node-e");
      // Give the document query every chance to land and be read.
      await waitFor(() => expect(dashboardStateFake.docs.size).toBeGreaterThan(0));
      for (const params of tiles.paramsSeen as { expanded: string[] }[]) {
        expect(params.expanded).toEqual([]);
      }
      // ...and nothing writes those fields back either.
      expect(dashboardStateFake.puts).toEqual([]);
    });

    it("still pins a card per project scope with the expansion gone", async () => {
      const view = render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <GraphStateProvider projectIds={["p1"]}>
            <MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>
          </GraphStateProvider>
        </QueryClientProvider>,
      );
      await screen.findByTestId("node-z");
      act(() => flow.current!.onNodesChange!([
        { id: "z", type: "position", position: { x: 720, y: 312 }, dragging: true },
      ]));
      const moved = flow.current!.nodes.find((node) => node.id === "z")!;
      act(() => flow.current!.onNodeDragStop!(null, moved));

      await waitFor(() => {
        const put = dashboardStateFake.puts.find((p) => p.namespace === "command_center_project_view");
        expect(put).toBeDefined();
        expect(put!.subject).toBe("p1");
        expect((put!.value as { manual_positions: Record<string, unknown> }).manual_positions).toEqual({
          z: { x: 3, y: 2 },
        });
      });
      view.unmount();
    });

    it("docks a worker running inside a collapsed container on that container's tile", () => {
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "collapsed", 0, 0)],
        edges: [], stubs: [], stub_overflow: [],
        workers: [{ agent_id: "a1", name: "bot", docked_at: "e", in_collapsed: true }],
        gates: [], layout_version: 1,
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      expect(screen.getByRole("img", { name: "bot (working in collapsed tasks)" })).toBeInTheDocument();
    });

    it("docks it just the same one level down, inside an entered container", () => {
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("pkg", "collapsed", 0.2, 0.5)],
        edges: [], stubs: [], stub_overflow: [],
        workers: [{ agent_id: "a1", name: "bot", docked_at: "pkg", in_collapsed: true }],
        gates: [], layout_version: 1,
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(screen.getByRole("img", { name: "bot (working in collapsed tasks)" })).toBeInTheDocument();
    });
  });

  describe("entering a container", () => {
    it("sets focus from a tile's enter control", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      const card = flow.current!.nodes.find((node) => node.id === "e")!;
      act(() => (card.data as { onFocus: (id: string) => void }).onFocus("e"));
      expect(base.setFocus).toHaveBeenCalledWith("e");
    });

    it("enters a container on a double click of its tile", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      const card = flow.current!.nodes.find((node) => node.id === "e")!;
      act(() => flow.current!.onNodeDoubleClick!(null, card));
      expect(base.setFocus).toHaveBeenCalledWith("e");
    });

    it("does not enter anything when a leaf card is double clicked", () => {
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("z", "card", 2, 0, { agg_children: 0, agg_descendants: 0 })],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      const card = flow.current!.nodes.find((node) => node.id === "z")!;
      act(() => flow.current!.onNodeDoubleClick!(null, card));
      expect(base.setFocus).not.toHaveBeenCalled();
    });

    it("sets root and keeps the variant it was given, and shows the path", () => {
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(tiles.params).toMatchObject({ variant: "active", root: "e", expanded: [] });
      expect(fitBounds).toHaveBeenCalledWith({ x: 0, y: 0, width: 720, height: 312 }, expect.anything());
      expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("P1");
    });

    it("shows the whole ancestor path and an up-one-level control at depth 3", async () => {
      layoutNode.data = {
        node: n("g", "container", 0, 0, { w: 2, h: 2, title: "Grandchild" }),
        ancestors: [{ id: "e", title: "Epic" }, { id: "pkg", title: "Package" }],
        layout_version: 1,
      };
      render(<MemoryRouter><LayoutCanvas {...base} focusId="g" /></MemoryRouter>);
      const nav = screen.getByRole("navigation", { name: "Focus path" });
      expect(nav).toHaveTextContent("P1");
      expect(nav).toHaveTextContent("Epic");
      expect(nav).toHaveTextContent("Package");
      expect(nav).toHaveTextContent("Grandchild");
      fireEvent.click(screen.getByRole("button", { name: "Up one level" }));
      expect(base.setFocus).toHaveBeenCalledWith("pkg");
      fireEvent.click(screen.getByRole("button", { name: "Epic" }));
      expect(base.setFocus).toHaveBeenCalledWith("e");
    });

    it("says completed work is shown when the response was served from `all`", () => {
      // The container's own status is DEFINED: the promotion is the
      // LAYOUT's decision (every descendant finished), which is why the
      // response reports it and the client never infers it.
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("kid", "card", 0.2, 0.5)],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
        variant_applied: "all",
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(screen.getByText(/completed work is shown/i)).toBeInTheDocument();
    });

    it("says nothing when the operator asked for completed work themselves", () => {
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("kid", "card", 0.2, 0.5)],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
        variant_applied: "all",
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e"
        filters={{ ...filters, showCompleted: true }} /></MemoryRouter>);
      expect(screen.queryByText(/completed work is shown/i)).toBeNull();
    });

    it("says nothing for a finished container the active layout still carries", () => {
      // The old status inference claimed completed work was on screen here.
      layoutNode.data = {
        node: n("e", "container", 0, 0, { w: 3, h: 2, status: "COMPLETED" }), ancestors: [], layout_version: 1,
      };
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(screen.queryByText(/completed work is shown/i)).toBeNull();
    });

    it("says there is no unfinished work inside a container whose children are all hidden", () => {
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      // A focused response ALWAYS carries the entered container itself, so
      // "nothing here" can never mean "no nodes at all".
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 })],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
        variant_applied: "active",
      } as unknown as TilesResponse);
      tiles.loaded = true;
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(screen.getByText(/no unfinished work here/i)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Show completed" }));
      expect(base.setShowCompleted).toHaveBeenCalledWith(true);
    });

    it("shows no empty state inside a container that does have children", () => {
      layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("kid", "card", 0.2, 0.5)],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
        variant_applied: "active",
      } as unknown as TilesResponse);
      render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
      expect(screen.queryByText(/no unfinished work here/i)).toBeNull();
    });

    it("fits the viewport to a located search result in the current scope", () => {
      const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      fitBounds.mockClear();
      const hit = { id: "z", x: 2, y: 1, w: 1, h: 1, container_id: null };
      view.rerender(<MemoryRouter><LayoutCanvas {...base} jumpTarget={hit} /></MemoryRouter>);
      expect(fitBounds).toHaveBeenCalledWith(
        { ...toPx(hit.x, hit.y), ...sizePx(hit.w, hit.h) },
        expect.anything(),
      );
    });

    it("pans to a hit a filter already drew in this scope instead of re-scoping", () => {
      // A search force-opens the ancestors of every match server-side (the
      // one exception to enter-only), so the match IS on screen: entering
      // its container would throw the operator's place away.
      tiles.store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("g0", "card", 2, 1)],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
        variant_applied: "active",
      } as unknown as TilesResponse);
      const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      fitBounds.mockClear();
      const hit = { id: "g0", x: 2, y: 1, w: 1, h: 1, container_id: "pkg" };
      view.rerender(<MemoryRouter><LayoutCanvas {...base} jumpTarget={hit} /></MemoryRouter>);
      expect(base.setFocus).not.toHaveBeenCalled();
      expect(fitBounds).toHaveBeenCalledWith(
        { ...toPx(hit.x, hit.y), ...sizePx(hit.w, hit.h) },
        expect.anything(),
      );
    });

    it("enters the parent container when the jump target lives inside one", () => {
      const view = render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      fitBounds.mockClear();
      const hit = { id: "g0", x: 2, y: 1, w: 1, h: 1, container_id: "pkg" };
      view.rerender(<MemoryRouter><LayoutCanvas {...base} jumpTarget={hit} /></MemoryRouter>);
      expect(base.setFocus).toHaveBeenCalledWith("pkg");
      // The entered container is what the viewport is fitted to; fitting the
      // hit's own box now would use coordinates from the scope we just left.
      expect(fitBounds).not.toHaveBeenCalled();
    });

    it("does not re-fit a spent hit once entering its container has landed", () => {
      const hit = { id: "g0", x: 2, y: 1, w: 1, h: 1, container_id: "pkg" };
      const view = render(<MemoryRouter><LayoutCanvas {...base} jumpTarget={hit} /></MemoryRouter>);
      expect(base.setFocus).toHaveBeenCalledWith("pkg");
      fitBounds.mockClear();
      // The URL change comes back as a new `focusId`, with the same hit.
      view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="pkg" jumpTarget={hit} /></MemoryRouter>);
      expect(fitBounds).not.toHaveBeenCalledWith(
        { ...toPx(hit.x, hit.y), ...sizePx(hit.w, hit.h) },
        expect.anything(),
      );
    });

    it("fits the hit itself once its own container is the one on screen", () => {
      const view = render(<MemoryRouter><LayoutCanvas {...base} focusId="pkg" /></MemoryRouter>);
      fitBounds.mockClear();
      const hit = { id: "g0", x: 2, y: 1, w: 1, h: 1, container_id: "pkg" };
      view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="pkg" jumpTarget={hit} /></MemoryRouter>);
      expect(base.setFocus).not.toHaveBeenCalled();
      expect(fitBounds).toHaveBeenCalledWith(
        { ...toPx(hit.x, hit.y), ...sizePx(hit.w, hit.h) },
        expect.anything(),
      );
    });
  });
});
