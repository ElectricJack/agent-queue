import type { ReactNode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { TilesResponse } from "@aq/ts-client";

interface FlowProps {
  nodes: { id: string; type?: string; data: Record<string, unknown> }[];
  children: ReactNode;
  onlyRenderVisibleElements?: boolean;
  onMove?: (event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null }));
const fitBounds = vi.hoisted(() => vi.fn());
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  useReactFlow: () => ({ fitBounds }),
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
  useNodes: () => [],
}));

const tiles = vi.hoisted(() => ({
  store: null as unknown,
  pending: false,
  error: null,
  refetchVisible: vi.fn(),
  params: null as unknown,
}));
vi.mock("../useLayoutTiles", () => ({
  useLayoutTiles: (_projectId: string, params: unknown) => {
    tiles.params = params;
    return tiles;
  },
}));
const layoutNode = vi.hoisted(() => ({ data: undefined as unknown }));
vi.mock("../../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) =>
    ids.map(() => ({ layout_version: 1, extent_w: 10, extent_h: 10, node_count: 3 })),
  useLayoutNode: () => layoutNode,
  locate: vi.fn(),
  useTidyLayout: () => ({ mutate: vi.fn() }),
}));

import { emptyStore, mergeTiles } from "../layoutStore";
import LayoutCanvas from "../LayoutCanvas";

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
  fitBounds.mockReset();
  layoutNode.data = undefined;
  localStorage.clear();
});
afterEach(cleanup);

describe("LayoutCanvas", () => {
  it("renders server nodes with visibility culling and sends viewport-derived params", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.getByTestId("node-e")).toBeInTheDocument();
    expect(screen.getByTestId("node-z")).toBeInTheDocument();
    expect(flow.current?.onlyRenderVisibleElements).toBe(true);
    expect(tiles.params).toMatchObject({ variant: "active", expanded: [], root: null, q: "", status: "" });
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
});
