import type { PlaybookSummary } from "../../api/hooks";
import type { ProjectGraphResponse } from "@aq/ts-client";

/** Fixed card dimensions keep wrapped rows and connection handles aligned. */
export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 156;

export type GraphTaskNode = NonNullable<ProjectGraphResponse["tasks"]>[number];
export type GraphEdge = NonNullable<ProjectGraphResponse["edges"]>[number];
export type GraphGate = NonNullable<ProjectGraphResponse["gates"]>[number];
export type GraphAgent = NonNullable<ProjectGraphResponse["agents"]>[number];

export interface MergedGraph {
  tasks: GraphTaskNode[];
  edges: GraphEdge[];
  gates: GraphGate[];
  agents: GraphAgent[];
  taskProject: Record<string, string>;
}

export interface GraphViewProps {
  graph: MergedGraph;
  playbooks?: PlaybookSummary[];
  selectedPlaybookId?: string | null;
  onPlaybookClick?: (playbookId: string) => void;
  onTaskClick: (taskId: string) => void;
  selectedTaskId?: string | null;
  onBackgroundClick?: () => void;
  matchingTaskIds?: ReadonlySet<string>;
  filtering?: boolean;
}

export interface TaskHierarchy {
  parentId: string | null;
  parentTitle: string | null;
  depth: number;
  childCount: number;
  visibleChildCount: number;
  descendantCount: number;
  completedCount: number;
  runningCount: number;
  blockedCount: number;
  expanded: boolean;
  autoExpanded: boolean;
  contextOnly: boolean;
}

export interface TaskNodeData extends Record<string, unknown> {
  task: GraphTaskNode;
  gates: GraphGate[];
  projectId: string;
  hierarchy: TaskHierarchy;
  onOpenTask?: (taskId: string) => void;
  onToggleChildren?: (taskId: string) => void;
}

export interface PlaybookNodeData extends Record<string, unknown> {
  playbook: PlaybookSummary;
  onOpenPlaybook?: (playbookId: string) => void;
}
