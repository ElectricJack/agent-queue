import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GraphCanvas from "../GraphCanvas";
import type { TaskNodeData, PlaybookNodeData } from "../types";
import { edge, graph, task } from "./fixtures";
import { setExpandedTaskIds } from "../useGraphHierarchy";

interface FlowProps {
  nodes: Node<TaskNodeData | PlaybookNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: TaskNodeData | PlaybookNodeData; selected: boolean }>>;
  children: ReactNode;
  onNodeClick: (event: MouseEvent, node: Node<TaskNodeData | PlaybookNodeData>) => void;
  onPaneClick?: (event: MouseEvent) => void;
  nodesDraggable?: boolean;
  nodesConnectable?: boolean;
  deleteKeyCode?: string | null;
  fitView?: boolean;
  defaultViewport?: { x: number; y: number; zoom: number };
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null }));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => {
    flow.current = props;
    return <div>
      <button type="button" aria-label="Blank canvas" onClick={(event) => props.onPaneClick?.(event)} />
      {props.nodes.map((node) => {
        const NodeView = props.nodeTypes[node.type ?? "task"]!;
        return (
        <div key={node.id} data-testid={`node-${node.id}`} onClick={(event) => props.onNodeClick(event, node)}>
          <NodeView id={node.id} data={node.data} selected={Boolean(node.selected)} />
        </div>
      ); })}
      {props.children}
    </div>;
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));
vi.mock("../AgentAvatarLayer", () => ({ default: () => null }));

beforeEach(() => {
  flow.current = null;
  localStorage.clear();
  // The expanded set is one live store, not per-component state.
  setExpandedTaskIds(new Set());
});
afterEach(cleanup);

