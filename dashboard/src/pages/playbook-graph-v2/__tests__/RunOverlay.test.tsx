import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PlaybookSemanticGraphCanvas from "../PlaybookSemanticGraphCanvas";
import { layoutSemanticGraph, overlayAppliesTo } from "../layout";
import { TRAVERSED_EDGE_WIDTH, UNTRAVERSED_EDGE_OPACITY, UNVISITED_NODE_CLASS } from "../types";
import { foreignRunOverlay, graph, runOverlay } from "./fixtures";

interface FlowProps {
  nodes: Node<Record<string, unknown>>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<Record<string, unknown>>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
  onNodeClick?: (event: MouseEvent, node: { id: string; type?: string }) => void;
}
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => (
    <div data-testid="flow">
      {props.nodes.map((n) => {
        const NodeView = props.nodeTypes[n.type!]!;
        return (
          <div key={n.id} data-testid={`node-${n.id}`}>
            <NodeView id={n.id} data={n.data} selected={Boolean(n.selected)} width={n.width} height={n.height} />
          </div>
        );
      })}
      <ul data-testid="edges">
        {props.edges.map((e) => (
          <li key={e.id} data-testid={`edge-${e.id}`} aria-label={String(e.ariaLabel)}>
            {e.label ? String(e.label) : ""}
          </li>
        ))}
      </ul>
      {props.children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Controls: () => null,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

const TRAVERSED = new Set(runOverlay.edges!.map((e) => e.edge_id));
const VISITED = new Set(runOverlay.nodes!.map((n) => n.step_id));

afterEach(cleanup);

describe("run overlay on the semantic graph", () => {
  it("applies a run only to the exact artifact it pinned", () => {
    expect(overlayAppliesTo(graph, runOverlay)).toBe(true);
    expect(overlayAppliesTo(graph, foreignRunOverlay)).toBe(false);
    expect(overlayAppliesTo(graph, undefined)).toBe(false);
    // A projection with no artifact ref cannot prove the match, so it is not one.
    expect(overlayAppliesTo({ nodes: graph.nodes }, runOverlay)).toBe(false);
  });

  it("decorates the traversed nodes and leaves the rest visibly untouched", () => {
    const result = layoutSemanticGraph(graph, runOverlay);
    expect(result.overlayApplied).toBe(true);
    expect(result.overlayMismatch).toBe(false);

    const steps = result.nodes.filter((n) => "node" in n.data);
    for (const step of steps) {
      const data = step.data as { overlay?: { step_id: string }; overlayApplied?: boolean };
      expect(data.overlayApplied).toBe(true);
      expect(data.overlay?.step_id).toBe(VISITED.has(step.id) ? step.id : undefined);
    }
  });

  it("weights traversed edges, fades untraversed ones and keeps every dash pattern", () => {
    const plain = layoutSemanticGraph(graph);
    const result = layoutSemanticGraph(graph, runOverlay);

    for (const edge of result.edges) {
      const dashes = plain.edges.find((e) => e.id === edge.id)!.style!.strokeDasharray;
      // Weight and opacity carry the run; the dash pattern still carries the kind.
      expect(edge.style!.strokeDasharray).toBe(dashes);
      if (TRAVERSED.has(edge.id)) {
        expect(edge.style!.strokeWidth).toBe(TRAVERSED_EDGE_WIDTH);
        expect(edge.style!.strokeOpacity).toBe(1);
        expect(edge.data!.traversed).toBe(true);
      } else {
        expect(edge.style!.strokeOpacity).toBe(UNTRAVERSED_EDGE_OPACITY);
        expect(edge.data!.traversalCount).toBe(0);
      }
    }
  });

  it("counts loop traversals on the edge and loop iterations on the card", () => {
    const result = layoutSemanticGraph(graph, runOverlay);
    const body = result.edges.find((e) => e.id === "sweep-on-spec-approved::for-each-task::body")!;
    expect(body.data!.traversalCount).toBe(3);
    expect(body.label).toBe("each task ×3");
    expect(String(body.ariaLabel)).toContain("traversed 3 times in this run");

    // A single traversal adds no count: "×1" is noise, not information.
    const listed = result.edges.find((e) => e.id === "sweep-on-spec-approved::list-downstream::listed")!;
    expect(listed.label).toBe("listed");
    expect(String(listed.ariaLabel)).toContain("traversed 1 time in this run");

    render(<PlaybookSemanticGraphCanvas graph={graph} overlay={runOverlay} onSelectNode={vi.fn()} />);
    const loop = within(screen.getByTestId("node-for-each-task"));
    expect(loop.getByText("3 iterations")).toBeInTheDocument();
    expect(loop.getByText("3 visits")).toBeInTheDocument();
    // Per-iteration outcomes stay legible rather than collapsing into one status.
    expect(loop.getByText("3 iterations")).toHaveAttribute(
      "title",
      "0: task-a → created\n1: task-b → reused\n2: task-c → created",
    );
  });

  it("labels each card's run state and dims the steps the run never reached", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} overlay={runOverlay} onSelectNode={vi.fn()} />);

    const visited = screen.getByRole("button", { name: /Inspect step Open a spec-ingest gate/ });
    expect(visited).toHaveAttribute("data-run-state", "completed");
    expect(visited).toHaveAttribute("data-visit-count", "3");
    expect(visited.getAttribute("aria-label")).toContain("run state completed");
    expect(visited.className).not.toContain(UNVISITED_NODE_CLASS);

    // The whole artifact stays on the canvas; the untouched rule just recedes.
    const untouched = screen.getByRole("button", { name: /Inspect step Classify review risk/ });
    expect(untouched).toHaveAttribute("data-run-state", "not_visited");
    expect(untouched.className).toContain(UNVISITED_NODE_CLASS);
    expect(within(untouched).getByText("not visited")).toBeInTheDocument();
  });

  it("names the run and says the run's artifact is older than the active one", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} overlay={runOverlay} onSelectNode={vi.fn()} />);
    const legend = within(screen.getByRole("region", { name: "Run overlay" }));
    expect(legend.getByText("run-42")).toBeInTheDocument();
    expect(legend.getByText("completed")).toBeInTheDocument();
    expect(legend.getByText("older than the active one")).toBeInTheDocument();
  });

  it("refuses a run pinned to another artifact instead of mis-attributing it", () => {
    const result = layoutSemanticGraph(graph, foreignRunOverlay);
    expect(result.overlayApplied).toBe(false);
    expect(result.overlayMismatch).toBe(true);
    expect(result.edges).toEqual(layoutSemanticGraph(graph).edges);

    render(<PlaybookSemanticGraphCanvas graph={graph} overlay={foreignRunOverlay} onSelectNode={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /Run state is not shown: this run executed artifact/,
    );
    expect(screen.queryByRole("region", { name: "Run overlay" })).not.toBeInTheDocument();
    expect(document.querySelectorAll("[data-run-state]")).toHaveLength(0);
  });

  it("draws no run state at all when no run is selected", () => {
    const result = layoutSemanticGraph(graph);
    expect(result.overlayApplied).toBe(false);
    expect(result.overlayMismatch).toBe(false);

    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    expect(document.querySelectorAll("[data-run-state]")).toHaveLength(0);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
