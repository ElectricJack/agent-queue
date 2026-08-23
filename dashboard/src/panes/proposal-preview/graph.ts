import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { ProposalEdge, ProposalTask } from "./hooks";

// Local, proposal-scoped layout adapter — deliberately not a shared import
// from command-center/layout.ts. Proposal graphs use tempId instead of a
// real task id and have no status/gates/agents to fold in; tighter spacing
// suits the smaller (~10-30 node) proposal graphs and narrower pane.
export const NODE_WIDTH = 180;
export const NODE_HEIGHT = 64;

export interface ProposalNodeData extends Record<string, unknown> {
  id: string;
  title: string;
  depCount: number;
  ghost: boolean;
}

export function layoutProposalGraph(
  tasks: ProposalTask[],
  edges: ProposalEdge[],
): { nodes: Node<ProposalNodeData>[]; edges: Edge[] } {
  const dg = new dagre.graphlib.Graph();
  dg.setDefaultEdgeLabel(() => ({}));
  dg.setGraph({ rankdir: "TB", nodesep: 32, ranksep: 64 });

  const knownIds = new Set(tasks.map((t) => t.tempId));
  const ghostIds = new Set<string>();
  for (const e of edges) {
    if (!knownIds.has(e.from)) ghostIds.add(e.from);
    if (!knownIds.has(e.to)) ghostIds.add(e.to);
  }

  const depCount = new Map<string, number>();
  for (const e of edges) {
    depCount.set(e.from, (depCount.get(e.from) ?? 0) + 1);
  }

  for (const t of tasks) {
    dg.setNode(t.tempId, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const gid of ghostIds) {
    dg.setNode(gid, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of edges) {
    dg.setEdge(e.to, e.from);
  }

  dagre.layout(dg);

  const nodes: Node<ProposalNodeData>[] = [
    ...tasks.map((t) => {
      const pos = dg.node(t.tempId);
      return {
        id: t.tempId,
        type: "proposalTask",
        position: { x: (pos?.x ?? 0) - NODE_WIDTH / 2, y: (pos?.y ?? 0) - NODE_HEIGHT / 2 },
        data: {
          id: t.tempId,
          title: t.title,
          depCount: depCount.get(t.tempId) ?? 0,
          ghost: false,
        },
      };
    }),
    ...[...ghostIds].map((gid) => {
      const pos = dg.node(gid);
      return {
        id: gid,
        type: "proposalTask",
        position: { x: (pos?.x ?? 0) - NODE_WIDTH / 2, y: (pos?.y ?? 0) - NODE_HEIGHT / 2 },
        data: { id: gid, title: gid.slice(0, 8), depCount: 0, ghost: true },
      };
    }),
  ];

  const rfEdges: Edge[] = edges.map((e) => ({
    id: `${e.to}->${e.from}:${e.dep_type}`,
    source: e.to,
    target: e.from,
    type: e.dep_type === "blocks" ? "smoothstep" : "default",
  }));

  return { nodes, edges: rfEdges };
}