describe("GraphCanvas interactions", () => {
  it("opens a task from title, metadata, and card padding without draggable nodes or duplicate clicks", () => {
    const open = vi.fn();
    render(<GraphCanvas graph={graph([task("one", {
      title: "Investigate queue", profile_id: "coder", intelligence_class: "standard-high",
    })])} onTaskClick={open} selectedTaskId="one" />);
    fireEvent.click(screen.getByText("Investigate queue"));
    fireEvent.click(screen.getByText("coder"));
    fireEvent.click(screen.getByText("standard-high"));
    fireEvent.click(screen.getByTestId("node-one"));
    expect(open.mock.calls).toEqual([["one"], ["one"], ["one"], ["one"]]);
    expect(screen.getByRole("button", { name: "Open task Investigate queue" })).toHaveAttribute("aria-pressed", "true");
    expect(flow.current).toMatchObject({ nodesDraggable: false, nodesConnectable: false, deleteKeyCode: null });
  });

  it("expands and collapses children independently from opening their task pane", () => {
    const open = vi.fn();
    const data = graph([task("parent"), task("child", { status: "IN_PROGRESS" })], [edge("child", "parent")]);
    render(<GraphCanvas graph={data} onTaskClick={open} />);
    expect(screen.queryByText("Task child")).not.toBeInTheDocument();
    expect(screen.getByText("0/1 descendants completed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand children of Task parent" }));
    expect(screen.getByText("Task child")).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Task child"));
    expect(open).toHaveBeenCalledExactlyOnceWith("child");
    fireEvent.click(screen.getByRole("button", { name: "Collapse children of Task parent" }));
    expect(screen.queryByText("Task child")).not.toBeInTheDocument();
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("restores expanded subtrees for the same viewer", () => {
    const data = graph([task("parent"), task("child")], [edge("child", "parent")]);
    const first = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand children of Task parent" }));
    expect(screen.getByText("Task child")).toBeInTheDocument();
    first.unmount();

    render(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    expect(screen.getByText("Task child")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse children of Task parent" })).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps a collapsed parent closed while live children update its hidden rollup", () => {
    const data = graph([task("parent"), task("child")], [edge("child", "parent")]);
    const { rerender } = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    expect(screen.getByText("1 hidden")).toBeInTheDocument();

    rerender(<GraphCanvas graph={graph(
      [task("parent"), task("child"), task("new-child", { status: "IN_PROGRESS" })],
      [edge("child", "parent"), edge("new-child", "parent")],
    )} onTaskClick={vi.fn()} />);

    expect(screen.queryByText("Task child")).not.toBeInTheDocument();
    expect(screen.queryByText("Task new-child")).not.toBeInTheDocument();
    expect(screen.getByText("2 hidden")).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
  });

  it("animates node positions when hierarchy projection changes", () => {
    const data = graph([task("parent"), task("child"), task("other")], [edge("child", "parent")]);
    render(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    expect(flow.current!.nodes.every((node) => node.style?.transitionProperty === "transform")).toBe(true);
    expect(flow.current!.nodes.every((node) => node.style?.transitionDuration === "200ms")).toBe(true);
  });

  it("clears selection on blank canvas or Escape without automatically selecting the first task again", () => {
    const clear = vi.fn();
    const data = graph([task("one"), task("two")]);
    const { rerender } = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} onBackgroundClick={clear} />);
    fireEvent.click(screen.getByText("Task one"));
    expect(screen.getByRole("button", { name: "Open task Task one" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Blank canvas" }));
    expect(clear).toHaveBeenCalledTimes(1);
    rerender(<GraphCanvas graph={{ ...data }} onTaskClick={vi.fn()} onBackgroundClick={clear} />);
    expect(flow.current?.nodes.every((node) => !node.selected && !node.className?.includes("aq-focused"))).toBe(true);
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "Escape" });
    expect(clear).toHaveBeenCalledTimes(2);
  });

  it("clears the focus outline when selection is cleared outside the canvas", () => {
    const data = graph([task("one")]);
    const { rerender } = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} selectedTaskId="one" />);
    fireEvent.click(screen.getByText("Task one"));
    expect(flow.current!.nodes[0]!.className).toBe("aq-focused");
    rerender(<GraphCanvas graph={data} onTaskClick={vi.fn()} selectedTaskId={null} />);
    expect(flow.current!.nodes[0]!.selected).toBe(false);
    expect(flow.current!.nodes[0]!.className).not.toBe("aq-focused");
  });

  it("keeps the actual running state visible when new dependencies block a task", () => {
    render(<GraphCanvas graph={graph([task("one", { status: "IN_PROGRESS", is_blocked: true })])} onTaskClick={vi.fn()} />);
    expect(screen.getByText("IN PROGRESS")).toBeInTheDocument();
    expect(screen.getByLabelText("Blocked by dependencies or gates")).toBeInTheDocument();
  });

  it("keeps dependency-blocked defined tasks pale yellow while actual blocked tasks stay amber", () => {
    render(<GraphCanvas graph={graph([
      task("defined", { status: "DEFINED", is_blocked: true }),
      task("blocked", { status: "BLOCKED" }),
    ])} onTaskClick={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Open task Task defined" }).closest("[data-task-card]")).toHaveClass("border-yellow-300");
    expect(screen.getByRole("button", { name: "Open task Task blocked" }).closest("[data-task-card]")).toHaveClass("border-amber-500");
    expect(screen.getByLabelText("Blocked by dependencies or gates")).toBeInTheDocument();
  });

  it("keeps terminal colors and labels despite stale blocked flags", () => {
    render(<GraphCanvas graph={graph([
      task("done", { status: "COMPLETED", is_blocked: true }),
      task("failed", { status: "FAILED", is_blocked: true }),
    ])} onTaskClick={vi.fn()} />);
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open task Task done" }).closest("[data-task-card]")).toHaveClass("border-emerald-500");
    expect(screen.getByRole("button", { name: "Open task Task failed" }).closest("[data-task-card]")).toHaveClass("border-red-500");
    expect(screen.queryByLabelText("Blocked by dependencies or gates")).not.toBeInTheDocument();
  });

  it("supports arrow navigation and Enter", () => {
    const open = vi.fn();
    render(<GraphCanvas graph={graph([task("one"), task("two")])} onTaskClick={open} />);
    fireEvent.click(screen.getByText("Task one"));
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "ArrowRight" });
    fireEvent.keyDown(screen.getByRole("region", { name: "Task graph" }), { key: "Enter" });
    expect(open.mock.calls).toEqual([["one"], ["two"]]);
  });

  it("keeps native Enter activation separate for expansion and task buttons", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    render(<GraphCanvas graph={graph([task("parent"), task("child")], [edge("child", "parent")])} onTaskClick={open} />);
    screen.getByRole("button", { name: "Expand children of Task parent" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByText("Task child")).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    screen.getByRole("button", { name: "Open task Task parent" }).focus();
    await user.keyboard("{Enter}");
    expect(open).toHaveBeenCalledExactlyOnceWith("parent");
  });

  it("retains expansion, card positions, and the camera policy through live status changes and arrivals", () => {
    const data = graph([task("parent"), task("child"), task("other")], [edge("child", "parent")]);
    const { rerender } = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand children of Task parent" }));
    const positions = flow.current!.nodes.map(({ id, position }) => ({ id, position }));
    const viewport = flow.current!.defaultViewport;
    rerender(<GraphCanvas graph={{
      ...data, tasks: [task("new", { priority: 1 }), task("other"), task("child", { status: "COMPLETED" }), task("parent")],
    }} onTaskClick={vi.fn()} />);
    expect(screen.getByText("1/1 descendants completed")).toBeInTheDocument();
    for (const node of positions) {
      expect(flow.current!.nodes.find((candidate) => candidate.id === node.id)?.position).toEqual(node.position);
    }
    expect(flow.current!.fitView).not.toBe(true);
    expect(flow.current!.defaultViewport).toEqual(viewport);
    expect(screen.getByText("Zoom controls")).toBeInTheDocument();
  });

  it("reveals a filtered child with its ancestors while leaving the saved collapse state intact", () => {
    const data = graph([task("parent"), task("child")], [edge("child", "parent")]);
    const { rerender } = render(<GraphCanvas graph={data} onTaskClick={vi.fn()} matchingTaskIds={new Set(["child"])} filtering />);
    expect(screen.getByText("Task child")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse children of Task parent" })).toBeDisabled();
    rerender(<GraphCanvas graph={data} onTaskClick={vi.fn()} />);
    expect(screen.queryByText("Task child")).not.toBeInTheDocument();
  });
});

it("renders persistent playbook nodes before task rows, keeps their positions after completion, and never opens them as tasks", () => {
  const openTask = vi.fn(), openPlaybook = vi.fn();
  const definitions = Array.from({ length: 5 }, (_, i) => ({ id: `book-${i}`, scope: "system", triggers: ["timer.24h"], running_count: 1 }));
  const props = { graph: graph([task("one")]), onTaskClick: openTask, onPlaybookClick: openPlaybook };
  const view = render(<GraphCanvas {...props} playbooks={definitions} selectedPlaybookId="book-0" />);
  const positions = flow.current!.nodes.map(n => ({ id: n.id, position: n.position }));
  expect(positions.find(n => n.id === "one")!.position.y).toBeGreaterThan(Math.max(...positions.filter(n => n.id.startsWith("playbook:")).map(n => n.position.y)));
  fireEvent.click(screen.getByTestId("node-playbook:book-0"));
  fireEvent.click(screen.getByRole("button", { name: "Open playbook book-0" }));
  expect(openPlaybook.mock.calls).toEqual([["book-0"], ["book-0"]]);
  expect(openTask).not.toHaveBeenCalled();
  view.rerender(<GraphCanvas {...props} playbooks={definitions.map(p => ({ ...p, running_count: 0, last_run: { run_id: "r", status: "completed" } }))} />);
  expect(flow.current!.nodes.map(n => ({ id: n.id, position: n.position }))).toEqual(positions);
  expect(screen.getAllByText("Waiting for trigger")).toHaveLength(5);
  screen.getByRole("button", { name: "Open playbook book-0" }).focus();
  fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
  expect(screen.getByRole("button", { name: "Open playbook book-1" })).toHaveFocus();
});
