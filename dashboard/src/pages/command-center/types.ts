import type { PlaybookSummary } from "../../api/hooks";
import type { ProjectGraphResponse } from "@aq/ts-client";

/** Fixed card dimensions keep wrapped rows and connection handles aligned. */
export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 156;

export type GraphTaskNode = NonNullable<ProjectGraphResponse["tasks"]>[number];
export type GraphEdge = NonNullable<ProjectGraphResponse["edges"]>[number];
export type GraphGate = NonNullable<ProjectGraphResponse["gates"]>[number];
export type GraphAgent = NonNullable<ProjectGraphResponse["agents"]>[number];
/** A worker badge's agent; the tiled layout also reports collapsed docking. */
export type GraphWorker = GraphAgent & { in_collapsed?: boolean };

export interface MergedGraph {
  tasks: GraphTaskNode[];
  edges: GraphEdge[];
  gates: GraphGate[];
  agents: GraphAgent[];
  taskProject: Record<string, string>;
}

/** What a click needs in order to route: the card's id and its playbook run. */
export type SelectableTask = { id: string; playbook_run_id?: string | null };

/** The props a task-graph view shares with the shell that hosts it. */
export interface GraphViewProps {
  playbooks?: PlaybookSummary[];
  selectedPlaybookId?: string | null;
  onPlaybookClick?: (playbookId: string) => void;
  onTaskClick: (taskId: string, task?: SelectableTask) => void;
  selectedTaskId?: string | null;
  onBackgroundClick?: () => void;
}

export interface TaskHierarchy {
  parentId: string | null;
  parentTitle: string | null;
  depth: number;
  childCount: number;
  descendantCount: number;
  completedCount: number;
  runningCount: number;
  blockedCount: number;
  contextOnly: boolean;
}

/** What a card's reason line names (spec §3.4), resolved from the loaded tiles. */
export interface TaskRelations {
  /** Unsettled tasks this one waits on, and the first one's title when loaded. */
  blockerCount: number;
  blockerTitle: string | null;
  /** The first unsettled task waiting on this one. */
  dependentTitle: string | null;
  /** Loaded READY tasks the frontier takes first, or `null` on a priority tie
   *  (the frontier then orders by age, which the layout does not carry). */
  readyAhead: number | null;
}

export interface TaskNodeData extends Record<string, unknown> {
  task: GraphTaskNode;
  gates: GraphGate[];
  projectId: string;
  hierarchy: TaskHierarchy;
  onOpenTask?: (taskId: string, task?: SelectableTask) => void;
  /** Enter this container: the canvas re-scopes to it (there is no inline
   *  expansion). Absent on a leaf, and on the container already entered. */
  onFocus?: (taskId: string) => void;
  /** Presentation-only scale selected in the tiled graph. */
  layoutScale?: number;
  /** This task's own durable subtask checklist counts, when it has any. */
  subtasks?: { total: number; settled: number };
  /** Set when this node is a phase container (graph-visibility A1). */
  phase?: { order: number; label: string } | null;
  /** An epic's delivery, kept apart from its implementation progress. */
  delivery?: import("@aq/ts-client").EpicDeliveryStatus | null;
  /** Reviews this task's gates are tied to, blocking first (`aq task explain`'s
   *  `blocked_gate`); the card links the first to its review page. */
  reviewWaits?: import("@aq/ts-client").ReviewWait[];
  /** Neighbours the reason line names; absent where no graph is loaded. */
  relations?: TaskRelations;
  /** A boundary marker: a task outside the loaded tiles, `foreign` when it
   *  lives in another project. Its stored status is not known here. */
  stub?: { foreign: boolean };
}

export interface ContainerNodeData extends Record<string, unknown> {
  node: import("@aq/ts-client").LayoutNode;
  projectId: string;
  onFocus?: (taskId: string) => void;
  onOpenTask?: (taskId: string, task?: SelectableTask) => void;
  layoutScale?: number;
  /** Only on the entered container: where its "Up to" button leads, the
   *  parent epic or the project itself (`id: null`). */
  upTarget?: { id: string | null; title: string };
  onUp?: (id: string | null) => void;
}

export interface StubNodeData extends Record<string, unknown> {
  id: string;
  title: string;
}

export interface PlaybookNodeData extends Record<string, unknown> {
  playbook: PlaybookSummary;
  onOpenPlaybook?: (playbookId: string) => void;
}
