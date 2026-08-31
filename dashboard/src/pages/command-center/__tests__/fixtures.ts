import type { GraphEdge, GraphTaskNode, MergedGraph } from "../types";

export function task(id: string, patch: Partial<GraphTaskNode> = {}): GraphTaskNode {
  return { id, title: `Task ${id}`, status: "READY", priority: 100, ...patch };
}

export function edge(from: string, to: string, dep_type = "parent-child"): GraphEdge {
  return { from, to, dep_type };
}

export function graph(tasks: GraphTaskNode[], edges: GraphEdge[] = []): MergedGraph {
  return {
    tasks, edges, gates: [], agents: [],
    taskProject: Object.fromEntries(tasks.map((t) => [t.id, "project-one"])),
  };
}
