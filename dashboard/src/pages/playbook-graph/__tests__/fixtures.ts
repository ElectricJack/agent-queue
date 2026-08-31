import type {
  PlaybookGraphEdge,
  PlaybookGraphNode,
  PlaybookGraphNodesEdges,
  PlaybookGraphLayout,
} from "../../../api/client";

const COLORS = { fill: "#E3F2FD", stroke: "#1565C0", text: "#000000" };

export function node(
  id: string,
  overrides: Partial<PlaybookGraphNode> = {},
): PlaybookGraphNode {
  return {
    id,
    type: "action",
    symbol: "●",
    label: id,
    colors: COLORS,
    entry: false,
    terminal: false,
    wait_for_human: false,
    out_degree: 1,
    details: { prompt: `Do ${id}` },
    ...overrides,
  };
}

export function edge(
  source: string,
  target: string,
  overrides: Partial<PlaybookGraphEdge> = {},
): PlaybookGraphEdge {
  return { source, target, label: "", edge_type: "goto", ...overrides };
}

/** A playbook exercising every node classification and every edge kind. */
export const graph: PlaybookGraphNodesEdges = {
  nodes: [
    node("triage", {
      type: "entry+decision",
      symbol: "▶◆",
      entry: true,
      out_degree: 2,
      prompt_preview: "Classify the incoming task",
      details: { prompt: "Classify the incoming task", entry: true },
    }),
    node("review", { prompt_preview: "Review the diff", timeout_seconds: 600, on_timeout: "escalate" }),
    node("approve", { type: "checkpoint", symbol: "⏸", wait_for_human: true, prompt_preview: "Approve the change" }),
    node("escalate", { type: "decision", symbol: "◆", out_degree: 2, prompt_preview: "Decide how to escalate" }),
    node("done", { type: "terminal", symbol: "■", terminal: true, out_degree: 0, details: { terminal: true } }),
  ],
  edges: [
    edge("triage", "review", { label: "needs_review", edge_type: "condition" }),
    edge("triage", "approve", { label: "otherwise", edge_type: "otherwise" }),
    edge("review", "approve"),
    edge("review", "escalate", { label: "timeout", edge_type: "timeout" }),
    edge("escalate", "done", { label: "give_up", edge_type: "condition" }),
    edge("approve", "done"),
  ],
};

export const layout: PlaybookGraphLayout = {
  direction: "TD",
  grid_positions: {
    triage: { x: 0, y: 0 },
    review: { x: 0, y: 1 },
    approve: { x: 1, y: 1 },
    escalate: { x: 1, y: 2 },
    done: { x: 0, y: 3 },
  },
};
