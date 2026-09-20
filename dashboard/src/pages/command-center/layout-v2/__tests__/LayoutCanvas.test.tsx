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
  /** The last params each project's own layer asked for. A multi-project
   * canvas renders one layer per project, so `params` alone only ever shows
   * whichever rendered last. */
  paramsByProject: {} as Record<string, unknown>,
  /** The `options` (third-party callbacks) the canvas last passed in. */
  options: null as { onExpandedApplied?: (ids: string[]) => void } | null,
}));
const extents = vi.hoisted(() => ({ pending: false }));
vi.mock("../useLayoutTiles", () => ({
  useLayoutTiles: (
    projectId: string, params: unknown, rect: unknown,
    options?: { onExpandedApplied?: (ids: string[]) => void },
  ) => {
    tiles.params = params;
    tiles.paramsByProject[projectId] = params;
    tiles.rects.push(rect);
    tiles.options = options ?? null;
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

// A minimal fake of the daemon's dashboard-state network boundary (F1/F2/F4):
// stubbing HERE -- `dashboardStateGet`/`dashboardStatePut`, the two SDK calls
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
import { GraphStateProvider } from "../../useGraphHierarchy";
import { resetExpandedInitialisation, setExpandedTaskIds, useGraphState } from "../../useGraphHierarchy";

/** "Focus active" lives in the task toolbar, not in the canvas overlay (it
 * covered the card underneath it). These tests still own the canvas half of
 * the behaviour -- that re-arming the expansion makes the next tiles request
 * ask for `auto_expand` again -- so they drive it through the same
 * `requestActiveExpansion` call the toolbar button makes. The button itself is
 * covered by `command-center/__tests__/TaskToolbar.test.tsx`. */
function FocusActiveProbe({ projectIds }: { projectIds?: string[] }) {
  const { requestActiveExpansion } = useGraphState();
  return <button type="button" onClick={() => requestActiveExpansion(projectIds)}>Focus active (toolbar)</button>;
}

/** Density lives in the task toolbar too, immediately left of "Focus active"
 * (it floated over the canvas and covered the card underneath it). These
 * tests still own the canvas half of the behaviour -- that a density change
 * re-scales card positions -- driven through the same `setDensity` call the
 * toolbar control makes. The control itself is covered by
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
  } as unknown as TilesResponse);
  tiles.params = null;
  tiles.rects.length = 0;
  tiles.loaded = true;
  tiles.refetchVisible = vi.fn();
  extents.pending = false;
  hiddenFinishedCount.value = null;
  base.setShowCompleted.mockClear();
  fitBounds.mockReset();
  setCenter.mockReset();
  setViewport.mockReset();
  getViewport.mockReset();
  getViewport.mockReturnValue({ x: 0, y: 0, zoom: 1 });
  tiles.error = null;
  layoutNode.data = undefined;
  // The expanded set is one live store, not per-component state.
  setExpandedTaskIds(new Set());
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

  it("uses the full layout while a finished container is expanded", async () => {
    tiles.store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("done", "stub", 0, 0, { status: "COMPLETED" })],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as unknown as TilesResponse);
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const toggle = () => {
      const node = flow.current!.nodes.find((candidate) => candidate.id === "done")!;
      return (node.data as { onToggleChildren: (id: string, finished?: boolean) => void }).onToggleChildren;
    };

    act(() => toggle()("done", true));
    await screen.findByTestId("node-done");
    expect(tiles.params).toMatchObject({ variant: "all", expanded: ["done"] });

    act(() => toggle()("done", true));
    await screen.findByTestId("node-done");
    expect(tiles.params).toMatchObject({ variant: "active", expanded: [] });
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

  it("waits for the focus node's layout: a 202 fits nothing, the real response fits once", () => {
    layoutNode.data = { pending: true };
    const view = render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).not.toHaveBeenCalled();
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("e");
    layoutNode.data = { node: n("e", "container", 0, 0, { w: 3, h: 2 }), ancestors: [], layout_version: 1 };
    view.rerender(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(fitBounds).toHaveBeenCalledTimes(1);
  });

  describe("land on the active subgraph (design A3)", () => {
    it("a first load with no stored expansion sends auto_expand and persists the applied set once", async () => {
      resetExpandedInitialisation();
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(true);
      expect((tiles.params as { expanded: string[] }).expanded).toEqual([]);

      // The server's response carries the computed set; the layer reports it
      // back through `onExpandedApplied`, exactly as a real response would.
      act(() => tiles.options?.onExpandedApplied?.(["e"]));
      await screen.findByTestId("node-e");
      expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e"]);
      // Now that the project has a stored expansion, a later render must not
      // ask the server to compute it again.
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(false);
    });

    it("a project with a stored expansion -- even an empty one the user chose -- never sends auto_expand", () => {
      setExpandedTaskIds(new Set());
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      expect((tiles.params as { expanded: string[] }).expanded).toEqual([]);
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(false);
    });

    it("re-arms auto_expand when the toolbar clears the stored expansion", () => {
      setExpandedTaskIds(new Set(["e"]));
      render(<MemoryRouter><LayoutCanvas {...base} /><FocusActiveProbe /></MemoryRouter>);
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(false);

      fireEvent.click(screen.getByRole("button", { name: "Focus active (toolbar)" }));
      expect((tiles.params as { expanded: string[] }).expanded).toEqual([]);
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(true);
    });

    it("no longer floats Focus active or Density over the canvas, where they covered a card", () => {
      render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
      expect(screen.queryByRole("button", { name: "Focus active" })).not.toBeInTheDocument();
      expect(screen.queryByRole("combobox", { name: "Graph density" })).not.toBeInTheDocument();
    });
  });

  describe("against the REAL GraphStateProvider (F1/F2/F3 fix verification)", () => {
    function mountWithRealProvider(props: Partial<typeof base> = {}) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
      return render(
        <QueryClientProvider client={qc}>
          <GraphStateProvider projectIds={["p1"]}>
            <MemoryRouter><LayoutCanvas {...base} {...props} /><FocusActiveProbe /></MemoryRouter>
          </GraphStateProvider>
        </QueryClientProvider>,
      );
    }

    it("F1: persists a server-computed expanded_applied as a real SET, not intersected against the (empty) stored expansion", async () => {
      // No seeded document: `command_center_project_view` for "p1" has never
      // been written, so the project starts un-initialised.
      mountWithRealProvider();
      await screen.findByTestId("node-e");
      expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(true);
      expect((tiles.params as { expanded: string[] }).expanded).toEqual([]);

      // The server's response carries ids the client had never stored before.
      // The OLD `replaceExpanded`-based setter would have intersected these
      // against the stored `[]` and written `[]` right back.
      await act(async () => { tiles.options?.onExpandedApplied?.(["e", "pkg"]); });

      await waitFor(() => {
        const put = dashboardStateFake.puts.find((p) => p.namespace === "command_center_project_view");
        expect(put).toBeDefined();
        expect((put!.value as { expanded_task_ids: string[] }).expanded_task_ids).toEqual(["e", "pkg"]);
        expect((put!.value as { expanded_initialised: boolean }).expanded_initialised).toBe(true);
      });

      // The persisted write lands in the query cache, `hasStoredExpansion`
      // flips, and the layer's NEXT request sends the set explicitly with no
      // `auto_expand` -- never re-asking the server to compute it.
      await waitFor(() => {
        expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e", "pkg"]);
        expect((tiles.params as { autoExpand?: boolean }).autoExpand).toBe(false);
      });
    });

    it("F2: Focus active clears BOTH expanded_task_ids and expanded_finished_task_ids", async () => {
      seedDashboardDoc("command_center_project_view", "p1", {
        expanded_task_ids: ["e", "pkg"],
        expanded_finished_task_ids: ["pkg"],
        manual_positions: {},
        expanded_initialised: true,
      });
      mountWithRealProvider();
      await screen.findByTestId("node-e");
      await waitFor(() => expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e", "pkg"]));

      dashboardStateFake.puts.length = 0;
      fireEvent.click(screen.getByRole("button", { name: "Focus active (toolbar)" }));

      await waitFor(() => {
        const put = dashboardStateFake.puts.find((p) => p.namespace === "command_center_project_view");
        expect(put).toBeDefined();
        const value = put!.value as { expanded_task_ids: string[]; expanded_finished_task_ids: string[] };
        // Both lists clear: the server validator requires finished to stay a
        // subset of expanded, so a write that cleared only one is rejected
        // and silently reverted (F2).
        expect(value.expanded_task_ids).toEqual([]);
        expect(value.expanded_finished_task_ids).toEqual([]);
      });
      // No conflict (revision_conflict) response was ever recorded: had the
      // write been rejected, `putDashboardDocument`'s catch path would have
      // reconciled onto the server's `current` document instead.
      expect(dashboardStateFake.puts.every((p) => p.value !== undefined)).toBe(true);
    });

    it("F3: a multi-project canvas persists per project instead of being gated off", async () => {
      // Two projects, both never initialised.
      const view = render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <GraphStateProvider projectIds={["p1", "p2"]}>
            <MemoryRouter>
              <LayoutCanvas {...base} projectIds={["p1", "p2"]}
                projectNames={new Map([["p1", "P1"], ["p2", "P2"]])} />
            </MemoryRouter>
          </GraphStateProvider>
        </QueryClientProvider>,
      );
      await screen.findAllByTestId("node-e");
      // Both layers rendered against the mocked `useLayoutTiles`, so
      // `tiles.params`/`tiles.options` reflect whichever rendered last; what
      // matters here is that persisting p2's result does not depend on p1
      // being the only project (the old single-project gate would have left
      // `onExpandedApplied` `undefined` for a 2-project canvas).
      await act(async () => { tiles.options?.onExpandedApplied?.(["z"]); });
      await waitFor(() => {
        const puts = dashboardStateFake.puts.filter((p) => p.namespace === "command_center_project_view");
        expect(puts.length).toBeGreaterThan(0);
        const last = puts[puts.length - 1]!;
        expect((last.value as { expanded_task_ids: string[] }).expanded_task_ids).toEqual(["z"]);
      });
      view.unmount();
    });

    it("each layer sends only its OWN project's stored expansion", async () => {
      // p1 has expanded something; p2 has never been initialised. The union
      // `expandedTaskIds` used to go to both layers, which made p2 look
      // expanded and suppressed its auto_expand indefinitely.
      seedDashboardDoc("command_center_project_view", "p1", {
        expanded_task_ids: ["e"],
        expanded_finished_task_ids: [],
        manual_positions: {},
        expanded_initialised: true,
      });
      tiles.paramsByProject = {};
      const view = render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <GraphStateProvider projectIds={["p1", "p2"]}>
            <MemoryRouter>
              <LayoutCanvas {...base} projectIds={["p1", "p2"]}
                projectNames={new Map([["p1", "P1"], ["p2", "P2"]])} />
            </MemoryRouter>
          </GraphStateProvider>
        </QueryClientProvider>,
      );
      await screen.findAllByTestId("node-e");
      await waitFor(() => {
        const p1 = tiles.paramsByProject.p1 as { expanded: string[]; autoExpand?: boolean };
        expect(p1.expanded).toEqual(["e"]);
        expect(p1.autoExpand).toBe(false);
      });
      const p2 = tiles.paramsByProject.p2 as { expanded: string[]; autoExpand?: boolean };
      expect(p2.expanded).toEqual([]);
      expect(p2.autoExpand).toBe(true);
      view.unmount();
    });
  });
});
