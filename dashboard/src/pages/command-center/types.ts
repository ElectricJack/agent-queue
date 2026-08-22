import type { ProjectGraphResponse } from "@aq/ts-client";

/** Shared task-node dimensions — used by both the dagre layout pass
 *  (layout.ts) and the avatar-docking projection (AgentAvatarLayer.tsx).
 *  Keep these in sync with the actual TaskNode.tsx rendered size. */
export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 88;

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
