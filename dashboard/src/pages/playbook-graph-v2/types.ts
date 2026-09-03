import type { CSSProperties } from "react";
import type { GraphNodeDTO, RuleClusterDTO } from "../../api/client";

/** Fixed card geometry. The backend owns rank and order (`GraphLayoutDTO`);
 *  the frontend owns pixels and nothing else. */
export const NODE_WIDTH = 264;
export const NODE_HEIGHT = 156;
export const COLUMN_GAP = 72;
export const ROW_GAP = 88;
export const PADDING = 40;

/** Slack around a rule cluster's step bounding box, plus room for its header. */
export const CLUSTER_PADDING = 28;
export const CLUSTER_HEADER = 44;

export const SEMANTIC_NODE_TYPE = "semanticStep";
export const RULE_CLUSTER_NODE_TYPE = "ruleCluster";

/** The seven step kinds of the V2 step union (`StepKind` in
 *  `src/api/models/playbook_v2.py`). */
export type SemanticStepKind =
  | "command"
  | "llm"
  | "agent_task"
  | "decision"
  | "wait"
  | "foreach"
  | "terminal";

export const STEP_KIND_LABELS: Record<string, string> = {
  command: "command",
  llm: "AI",
  agent_task: "agent task",
  decision: "decision",
  wait: "wait",
  foreach: "for each",
  terminal: "terminal",
};

/** Dark-canvas tones, one per step kind. Colour is the fast read; the kind
 *  label under the title is the accessible one, so a card never depends on
 *  hue alone to say what it is. */
export const STEP_KIND_TONES: Record<string, string> = {
  command: "border-sky-600 bg-sky-950 text-sky-100",
  llm: "border-violet-500 bg-violet-950 text-violet-100",
  agent_task: "border-teal-500 bg-teal-950 text-teal-100",
  decision: "border-amber-500 bg-amber-950 text-amber-100",
  wait: "border-indigo-400 bg-indigo-950 text-indigo-100",
  foreach: "border-cyan-500 bg-cyan-950 text-cyan-100",
  terminal: "border-gray-500 bg-gray-900 text-gray-200",
};

/** The twelve edge kinds of `EdgeKind`. */
export type SemanticEdgeKind =
  | "success"
  | "failure"
  | "decision_case"
  | "decision_default"
  | "loop_body"
  | "loop_exit"
  | "loop_back"
  | "timeout"
  | "wait_matched"
  | "runtime_error"
  | "cancelled"
  | "terminal";

/** Stroke pattern carries the edge kind on its own, so every kind stays
 *  distinguishable for anyone who cannot separate them by colour. Every dash
 *  pattern in this map is unique — `types.test.ts` pins that. */
export const EDGE_KIND_STYLES: Record<string, CSSProperties> = {
  success: { stroke: "#34d399", strokeWidth: 2, strokeDasharray: "0" },
  failure: { stroke: "#fb7185", strokeWidth: 2, strokeDasharray: "7 4" },
  decision_case: { stroke: "#fbbf24", strokeWidth: 1.5, strokeDasharray: "2 4" },
  decision_default: { stroke: "#f472b6", strokeWidth: 1.5, strokeDasharray: "1 5" },
  loop_body: { stroke: "#22d3ee", strokeWidth: 1.5, strokeDasharray: "10 3 2 3" },
  loop_exit: { stroke: "#38bdf8", strokeWidth: 1.5, strokeDasharray: "12 5" },
  loop_back: { stroke: "#818cf8", strokeWidth: 1.5, strokeDasharray: "4 2 1 2" },
  timeout: { stroke: "#f87171", strokeWidth: 1.5, strokeDasharray: "9 3 3 3" },
  wait_matched: { stroke: "#a5b4fc", strokeWidth: 1.5, strokeDasharray: "6 3" },
  runtime_error: { stroke: "#ef4444", strokeWidth: 1.5, strokeDasharray: "3 3" },
  cancelled: { stroke: "#94a3b8", strokeWidth: 1.5, strokeDasharray: "2 2 8 2" },
  terminal: { stroke: "#cbd5e1", strokeWidth: 2, strokeDasharray: "14 4 2 4" },
};

/** An edge kind this build does not know stays visible and labelled rather
 *  than being silently rendered as a known transition. */
export const NEUTRAL_EDGE_STYLE: CSSProperties = {
  stroke: "#cbd5e1",
  strokeWidth: 1.5,
  strokeDasharray: "4 4",
};

/** Selection emphasis for one transition edge.
 *
 *  The dash pattern is what carries the edge kind, so a selected edge keeps it
 *  and gains weight plus a halo in its own colour instead. React Flow's
 *  `.react-flow__edge.selected` rule cannot do this for us: it recolours the
 *  path through a CSS variable, and every edge here already carries an inline
 *  stroke that wins over it. */
export function selectedEdgeStyle(base: CSSProperties): CSSProperties {
  const width = typeof base.strokeWidth === "number" ? base.strokeWidth : 1.5;
  return {
    ...base,
    strokeWidth: width * 2,
    filter: `drop-shadow(0 0 5px ${String(base.stroke ?? "#e2e8f0")})`,
  };
}

export const EDGE_KIND_LABELS: Record<string, string> = {
  success: "success",
  failure: "failure",
  decision_case: "case",
  decision_default: "default",
  loop_body: "loop body",
  loop_exit: "loop exit",
  loop_back: "loop back",
  timeout: "timeout",
  wait_matched: "wait matched",
  runtime_error: "runtime error",
  cancelled: "cancelled",
  terminal: "terminal",
};

export interface SemanticGraphNodeData extends Record<string, unknown> {
  node: GraphNodeDTO;
  onSelect?: (nodeId: string) => void;
}

export interface RuleClusterNodeData extends Record<string, unknown> {
  rule: RuleClusterDTO;
  /** Diagnostics attributed to the rule itself plus each of its steps. */
  diagnosticCount: number;
}
