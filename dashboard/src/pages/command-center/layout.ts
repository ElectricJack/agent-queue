import type { CSSProperties } from "react";
import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { MergedGraph } from "./types";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 88;

export function layoutGraph(g: MergedGraph): { nodes: Node[]; edges: Edge[] } {
  const dg = new dagre.graphlib.Graph();
  dg.setDefaultEdgeLabel(() => ({}));
  dg.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 80 });

  for (const t of g.tasks) {
    dg.setNode(t.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  // Only render edges whose BOTH endpoints are loaded (spec §9.2 cross-project rule).
  const loaded = new Set(g.tasks.map((t) => t.id));
  const edges = g.edges.filter((e) => loaded.has(e.from) && loaded.has(e.to));
  for (const e of edges) {
    dg.setEdge(e.from, e.to);
  }

  dagre.layout(dg);

  const nodes: Node[] = g.tasks.map((t) => {
    const pos = dg.node(t.id);
    // Attach gates that reference this task so the node renderer can badge them.
    const nodeGates = g.gates.filter((gate) => (gate.task_ids ?? []).includes(t.id));
    return {
      id: t.id,
      type: "task",
      position: { x: (pos?.x ?? 0) - NODE_WIDTH / 2, y: (pos?.y ?? 0) - NODE_HEIGHT / 2 },
      data: { task: t, gates: nodeGates, projectId: g.taskProject[t.id] },
    };
  });

  const rfEdges: Edge[] = edges.map((e) => ({
    id: `${e.from}->${e.to}:${e.dep_type}`,
    source: e.from,
    target: e.to,
    type: e.dep_type === "blocks" ? "smoothstep" : "default",
    animated: e.dep_type === "waits_for",
    style: edgeStyleForType(e.dep_type),
  }));

  return { nodes, edges: rfEdges };
}

function edgeStyleForType(depType: string): CSSProperties {
  switch (depType) {
    case "blocks":
      return { stroke: "#818cf8", strokeWidth: 2 };
    case "parent_child":
      return { stroke: "#a3a3a3", strokeDasharray: "4 4" };
    case "waits_for":
      return { stroke: "#fbbf24", strokeWidth: 2 };
    case "conditional_blocks":
      return { stroke: "#fb923c", strokeDasharray: "6 3" };
    case "discovered_from":
      return { stroke: "#6b7280", strokeDasharray: "2 4" };
    default:
      return { stroke: "#4b5563" };
  }
}
