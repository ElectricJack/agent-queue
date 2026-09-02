/** Expand/collapse is owned by the user, never by the viewport.
 *
 *  The operator report behind these tests: "zooming out collapses epics, it is
 *  jarring and disorienting". Nesting must change only on an explicit user
 *  action (the container toggle) — never as a side effect of zoom, pan,
 *  resize or a live graph refresh. A future level-of-detail mode may simplify
 *  how a tile is drawn, but it may not mutate the expanded set. These tests
 *  are the guard rail for that invariant.
 */
import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GraphCanvas from "../GraphCanvas";
import { useGraphHierarchy } from "../useGraphHierarchy";
import { layoutGraph } from "../layout";
import type { PlaybookNodeData, TaskNodeData } from "../types";
import { edge, graph, task } from "./fixtures";

interface FlowProps {
  nodes: Node<TaskNodeData | PlaybookNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: TaskNodeData | PlaybookNodeData; selected: boolean }>>;
  children: ReactNode;
  onNodeClick: (event: MouseEvent, node: Node<TaskNodeData | PlaybookNodeData>) => void;
  onPaneClick?: (event: MouseEvent) => void;
  defaultViewport?: { x: number; y: number; zoom: number };
  minZoom?: number;
  maxZoom?: number;
  /** Not passed today. If a viewport handler is ever added, these tests drive
   *  it so a zoom-driven collapse cannot land unnoticed. */
  onMove?: (event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
  onMoveEnd?: (event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
  onViewportChange?: (viewport: { x: number; y: number; zoom: number }) => void;
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null }));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => {
    flow.current = props;
    return <div>
      {props.nodes.map((node) => {
        const NodeView = props.nodeTypes[node.type ?? "task"]!;
        return (
          <div key={node.id} data-testid={`node-${node.id}`} onClick={(event) => props.onNodeClick(event, node)}>
            <NodeView id={node.id} data={node.data} selected={Boolean(node.selected)} />
          </div>
        );
      })}
      {props.children}
    </div>;
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));
vi.mock("../AgentAvatarLayer", () => ({ default: () => null }));

const resizes: ResizeObserverCallback[] = [];
class RecordingResizeObserver {
  constructor(callback: ResizeObserverCallback) { resizes.push(callback); }
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** Everything the viewport can do to the canvas, short of a user click:
 *  a zoom (React Flow's viewport), and the resize that a zoom or a window
 *  change produces. */
function changeViewport(zoom: number, width: number) {
  const viewport = { x: 0, y: 0, zoom };
  const region = screen.getByRole("region", { name: "Task graph" });
  act(() => {
    flow.current?.onMove?.(new Event("wheel"), viewport);
    flow.current?.onMoveEnd?.(new Event("wheel"), viewport);
    flow.current?.onViewportChange?.(viewport);
    fireEvent.wheel(region, { deltaY: 240, ctrlKey: true });
    for (const callback of resizes) {
      callback([{ contentRect: { width } } as ResizeObserverEntry], {} as ResizeObserver);
    }
  });
}

const epicGraph = () => graph(
  [task("epic", { title: "Epic" }), task("child", { title: "Child" }),
    task("other", { title: "Other epic" }), task("other-child", { title: "Other child" })],
  [edge("child", "epic"), edge("other-child", "other")],
);

let originalResizeObserver: typeof ResizeObserver;
beforeEach(() => {
  flow.current = null;
  resizes.length = 0;
  localStorage.clear();
  originalResizeObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = RecordingResizeObserver as unknown as typeof ResizeObserver;
});
afterEach(() => {
  cleanup();
  globalThis.ResizeObserver = originalResizeObserver;
});

describe("expanded state ownership", () => {
  it("never changes the expanded set when the graph refreshes or the hook remounts", () => {
    const first = renderHook((props: { graph: ReturnType<typeof epicGraph> }) => useGraphHierarchy(props), {
      initialProps: { graph: epicGraph() },
    });
    expect(first.result.current.projection.details.get("epic")!.expanded).toBe(false);

    act(() => first.result.current.toggleExpanded("epic"));
    expect(first.result.current.projection.details.get("epic")!.expanded).toBe(true);

    // A live refresh delivers a new graph object with a new task.
    const refreshed = graph(
      [...epicGraph().tasks, task("late", { title: "Late arrival" })],
      epicGraph().edges,
    );
    first.rerender({ graph: refreshed });
    expect(first.result.current.projection.details.get("epic")!.expanded).toBe(true);
    expect(first.result.current.projection.details.get("other")!.expanded).toBe(false);

    // A remount (tab switch, page reload, desktop/mobile view swap) restores
    // the same choices from storage.
    first.unmount();
    const second = renderHook(() => useGraphHierarchy({ graph: epicGraph() }));
    expect(second.result.current.projection.details.get("epic")!.expanded).toBe(true);
    expect(second.result.current.projection.details.get("other")!.expanded).toBe(false);
  });

  it("projects the same nesting at every canvas width", () => {
    const expandedTaskIds = new Set(["epic"]);
    const visibleAt = (columns: number) =>
      layoutGraph(epicGraph(), { columns, expandedTaskIds }).nodes.map((node) => node.id).sort();
    expect(visibleAt(1)).toEqual(visibleAt(4));
    expect(visibleAt(1)).toContain("child");
    expect(layoutGraph(epicGraph(), { columns: 1 }).nodes.map((node) => node.id)).not.toContain("child");
  });
});

describe("GraphCanvas zoom", () => {
  it("keeps a collapsed epic collapsed and an expanded one expanded across zoom and a live refresh", async () => {
    const user = userEvent.setup();
    const view = render(<GraphCanvas graph={epicGraph()} onTaskClick={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Expand children of Epic" }));
    expect(screen.getByTestId("node-child")).toBeInTheDocument();
    expect(screen.queryByTestId("node-other-child")).not.toBeInTheDocument();

    changeViewport(0.2, 320);   // zoom all the way out, narrow canvas
    expect(screen.getByTestId("node-child")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse children of Epic" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Expand children of Other epic" })).toHaveAttribute("aria-expanded", "false");

    // A live graph refresh at the zoomed-out scale must not re-derive nesting.
    view.rerender(<GraphCanvas graph={epicGraph()} onTaskClick={vi.fn()} />);
    expect(screen.getByTestId("node-child")).toBeInTheDocument();

    changeViewport(2, 1600);    // and back in
    expect(screen.getByTestId("node-child")).toBeInTheDocument();
    expect(screen.queryByTestId("node-other-child")).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("aq:command-center:expanded-task-ids:v1") ?? "[]")).toEqual(["epic"]);
  });
});
