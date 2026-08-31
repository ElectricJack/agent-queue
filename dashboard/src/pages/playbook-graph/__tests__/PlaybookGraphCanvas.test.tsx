import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaybookGraphCanvas from "../PlaybookGraphCanvas";
import type { PlaybookGraphNodeData } from "../types";
import { edge, graph, layout, node } from "./fixtures";

interface FlowProps {
  nodes: Node<PlaybookGraphNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: PlaybookGraphNodeData; selected: boolean }>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
  nodesDraggable?: boolean;
  nodesConnectable?: boolean;
  edgesReconnectable?: boolean;
  deleteKeyCode?: string | null;
  fitView?: boolean;
  viewport?: unknown;
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null, mounts: 0 }));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => {
    flow.current = props;
    return <div data-testid="flow">
      <button type="button" aria-label="Blank canvas" onClick={(event) => props.onPaneClick?.(event)} />
      {props.nodes.map((n) => {
        const NodeView = props.nodeTypes[n.type ?? "playbookStep"]!;
        return <div key={n.id} data-testid={`node-${n.id}`} data-position={`${n.position.x},${n.position.y}`}>
          <NodeView id={n.id} data={n.data} selected={Boolean(n.selected)} />
        </div>;
      })}
      <ul data-testid="edges">
        {props.edges.map((e) => (
          <li key={e.id} data-testid={`edge-${e.source}-${e.target}-${String(e.data?.edgeType)}`}>
            {e.label ? String(e.label) : ""}
          </li>
        ))}
      </ul>
      {props.children}
    </div>;
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

beforeEach(() => { flow.current = null; });
afterEach(cleanup);

describe("PlaybookGraphCanvas", () => {
  it("renders every compiled node with its id, type badge and prompt preview", () => {
    render(<PlaybookGraphCanvas graph={graph} layout={layout} onSelectNode={vi.fn()} />);
    for (const id of ["triage", "review", "approve", "escalate", "done"]) {
      expect(screen.getByRole("button", { name: `Inspect node ${id}` })).toBeInTheDocument();
    }
    expect(screen.getByText("entry + decision")).toBeInTheDocument();
    expect(screen.getByText("human checkpoint")).toBeInTheDocument();
    expect(screen.getByText("terminal")).toBeInTheDocument();
    expect(screen.getByText("decision")).toBeInTheDocument();
    expect(screen.getAllByText("action")).toHaveLength(1);
    expect(screen.getByText("Classify the incoming task")).toBeInTheDocument();
    expect(screen.getByText("Review the diff")).toBeInTheDocument();
  });

  it("renders every directed edge with its literal label and a kind-specific stroke", () => {
    render(<PlaybookGraphCanvas graph={graph} layout={layout} onSelectNode={vi.fn()} />);
    expect(screen.getByTestId("edge-triage-review-condition")).toHaveTextContent("needs_review");
    expect(screen.getByTestId("edge-triage-approve-otherwise")).toHaveTextContent("otherwise");
    expect(screen.getByTestId("edge-review-escalate-timeout")).toHaveTextContent("timeout");
    expect(screen.getByTestId("edge-review-approve-goto")).toHaveTextContent("");
    expect(screen.getByTestId("edge-approve-done-goto")).toBeInTheDocument();
    expect(screen.getByTestId("edges").children).toHaveLength(6);
  });

  it("is a read-only canvas with zoom controls", () => {
    render(<PlaybookGraphCanvas graph={graph} layout={layout} onSelectNode={vi.fn()} />);
    expect(screen.getByText("Zoom controls")).toBeInTheDocument();
    expect(flow.current).toMatchObject({
      nodesDraggable: false,
      nodesConnectable: false,
      edgesReconnectable: false,
      deleteKeyCode: null,
      fitView: true,
    });
  });

  it("reports the activated node id on pointer, Enter and Space", async () => {
    const user = userEvent.setup();
    const onSelectNode = vi.fn();
    render(<PlaybookGraphCanvas graph={graph} layout={layout} onSelectNode={onSelectNode} />);
    const card = screen.getByRole("button", { name: "Inspect node review" });
    await user.click(card);
    card.focus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onSelectNode.mock.calls).toEqual([["review"], ["review"], ["review"]]);
  });

  it("marks the selected node and clears on Escape or a blank-canvas click", async () => {
    const user = userEvent.setup();
    const onSelectNode = vi.fn();
    const { rerender } = render(
      <PlaybookGraphCanvas graph={graph} layout={layout} selectedNodeId="review" onSelectNode={onSelectNode} />,
    );
    expect(screen.getByRole("button", { name: "Inspect node review" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Inspect node done" })).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(screen.getByRole("button", { name: "Blank canvas" }));
    expect(onSelectNode).toHaveBeenLastCalledWith(null);

    rerender(<PlaybookGraphCanvas graph={graph} layout={layout} selectedNodeId="review" onSelectNode={onSelectNode} />);
    screen.getByRole("region", { name: "Playbook graph" }).focus();
    await user.keyboard("{Escape}");
    expect(onSelectNode).toHaveBeenLastCalledWith(null);
  });

  it("leaves the camera alone on a selection-only rerender", () => {
    const onSelectNode = vi.fn();
    const { rerender } = render(
      <PlaybookGraphCanvas graph={graph} layout={layout} onSelectNode={onSelectNode} />,
    );
    const before = screen.getByTestId("node-review").dataset.position;
    expect(flow.current?.viewport).toBeUndefined();

    rerender(
      <PlaybookGraphCanvas graph={graph} layout={layout} selectedNodeId="review" onSelectNode={onSelectNode} />,
    );
    expect(screen.getByTestId("node-review").dataset.position).toBe(before);
    expect(screen.getByTestId("flow")).toBeInTheDocument();
    // fitView applies once on init; a selection rerender must not re-fit.
    expect(flow.current?.viewport).toBeUndefined();
  });

  it("reports incomplete data rather than crashing when an edge endpoint is missing", () => {
    render(
      <PlaybookGraphCanvas
        graph={{ nodes: [node("only")], edges: [edge("only", "ghost")] }}
        layout={{ direction: "TD", grid_positions: {} }}
        onSelectNode={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Inspect node only" })).toBeInTheDocument();
    expect(screen.getByText(/1 edge references a missing node/i)).toBeInTheDocument();
  });

  it("shows the empty-graph message for a compiled playbook with no nodes", () => {
    render(<PlaybookGraphCanvas graph={{ nodes: [], edges: [] }} layout={undefined} onSelectNode={vi.fn()} />);
    expect(screen.getByText("This compiled playbook has no nodes.")).toBeInTheDocument();
  });
});
