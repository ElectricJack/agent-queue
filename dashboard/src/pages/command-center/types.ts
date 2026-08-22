import type { ProjectGraphResponse } from "@aq/ts-client";

export type GraphTaskNode = NonNullable<ProjectGraphResponse["tasks"]>[number];
export type GraphEdge = NonNullable<ProjectGraphResponse["edges"]>[number];
export type GraphGate = NonNullable<ProjectGraphResponse["gates"]>[number];
export type GraphAgent = NonNullable<ProjectGraphResponse["agents"]>[number];

export interface MergedGraph {
  tasks: GraphTaskNode[];
  edges: GraphEdge[];
  gates: GraphGate[];
  agents: GraphAgent[];
  /** projectId a task belongs to — filled by the merger, keyed by task.id */
  taskProject: Record<string, string>;
}
