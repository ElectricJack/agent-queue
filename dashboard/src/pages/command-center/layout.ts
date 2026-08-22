import type { CSSProperties } from "react";
import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import { NODE_WIDTH, NODE_HEIGHT, type GraphTaskNode, type MergedGraph } from "./types";

/**
 * Rank ordering for statuses — lower = higher up in the canvas.
 * Completed & terminal work floats to the top; active work sits in the
 * middle; pending/new work drifts toward the bottom. Dagre's DAG rank
 * dominates when edges exist; this only breaks ties for isolated tasks
 * and biases the within-rank order of same-depth siblings.
 */
const STATUS_ORDER: Record<string, number> = {
  COMPLETED: 0,
  FAILED: 0,
  CANCELED: 0,
  BLOCKED: 1,
  IN_PROGRESS: 2,
  AWAITING_APPROVAL: 2,
  AWAITING_PLAN_APPROVAL: 2,
  WAITING_INPUT: 2,
  READY: 3,
  PENDING: 4,
};

function statusRank(t: GraphTaskNode): number {
  return STATUS_ORDER[(t.status ?? "PENDING").toUpperCase()] ?? 5;
}

function priorityRank(t: GraphTaskNode): number {
  // Higher priority (bigger number) sorts first — dagre uses insertion
  // order to break ties in the barycenter heuristic, so higher-priority
  // nodes end up on the left within their rank.
  return -(t.priority ?? 0);
}

export function layoutGraph(g: MergedGraph): { nodes: Node[]; edges: Edge[] } {
  const dg = new dagre.graphlib.Graph();
  dg.setDefaultEdgeLabel(() => ({}));
  // Top-to-bottom flow: completed dependencies rank at the top, dependents
  // rank below them, and new work added to the graph naturally lands at
  // the bottom because it has more inbound "depends on" chains above it.
  dg.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 100 });

  // Feed nodes in an order that biases dagre's within-rank sort:
  // completed first, then in-progress, then pending; ties broken by
  // priority DESC, then task id for determinism across renders.
  const orderedTasks = [...g.tasks].sort((a, b) => {
    const s = statusRank(a) - statusRank(b);
    if (s !== 0) return s;
    const p = priorityRank(a) - priorityRank(b);
    if (p !== 0) return p;
    return a.id.localeCompare(b.id);
  });

  for (const t of orderedTasks) {
    dg.setNode(t.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }

  // Only render edges whose BOTH endpoints are loaded (spec §9.2 cross-project rule).
  const loaded = new Set(g.tasks.map((t) => t.id));
  const edges = g.edges.filter((e) => loaded.has(e.from) && loaded.has(e.to));

  // Backend edges are stored as {from: dependent, to: dependency}. We
  // want the visual flow to go dependency (top) → dependent (bottom) so
  // completed prerequisites float up and new work hooks in below them.
  // Feed dagre — and later React Flow — the reversed direction so arrows
  // point downward from prerequisite to dependent.
  for (const e of edges) {
    dg.setEdge(e.to, e.from);
  }

  dagre.layout(dg);

  const nodes: Node[] = g.tasks.map((t) => {
    const pos = dg.node(t.id);
    const nodeGates = g.gates.filter((gate) => (gate.task_ids ?? []).includes(t.id));
    return {
      id: t.id,
      type: "task",
      position: { x: (pos?.x ?? 0) - NODE_WIDTH / 2, y: (pos?.y ?? 0) - NODE_HEIGHT / 2 },
      data: { task: t, gates: nodeGates, projectId: g.taskProject[t.id] },
    };
  });

  const rfEdges: Edge[] = edges.map((e) => ({
    // React Flow edge: source at top (dependency), target at bottom (dependent).
    // The arrow points from the completed prerequisite down to whatever
    // depends on it — matches the "work flows down" reading of the graph.
    id: `${e.to}->${e.from}:${e.dep_type}`,
    source: e.to,
    target: e.from,
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
