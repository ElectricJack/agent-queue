import type { CSSProperties } from "react";
import type { PlaybookGraphNode } from "../../api/client";

/** Fixed card geometry — the backend owns rank/order, the frontend owns pixels. */
export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 132;
export const COLUMN_GAP = 64;
export const ROW_GAP = 72;
export const PADDING = 32;

export const PLAYBOOK_NODE_TYPE = "playbookStep";

/** The six classifications produced by `graph_view.py::_classify_node`. */
export type PlaybookNodeType =
  | "entry"
  | "entry+decision"
  | "action"
  | "decision"
  | "checkpoint"
  | "terminal";

export const NODE_TYPE_LABELS: Record<string, string> = {
  entry: "entry",
  "entry+decision": "entry + decision",
  action: "action",
  decision: "decision",
  checkpoint: "human checkpoint",
  terminal: "terminal",
};

/** Dark-canvas tones. The backend palette is tuned for light Mermaid output;
 *  reusing its inline fills here would make card text unreadable, so the
 *  semantic distinction is preserved while the contrast is not. */
export const NODE_TYPE_TONES: Record<string, string> = {
  entry: "border-emerald-500 bg-emerald-950 text-emerald-100",
  "entry+decision": "border-emerald-500 bg-emerald-950 text-emerald-100",
  action: "border-sky-600 bg-sky-950 text-sky-100",
  decision: "border-amber-500 bg-amber-950 text-amber-100",
  checkpoint: "border-indigo-400 bg-indigo-950 text-indigo-100",
  terminal: "border-gray-500 bg-gray-900 text-gray-200",
};

export type PlaybookEdgeKind = "goto" | "condition" | "otherwise" | "timeout" | "success" | "failure";

/** Stroke pattern carries the edge kind on its own, so the four kinds stay
 *  distinguishable for anyone who cannot separate them by color. */
export const EDGE_KIND_STYLES: Record<string, CSSProperties> = {
  goto: { stroke: "#94a3b8", strokeWidth: 1.5, strokeDasharray: "0" },
  condition: { stroke: "#fbbf24", strokeWidth: 1.5, strokeDasharray: "7 4" },
  otherwise: { stroke: "#f472b6", strokeWidth: 1.5, strokeDasharray: "2 4" },
  timeout: { stroke: "#f87171", strokeWidth: 1.5, strokeDasharray: "10 3 2 3" },
  success: { stroke: "#34d399", strokeWidth: 2, strokeDasharray: "0" },
  failure: { stroke: "#fb7185", strokeWidth: 2, strokeDasharray: "7 4" },
};

/** Unknown server edge kinds remain visible and labelled rather than being
 * silently treated as a known transition or crashing the graph. */
export const NEUTRAL_EDGE_STYLE: CSSProperties = {
  stroke: "#cbd5e1",
  strokeWidth: 1.5,
  strokeDasharray: "4 4",
};

export const EDGE_KIND_LABELS: Record<string, string> = {
  goto: "goto",
  condition: "condition",
  otherwise: "otherwise",
  timeout: "timeout",
  success: "on success",
  failure: "on failure",
};

export interface PlaybookGraphNodeData extends Record<string, unknown> {
  node: PlaybookGraphNode;
  onSelect?: (nodeId: string) => void;
}
