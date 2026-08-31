import { describe, expect, it } from "vitest";
import { layoutPlaybookGraph } from "../layout";
import { COLUMN_GAP, NODE_HEIGHT, NODE_WIDTH, PADDING, ROW_GAP } from "../types";
import { edge, graph, layout, node } from "./fixtures";

describe("layoutPlaybookGraph", () => {
  it("scales the backend grid positions into pixel positions with fixed node dimensions", () => {
    const result = layoutPlaybookGraph(graph, layout);
    const positions = Object.fromEntries(result.nodes.map((n) => [n.id, n.position]));
    expect(positions.triage).toEqual({ x: PADDING, y: PADDING });
    expect(positions.review).toEqual({ x: PADDING, y: PADDING + NODE_HEIGHT + ROW_GAP });
    expect(positions.approve).toEqual({
      x: PADDING + NODE_WIDTH + COLUMN_GAP,
      y: PADDING + NODE_HEIGHT + ROW_GAP,
    });
    for (const flowNode of result.nodes) {
      expect(flowNode.width).toBe(NODE_WIDTH);
      expect(flowNode.height).toBe(NODE_HEIGHT);
      expect(flowNode.draggable).toBe(false);
      expect(flowNode.connectable).toBe(false);
    }
  });

  it("keeps the backend node order and never derives flow from prompt text", () => {
    const result = layoutPlaybookGraph(graph, layout);
    expect(result.nodes.map((n) => n.id)).toEqual([
      "triage", "review", "approve", "escalate", "done",
    ]);
  });

  it("falls back to the node's own position, then to a stable index row", () => {
    const result = layoutPlaybookGraph(
      { nodes: [node("a", { position: { x: 2, y: 1 } }), node("b")], edges: [] },
      { direction: "TD", grid_positions: {} },
    );
    expect(result.nodes[0]!.position).toEqual({
      x: PADDING + 2 * (NODE_WIDTH + COLUMN_GAP),
      y: PADDING + NODE_HEIGHT + ROW_GAP,
    });
    expect(result.nodes[1]!.position).toEqual({ x: PADDING, y: PADDING + NODE_HEIGHT + ROW_GAP });
  });

  it("gives every edge a unique id, a directed marker and a kind-specific stroke", () => {
    const result = layoutPlaybookGraph(graph, layout);
    expect(new Set(result.edges.map((e) => e.id)).size).toBe(result.edges.length);
    const byKind = Object.fromEntries(result.edges.map((e) => [e.data!.edgeType as string, e]));
    expect(byKind.goto!.markerEnd).toBeTruthy();
    const dashes = new Set(
      ["goto", "condition", "otherwise", "timeout"].map((k) => String(byKind[k]!.style?.strokeDasharray)),
    );
    expect(dashes.size).toBe(4);
  });

  it("labels conditional, otherwise and timeout edges and leaves plain goto unlabelled", () => {
    const result = layoutPlaybookGraph(graph, layout);
    const labels = result.edges.map((e) => [e.source, e.target, e.label]);
    expect(labels).toEqual([
      ["triage", "review", "needs_review"],
      ["triage", "approve", "otherwise"],
      ["review", "approve", undefined],
      ["review", "escalate", "timeout"],
      ["escalate", "done", "give_up"],
      ["approve", "done", undefined],
    ]);
  });

  it("drops edges whose endpoint is absent and reports the incomplete data", () => {
    const result = layoutPlaybookGraph(
      { nodes: [node("a")], edges: [edge("a", "ghost"), edge("ghost", "a"), edge("a", "a")] },
      { direction: "TD", grid_positions: {} },
    );
    expect(result.edges.map((e) => [e.source, e.target])).toEqual([["a", "a"]]);
    expect(result.droppedEdgeCount).toBe(2);
  });

  it("tolerates a response with no graph payload at all", () => {
    const result = layoutPlaybookGraph(undefined, undefined);
    expect(result).toMatchObject({ nodes: [], edges: [], droppedEdgeCount: 0 });
  });
});
