import { describe, expect, it } from "vitest";
import { clusterPixelBounds, layoutSemanticGraph, toPixels } from "../layout";
import {
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  NODE_HEIGHT,
  NODE_WIDTH,
  RULE_CLUSTER_NODE_TYPE,
  SEMANTIC_NODE_TYPE,
} from "../types";
import { graph, doneNode } from "./fixtures";

describe("layoutSemanticGraph", () => {
  it("maps every DTO edge to exactly one flow edge and preserves its id verbatim", () => {
    const result = layoutSemanticGraph(graph);
    expect(result.edges).toHaveLength(graph.edges!.length);
    expect(result.edges.map((e) => e.id)).toEqual(graph.edges!.map((e) => e.id));
    // The ids are content-derived, never positional: inserting an unrelated
    // edge upstream must not renumber anything.
    const shifted = layoutSemanticGraph({
      ...graph,
      edges: [graph.edges![graph.edges!.length - 1]!, ...graph.edges!.slice(0, -1)],
    });
    expect(new Set(shifted.edges.map((e) => e.id))).toEqual(new Set(result.edges.map((e) => e.id)));
    expect(result.droppedEdgeCount).toBe(0);
  });

  it("places every step node inside its rule cluster", () => {
    const result = layoutSemanticGraph(graph);
    const clusters = result.nodes.filter((n) => n.type === RULE_CLUSTER_NODE_TYPE);
    expect(clusters.map((c) => c.id)).toEqual(graph.rules!.map((r) => r.rule_id));
    // A parent must precede its children in the node array.
    const firstStep = result.nodes.findIndex((n) => n.type === SEMANTIC_NODE_TYPE);
    expect(firstStep).toBe(clusters.length);

    const boxes = Object.fromEntries(
      clusters.map((c) => [c.id, { ...c.position, width: c.width!, height: c.height! }]),
    );
    for (const node of result.nodes.filter((n) => n.type === SEMANTIC_NODE_TYPE)) {
      const dto = graph.nodes!.find((n) => n.id === node.id)!;
      expect(node.parentId).toBe(dto.rule_id);
      const box = boxes[dto.rule_id]!;
      const absolute = { x: box.x + node.position.x, y: box.y + node.position.y };
      expect(absolute).toEqual(toPixels(dto.position!));
      expect(absolute.x).toBeGreaterThanOrEqual(box.x);
      expect(absolute.y).toBeGreaterThanOrEqual(box.y);
      expect(absolute.x + NODE_WIDTH).toBeLessThanOrEqual(box.x + box.width);
      expect(absolute.y + NODE_HEIGHT).toBeLessThanOrEqual(box.y + box.height);
    }
  });

  it("keeps two edges between the same pair independently addressable", () => {
    const result = layoutSemanticGraph(graph);
    const pair = result.edges.filter((e) => e.source === "check-gate" && e.target === "for-each-task");
    expect(pair).toHaveLength(2);
    expect(new Set(pair.map((e) => e.id)).size).toBe(2);
    expect(pair.map((e) => e.data!.edgeKind)).toEqual(["decision_case", "decision_default"]);
    expect(pair.map((e) => e.label)).toEqual(["already open", "Default"]);
    expect(pair.map((e) => e.selectable)).toEqual([true, true]);
  });

  it("makes every edge selectable and focusable without making it editable", () => {
    const result = layoutSemanticGraph(graph);
    expect(result.edges).not.toHaveLength(0);
    for (const edge of result.edges) {
      // Explicit per-edge flags, not the flow-level defaults: the canvas keeps
      // `elementsSelectable` off so the cards stay read-only, and xyflow reads
      // `edge.selectable ?? elementsSelectable`.
      expect(edge.selectable).toBe(true);
      expect(edge.focusable).toBe(true);
      // Focusable + a button role is what turns xyflow's Enter/Space/Escape
      // handling into something a screen reader announces as operable.
      expect(edge.ariaRole).toBe("button");
      expect(edge.deletable).toBe(false);
      expect(edge.reconnectable).toBe(false);
    }
  });

  it("anchors each edge on the outcome port of its source card", () => {
    const result = layoutSemanticGraph(graph);
    for (const edge of result.edges) {
      const dto = graph.edges!.find((e) => e.id === edge.id)!;
      expect(edge.sourceHandle).toBe(`out-${dto.source_port}`);
      expect(edge.targetHandle).toBe("in");
    }
  });

  it("gives every edge a kind-specific stroke, a marker and an aria label", () => {
    const result = layoutSemanticGraph(graph);
    for (const edge of result.edges) {
      const dto = graph.edges!.find((e) => e.id === edge.id)!;
      expect(edge.style).toEqual(EDGE_KIND_STYLES[dto.kind]);
      expect(edge.markerEnd).toBeTruthy();
      expect(edge.ariaLabel).toBe(
        `${EDGE_KIND_LABELS[dto.kind]} edge from ${dto.source} to ${dto.target} on outcome ${dto.outcome}`,
      );
    }
  });

  it("keeps an unknown edge kind visible, labelled and neutrally styled", () => {
    const result = layoutSemanticGraph({
      ...graph,
      edges: [{ ...graph.edges![0]!, kind: "teleport" as never }],
    });
    expect(result.edges[0]!.style).toEqual(NEUTRAL_EDGE_STYLE);
    expect(result.edges[0]!.ariaLabel).toContain("transition edge from");
    expect(result.edges[0]!.label).toBe(graph.edges![0]!.label);
  });

  it("drops edges whose endpoint is absent and reports the incomplete data", () => {
    const result = layoutSemanticGraph({
      ...graph,
      edges: [
        { ...graph.edges![0]!, id: "x", target: "ghost" },
        { ...graph.edges![0]!, id: "y", source: "ghost" },
        graph.edges![0]!,
      ],
    });
    expect(result.edges.map((e) => e.id)).toEqual([graph.edges![0]!.id]);
    expect(result.droppedEdgeCount).toBe(2);
  });

  it("falls back to the node's own position when the layout omits a grid entry", () => {
    const result = layoutSemanticGraph({
      nodes: [{ ...doneNode, position: { x: 2, y: 1 } }],
      edges: [],
      rules: [],
      layout: { direction: "TD", grid_positions: {}, cluster_bounds: {} },
    });
    expect(result.nodes[0]!.position).toEqual(toPixels({ x: 2, y: 1 }));
    expect(result.nodes[0]!.parentId).toBeUndefined();
  });

  it("counts rule and step diagnostics onto the owning cluster", () => {
    const result = layoutSemanticGraph(graph);
    const byId = Object.fromEntries(
      result.nodes.filter((n) => n.type === RULE_CLUSTER_NODE_TYPE).map((n) => [n.id, n.data]),
    );
    // Every graph-level diagnostic blames a step, so each lands on the cluster
    // that owns that step: `ensure-review-task` on rule 1, `list-downstream`
    // and `open-gate` on rule 2.
    expect(byId["review-on-task-completed"]!.diagnosticCount).toBe(1);
    expect(byId["sweep-on-spec-approved"]!.diagnosticCount).toBe(2);
  });

  it("tolerates a response with no graph payload at all", () => {
    expect(layoutSemanticGraph(undefined)).toMatchObject({
      nodes: [],
      edges: [],
      droppedEdgeCount: 0,
    });
  });

  it("sizes a cluster box around the grid cells its steps occupy", () => {
    const box = clusterPixelBounds({ x: 0, y: 0, width: 1, height: 1 });
    expect(box.width).toBeGreaterThanOrEqual(NODE_WIDTH);
    expect(box.height).toBeGreaterThanOrEqual(NODE_HEIGHT);
  });
});
