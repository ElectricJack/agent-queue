import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, EdgeChange, Node } from "@xyflow/react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaybookSemanticGraphCanvas from "../PlaybookSemanticGraphCanvas";
import { EDGE_KIND_STYLES, RULE_CLUSTER_NODE_TYPE, SEMANTIC_NODE_TYPE } from "../types";
import { graph } from "./fixtures";

interface FlowProps {
  nodes: Node<Record<string, unknown>>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<Record<string, unknown>>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
  onNodeClick?: (event: MouseEvent, node: { id: string; type?: string }) => void;
  onEdgesChange?: (changes: EdgeChange[]) => void;
  nodesDraggable?: boolean;
  elementsSelectable?: boolean;
  edgesFocusable?: boolean;
  disableKeyboardA11y?: boolean;
  deleteKeyCode?: string | null;
  fitView?: boolean;
  minZoom?: number;
  maxZoom?: number;
  panOnScroll?: boolean;
  viewport?: unknown;
}

const flow = vi.hoisted(() => ({ current: null as FlowProps | null, mounts: 0 }));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => {
    flow.current = props;
    flow.mounts += 1;
    return (
      <div data-testid="flow">
        <button type="button" aria-label="Blank canvas" onClick={(e) => props.onPaneClick?.(e)} />
        {props.nodes.map((n) => {
          const NodeView = props.nodeTypes[n.type!]!;
          return (
            <div
              key={n.id}
              data-testid={`node-${n.id}`}
              data-node-type={n.type}
              data-parent={n.parentId ?? ""}
              data-position={`${n.position.x},${n.position.y}`}
            >
              <button
                type="button"
                aria-label={`pane ${n.id}`}
                onClick={(e) => props.onNodeClick?.(e, { id: n.id, type: n.type })}
              />
              <NodeView
                id={n.id}
                data={n.data}
                selected={Boolean(n.selected)}
                width={n.width}
                height={n.height}
              />
            </div>
          );
        })}
        <ul data-testid="edges">
          {props.edges.map((e) => (
            <li key={e.id} data-testid={`edge-${e.id}`} aria-label={String(e.ariaLabel)}>
              {/* Stands in for the `<g role="button" tabIndex={0}>` xyflow
                  renders for a focusable, selectable edge: click and Enter
                  both reach it through the same `select` change. */}
              <button
                type="button"
                aria-label={`select edge ${e.id}`}
                aria-pressed={Boolean(e.selected)}
                onClick={() =>
                  props.onEdgesChange?.([
                    { id: e.id, type: "select", selected: !e.selected },
                  ])
                }
              />
              {e.label ? String(e.label) : ""}
            </li>
          ))}
        </ul>
        {props.children}
      </div>
    );
  },
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

beforeEach(() => {
  flow.current = null;
  flow.mounts = 0;
});
afterEach(cleanup);

