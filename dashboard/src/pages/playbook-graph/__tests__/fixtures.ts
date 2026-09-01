import type {
  PlaybookGraphEdge,
  PlaybookGraphNode,
  PlaybookGraphNodesEdges,
  PlaybookGraphLayout,
} from "../../../api/client";

const COLORS = { fill: "#E3F2FD", stroke: "#1565C0", text: "#000000" };

/** Long enough that a truncating inspector would visibly drop the tail. */
export const REVIEW_PROMPT = [
  "Review the diff for correctness.",
  "",
  "Check every changed file for:",
  "- behaviour changes that lack a test",
  "- error paths that swallow failures",
  "",
  "Finish by stating the single riskiest line in the diff.",
].join("\n");

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
    node("review", {
      prompt_preview: "Review the diff",
      timeout_seconds: 600,
      on_timeout: "escalate",
      details: {
        prompt: REVIEW_PROMPT,
        transitions: [
          { goto: "approve", when: "diff_is_clean" },
          { goto: "escalate", otherwise: true },
        ],
        timeout_seconds: 600,
        pause_timeout_seconds: 120,
        on_timeout: "escalate",
        action: { type: "notify", channel: "#reviews" },
        for_each: { items: "changed_files", as: "file" },
        output: { verdict: "string", notes: "string" },
        llm_config: { provider: "anthropic", model: "claude-opus-5", max_tokens: 4096, temperature: 0.2 },
        transition_llm_config: { provider: "anthropic", model: "claude-haiku-4-5-20251001" },
      },
    }),
    node("approve", {
      type: "checkpoint",
      symbol: "⏸",
      wait_for_human: true,
      prompt_preview: "Approve the change",
      details: { prompt: "Approve the change", wait_for_human: true, goto: "done" },
    }),
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

/** A deterministic pipeline action: its outcomes are execution results, not
 * prompt-derived transitions. */
export const pipelineGraph: PlaybookGraphNodesEdges = {
  nodes: [
    node("ensure-review", {
      entry: true,
      out_degree: 2,
      details: {
        entry: true,
        action: {
          command: "ensure_task",
          args: { title: "Review the proposal" },
          on_success: "review-ready",
          on_failure: "review-failed",
        },
      },
    }),
    node("review-ready", { terminal: true, out_degree: 0, details: { terminal: true } }),
    node("review-failed", { terminal: true, out_degree: 0, details: { terminal: true } }),
  ],
  edges: [
    edge("ensure-review", "review-ready", { edge_type: "success", label: "success" }),
    edge("ensure-review", "review-failed", { edge_type: "failure", label: "failure" }),
  ],
};

export const pipelineLayout: PlaybookGraphLayout = {
  direction: "TD",
  grid_positions: {
    "ensure-review": { x: 0, y: 0 },
    "review-ready": { x: 0, y: 1 },
    "review-failed": { x: 1, y: 1 },
  },
};
