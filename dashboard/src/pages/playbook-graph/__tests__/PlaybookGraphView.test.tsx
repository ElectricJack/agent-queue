import { useState, type ComponentType, type MouseEvent, type ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaybookGraphView from "../PlaybookGraphView";
import type { PlaybookGraphNodeData } from "../types";
import { REVIEW_PROMPT, edge, graph, layout, node } from "./fixtures";

interface FlowProps {
  nodes: Node<PlaybookGraphNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: PlaybookGraphNodeData; selected: boolean }>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
}

const flow = vi.hoisted(() => ({ instances: 0 }));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => {
    // Stable per-mount id: it changes only if React tears the canvas down.
    const [instance] = useState(() => (flow.instances += 1));
    return (
      <div data-testid="flow" data-instance={instance}>
        <button type="button" aria-label="Blank canvas" onClick={(event) => props.onPaneClick?.(event)} />
        {props.nodes.map((n) => {
          const NodeView = props.nodeTypes[n.type ?? "playbookStep"]!;
          return (
            <div key={n.id} data-testid={`node-${n.id}`} data-position={`${n.position.x},${n.position.y}`}>
              <NodeView id={n.id} data={n.data} selected={Boolean(n.selected)} />
            </div>
          );
        })}
        {props.children}
      </div>
    );
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

const query = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
  refetch: vi.fn(),
}));
vi.mock("../../../api/hooks", () => ({
  usePlaybookGraph: (playbookId?: string) => {
    query.state.playbookId = playbookId;
    return { ...query.state, refetch: query.refetch };
  },
}));

function success(data: unknown) {
  query.state = { data, isPending: false, isError: false, error: null };
}

beforeEach(() => {
  query.refetch.mockReset();
  flow.instances = 0;
  success({ success: true, playbook: { id: "review-flow" }, graph, layout, legend: {} });
});
afterEach(cleanup);

describe("PlaybookGraphView", () => {
  it("shows the loading message while the compiled graph is being fetched", () => {
    query.state = { data: undefined, isPending: true, isError: false, error: null };
    render(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByText("Loading compiled graph…")).toBeInTheDocument();
    expect(screen.queryByTestId("flow")).not.toBeInTheDocument();
  });

  it("shows a concise error with Retry instead of an empty canvas", async () => {
    const user = userEvent.setup();
    query.state = { data: undefined, isPending: false, isError: true, error: new Error("playbook not compiled") };
    render(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByText(/could not load the compiled graph/i)).toBeInTheDocument();
    expect(screen.getByText(/playbook not compiled/)).toBeInTheDocument();
    expect(screen.queryByTestId("flow")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(query.refetch).toHaveBeenCalledTimes(1);
  });

  it("shows the empty-graph message for a compiled playbook with no nodes", () => {
    success({ success: true, playbook: { id: "review-flow" }, graph: { nodes: [], edges: [] }, layout, legend: {} });
    render(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByText("This compiled playbook has no nodes.")).toBeInTheDocument();
  });

  it("surfaces the incomplete-data state when an edge endpoint is missing", () => {
    success({
      success: true,
      playbook: { id: "review-flow" },
      graph: { nodes: [node("only")], edges: [edge("only", "ghost")] },
      layout: { direction: "TD", grid_positions: {} },
      legend: {},
    });
    render(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByRole("button", { name: "Inspect node only" })).toBeInTheDocument();
    expect(screen.getByText(/1 edge references a missing node/i)).toBeInTheDocument();
  });

  it("starts with no selection and inspects the real node that was activated", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    const inspector = screen.getByRole("complementary", { name: "Node inspector" });
    expect(within(inspector).getByRole("heading", { name: "review" })).toBeInTheDocument();
    expect(within(inspector).getByText(REVIEW_PROMPT, { collapseWhitespace: false }).textContent).toBe(REVIEW_PROMPT);
    expect(within(inspector).getByText("diff_is_clean")).toBeInTheDocument();
    expect(within(inspector).getByText("claude-opus-5")).toBeInTheDocument();
    expect(within(inspector).getByText(/"items": "changed_files"/)).toBeInTheDocument();
  });

  it("clears the inspector on a blank-canvas click without refetching or remounting the canvas", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);
    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    const instanceWhenSelected = screen.getByTestId("flow").dataset.instance;

    fireEvent.click(screen.getByRole("button", { name: "Blank canvas" }));
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(query.refetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("flow").dataset.instance).toBe(instanceWhenSelected);
  });

  it("clears the inspector on Escape without refetching or remounting the canvas", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);
    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    const instanceWhenSelected = screen.getByTestId("flow").dataset.instance;

    screen.getByRole("region", { name: "Playbook graph" }).focus();
    await user.keyboard("{Escape}");
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(query.refetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("flow").dataset.instance).toBe(instanceWhenSelected);
  });

  it("keeps node positions and the camera untouched on a selection-only rerender", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);
    const before = screen.getByTestId("node-review").dataset.position;
    const instanceBefore = screen.getByTestId("flow").dataset.instance;

    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    expect(screen.getByTestId("node-review").dataset.position).toBe(before);
    expect(screen.getByTestId("flow").dataset.instance).toBe(instanceBefore);
  });

  it("drops the selection when refreshed graph data no longer contains the node", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<PlaybookGraphView playbookId="review-flow" />);
    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    expect(screen.getByRole("heading", { name: "review" })).toBeInTheDocument();

    success({
      success: true,
      playbook: { id: "review-flow" },
      graph: { nodes: graph.nodes!.filter((n) => n.id !== "review"), edges: [] },
      layout,
      legend: {},
    });
    rerender(<PlaybookGraphView playbookId="review-flow" />);
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
  });

  it("drops the selection when the page switches to a different playbook", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<PlaybookGraphView playbookId="review-flow" />);
    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    rerender(<PlaybookGraphView playbookId="other-flow" />);
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(query.state.playbookId).toBe("other-flow");
  });
});