describe("PlaybookSemanticGraphCanvas", () => {
  it("renders every step inside its rule cluster group node", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    for (const rule of graph.rules!) {
      const cluster = screen.getByTestId(`node-${rule.rule_id}`);
      expect(cluster).toHaveAttribute("data-node-type", RULE_CLUSTER_NODE_TYPE);
      expect(within(cluster).getByText(rule.name)).toBeInTheDocument();
      expect(within(cluster).getByText(rule.event_type)).toBeInTheDocument();
    }
    for (const node of graph.nodes!) {
      const rendered = screen.getByTestId(`node-${node.id}`);
      expect(rendered).toHaveAttribute("data-node-type", SEMANTIC_NODE_TYPE);
      expect(rendered).toHaveAttribute("data-parent", node.rule_id);
    }
  });

  it("draws one edge per transition and never merges two between the same pair", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    const edges = within(screen.getByTestId("edges")).getAllByRole("listitem");
    expect(edges).toHaveLength(graph.edges!.length);
    for (const dto of graph.edges!) {
      expect(screen.getByTestId(`edge-${dto.id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("edge-sweep-on-spec-approved::check-gate::case:0")).toHaveTextContent(
      "already open",
    );
    expect(screen.getByTestId("edge-sweep-on-spec-approved::check-gate::default")).toHaveTextContent(
      "Default",
    );
  });

  it("keeps every edge kind visually distinct and falls back neutrally", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    const drawn = flow.current!.edges;
    const kinds = new Set(drawn.map((e) => String(e.data!.edgeKind)));
    const dashes = new Set(drawn.map((e) => String(e.style?.strokeDasharray)));
    expect(dashes.size).toBe(kinds.size);

    cleanup();
    render(
      <PlaybookSemanticGraphCanvas
        graph={{ ...graph, edges: [{ ...graph.edges![0]!, kind: "teleport" as never }] }}
        onSelectNode={vi.fn()}
      />,
    );
    expect(flow.current!.edges[0]!.style?.strokeDasharray).toBe("4 4");
    expect(flow.current!.edges[0]!.ariaLabel).toContain("transition edge from");
  });

  it("gives every edge an aria label naming kind, source and target", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    for (const dto of graph.edges!) {
      const rendered = screen.getByTestId(`edge-${dto.id}`);
      expect(rendered.getAttribute("aria-label")).toContain(`from ${dto.source} to ${dto.target}`);
      expect(rendered.getAttribute("aria-label")).toContain(`outcome ${dto.outcome}`);
    }
  });

  it("legends only the edge kinds actually on the canvas", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    const legend = within(screen.getByRole("list", { name: "Edge kinds" }));
    expect(legend.getByText("success")).toBeInTheDocument();
    expect(legend.getByText("case")).toBeInTheDocument();
    expect(legend.queryByText("terminal")).not.toBeInTheDocument();
  });

  it("selects a step from its card, from its pane, and clears on the background", async () => {
    const onSelectNode = vi.fn();
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={onSelectNode} />);
    await userEvent.click(screen.getByRole("button", { name: /Inspect step Classify review risk/ }));
    expect(onSelectNode).toHaveBeenLastCalledWith("classify-risk");

    await userEvent.click(screen.getByRole("button", { name: "pane done" }));
    expect(onSelectNode).toHaveBeenLastCalledWith("done");

    // Cluster chrome is not a step: clicking it clears rather than selecting.
    await userEvent.click(screen.getByRole("button", { name: "pane sweep-on-spec-approved" }));
    expect(onSelectNode).toHaveBeenLastCalledWith(null);

    await userEvent.click(screen.getByRole("button", { name: "Blank canvas" }));
    expect(onSelectNode).toHaveBeenLastCalledWith(null);
  });

  it("marks the selected card pressed and clears the selection on Escape", () => {
    const onSelectNode = vi.fn();
    render(
      <PlaybookSemanticGraphCanvas
        graph={graph}
        selectedNodeId="escalate"
        onSelectNode={onSelectNode}
      />,
    );
    expect(
      screen.getByRole("button", { name: /Inspect step Escalate to a senior reviewer/ }),
    ).toHaveAttribute("aria-pressed", "true");

    const region = screen.getByRole("region", { name: "Playbook semantic graph" });
    fireEvent.keyDown(region, { key: "Escape" });
    expect(onSelectNode).toHaveBeenCalledWith(null);
  });

  it("preserves pan, zoom, fit and read-only ergonomics", () => {
    const { rerender } = render(
      <PlaybookSemanticGraphCanvas graph={graph} selectedNodeId={null} onSelectNode={vi.fn()} />,
    );
    expect(flow.current).toMatchObject({
      fitView: true,
      panOnScroll: true,
      nodesDraggable: false,
      elementsSelectable: false,
      deleteKeyCode: null,
      minZoom: 0.1,
      maxZoom: 2,
    });
    expect(flow.current!.viewport).toBeUndefined();
    const mounts = flow.mounts;
    // A selection change must not re-fit the camera: it re-renders the same
    // flow with `fitView` already consumed, and mounts nothing new.
    rerender(
      <PlaybookSemanticGraphCanvas graph={graph} selectedNodeId="done" onSelectNode={vi.fn()} />,
    );
    expect(screen.getByTestId("flow")).toBeInTheDocument();
    expect(flow.mounts).toBe(mounts + 1);
    expect(screen.getByRole("region", { name: "Playbook semantic graph" })).toHaveAttribute("tabindex", "0");
  });

  it("selects two edges that share endpoints independently", async () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    const caseId = "sweep-on-spec-approved::check-gate::case:0";
    const defaultId = "sweep-on-spec-approved::check-gate::default";
    const drawn = (id: string) => flow.current!.edges.find((e) => e.id === id)!;
    // Same source and same target: nothing but the id separates these two.
    expect(drawn(caseId).source).toBe(drawn(defaultId).source);
    expect(drawn(caseId).target).toBe(drawn(defaultId).target);
    expect(flow.current!.edges.filter((e) => e.selected)).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: `select edge ${caseId}` }));
    expect(drawn(caseId).selected).toBe(true);
    expect(drawn(defaultId).selected).toBe(false);
    // Selection has to be visible: the inline stroke would otherwise beat
    // xyflow's own `.selected` rule and nothing would change on screen.
    expect(drawn(caseId).style!.strokeWidth).toBeGreaterThan(
      drawn(defaultId).style!.strokeWidth as number,
    );
    expect(drawn(caseId).style!.strokeDasharray).toBe(
      EDGE_KIND_STYLES.decision_case!.strokeDasharray,
    );
    expect(drawn(caseId).zIndex!).toBeGreaterThan(drawn(defaultId).zIndex!);

    // Picking its twin moves the selection rather than adding to it.
    await userEvent.click(screen.getByRole("button", { name: `select edge ${defaultId}` }));
    expect(drawn(caseId).selected).toBe(false);
    expect(drawn(defaultId).selected).toBe(true);
    expect(flow.current!.edges.filter((e) => e.selected)).toHaveLength(1);

    // And clicking the selected one again lets it go.
    await userEvent.click(screen.getByRole("button", { name: `select edge ${defaultId}` }));
    expect(flow.current!.edges.filter((e) => e.selected)).toHaveLength(0);
  });

  it("makes every edge pointer- and keyboard-selectable without loosening the canvas", () => {
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    for (const edge of flow.current!.edges) {
      expect(edge.selectable).toBe(true);
      expect(edge.focusable).toBe(true);
      expect(edge.ariaRole).toBe("button");
      expect(edge.domAttributes).toMatchObject({ "aria-pressed": false });
    }
    expect(flow.current!.edgesFocusable).toBe(true);
    // xyflow gates an edge's own Enter/Space/Escape handling behind this flag,
    // so it cannot be set if edges are to be reachable without a mouse.
    expect(flow.current!.disableKeyboardA11y).toBeUndefined();
    // None of that reopens node selection, drag-select, dragging or delete.
    expect(flow.current).toMatchObject({
      elementsSelectable: false,
      nodesDraggable: false,
      panOnScroll: true,
      deleteKeyCode: null,
    });
  });

  it("holds one selection at a time across steps and edges", async () => {
    const onSelectNode = vi.fn();
    const edgeId = "sweep-on-spec-approved::check-gate::default";
    const selected = () => flow.current!.edges.find((e) => e.id === edgeId)!.selected;
    const { rerender } = render(
      <PlaybookSemanticGraphCanvas graph={graph} selectedNodeId="done" onSelectNode={onSelectNode} />,
    );

    await userEvent.click(screen.getByRole("button", { name: `select edge ${edgeId}` }));
    expect(selected()).toBe(true);
    // The canvas asks the view to drop the step it was inspecting.
    expect(onSelectNode).toHaveBeenLastCalledWith(null);
    rerender(
      <PlaybookSemanticGraphCanvas graph={graph} selectedNodeId={null} onSelectNode={onSelectNode} />,
    );
    expect(selected()).toBe(true);

    // The reverse holds for every path that picks a step, including the
    // diagnostics banner, which only ever changes the prop.
    rerender(
      <PlaybookSemanticGraphCanvas
        graph={graph}
        selectedNodeId="escalate"
        onSelectNode={onSelectNode}
      />,
    );
    expect(selected()).toBe(false);
  });

  it("clears a selected edge on Escape and on a background click", async () => {
    const edgeId = "sweep-on-spec-approved::check-gate::case:0";
    const selected = () => flow.current!.edges.find((e) => e.id === edgeId)!.selected;
    render(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: `select edge ${edgeId}` }));
    expect(selected()).toBe(true);
    fireEvent.keyDown(screen.getByRole("region", { name: "Playbook semantic graph" }), {
      key: "Escape",
    });
    expect(selected()).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: `select edge ${edgeId}` }));
    expect(selected()).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Blank canvas" }));
    expect(selected()).toBe(false);
  });

  it("forgets a selected edge that the event scope drops", async () => {
    const edgeId = "sweep-on-spec-approved::check-gate::case:0";
    const { rerender } = render(
      <PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: `select edge ${edgeId}` }));
    expect(flow.current!.edges.find((e) => e.id === edgeId)!.selected).toBe(true);

    const narrowed = { ...graph, edges: graph.edges!.filter((e) => e.id !== edgeId) };
    rerender(<PlaybookSemanticGraphCanvas graph={narrowed} onSelectNode={vi.fn()} />);
    expect(flow.current!.edges.some((e) => e.id === edgeId)).toBe(false);

    // Widening the scope again must not light the old edge back up.
    rerender(<PlaybookSemanticGraphCanvas graph={graph} onSelectNode={vi.fn()} />);
    expect(flow.current!.edges.filter((e) => e.selected)).toHaveLength(0);
  });

  it("reports an empty scope instead of rendering a blank canvas", () => {
    render(<PlaybookSemanticGraphCanvas graph={{ ...graph, nodes: [], edges: [] }} onSelectNode={vi.fn()} />);
    expect(screen.getByText("No rules match this event scope.")).toBeInTheDocument();
  });

  it("warns when a transition references a step outside the projection", () => {
    render(
      <PlaybookSemanticGraphCanvas
        graph={{ ...graph, edges: [{ ...graph.edges![0]!, target: "ghost" }] }}
        onSelectNode={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/1 transition/);
  });
});
