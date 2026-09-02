import type {
  PlaybookGraphEdge,
  PlaybookGraphNode,
  PlaybookGraphNodesEdges,
  PlaybookGraphLayout,
} from "../../../api/client";
import type { ExplainedPlaybookGraphNode, NodeExplanation } from "../explanation";

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

/* Contract-derived intent fixtures.
 *
 * The two contract explanations are IMPORTED from the backend goldens in
 * `tests/fixtures/contracts/` — the exact payloads
 * `render_node_explanation` produces for `pipeline-intent.md`, pinned by
 * `tests/test_contract_intent_parity.py`. Transcribing them into TypeScript by
 * hand (what this file did) meant the dashboard asserted copy the backend had
 * never rendered: the two drifted the moment the contract's presentation
 * changed, and nothing failed. Regenerate the goldens on the backend and both
 * suites move together.
 *
 * `node()` above deliberately leaves `explanation` undefined, so every
 * pre-existing test keeps exercising the uncontracted fallback path — which is
 * also the flag-off path. */

import createReviewGolden from "../../../../../tests/fixtures/contracts/explanation-create-review.json";
import gateDownstreamGolden from "../../../../../tests/fixtures/contracts/explanation-gate-downstream.json";

/** `ensure_task` under the per-task-review rule. */
export const createReviewExplanation = createReviewGolden as NodeExplanation;

/** The `for_each` gate node: a loop, a conditional effect, a `loop_ref` value
 *  and no result binding. */
export const gateDownstreamExplanation = gateDownstreamGolden as NodeExplanation;

/** The gate node with an argument the contract does not declare. No shipped
 *  node has one — `unrendered_fields` is the renderer's promise that an
 *  unknown executable key is still shown rather than dropped, so the fixture
 *  is a deliberate variation on a real payload, not a claim about it. */
export const explanationWithUnrenderedField: NodeExplanation = {
  ...gateDownstreamExplanation,
  unrendered_fields: ["reason"],
};

/** A contract whose argument the registry marks sensitive. No built-in
 *  declares `sensitive_args` yet (the backend proves the policy with a
 *  synthetic contract in `tests/test_playbook_explanation.py`), so this
 *  fixture is synthetic too: the placeholder is the only thing the payload
 *  carries, and there is no secret to leak. */
export const redactedExplanation: NodeExplanation = {
  kind: "command",
  title: "Route a task to a profile",
  command: "task_route",
  capability: "task_route",
  effects: [{ operation: "update", text: "Update the task's routing", subject: "task_routing" }],
  inputs: [
    {
      field: "webhook_token",
      label: "Webhook token",
      required: true,
      value: { kind: "literal", text: "[redacted]", raw: null, redacted: true },
    },
  ],
  outcomes: [],
  unrendered_fields: [],
};

/** The §10.2 node as the graph view returns it: the compiled action is still
 *  present for the Advanced disclosure, alongside the derived explanation. */
export const explanationNode: ExplainedPlaybookGraphNode = {
  ...node("per-task-review-create-review", {
    entry: true,
    out_degree: 2,
    details: {
      entry: true,
      action: {
        command: "ensure_task",
        args: {
          project_id: "{{event.project_id}}",
          dedup_key: "review:task:{{event.task_id}}",
          title: "Review: {{event.title}}",
          description: "Branch: {{event.task.branch_name}}",
          profile_id: "reviewer",
        },
        on_success: "per-task-review-link-discovered-from",
        on_failure: "per-task-review-done",
      },
      output: { as: "review" },
    },
  }),
  explanation: createReviewExplanation,
};

/** The looping gate node, with its `for_each` payload intact. */
export const loopExplanationNode: ExplainedPlaybookGraphNode = {
  ...node("per-task-review-gate-downstream", {
    out_degree: 1,
    details: {
      action: {
        command: "gate_create",
        args: {
          project_id: "{{event.project_id}}",
          gate_type: "task",
          title: "Awaiting review of {{event.task_id}}",
          await_id: "{{outputs.review.task_id}}",
          waiter_task_ids: ["{{outputs.dep.id}}"],
        },
        on_success: "per-task-review-done",
        on_failure: "per-task-review-done",
        for_each: { source: "outputs.downstream.tasks", as: "dep" },
      },
    },
  }),
  explanation: gateDownstreamExplanation,
};

/** The §10.2 node with its explanation stripped — the uncontracted node the
 *  inspector and the card must keep serving exactly as they do today. */
export const uncontractedNode: ExplainedPlaybookGraphNode = {
  ...explanationNode,
  explanation: undefined,
};

/** `pipelineGraph` with contract intent attached to its action node. */
export const explainedPipelineGraph: PlaybookGraphNodesEdges = {
  nodes: [explanationNode, node("per-task-review-link-discovered-from"), node("per-task-review-done")],
  edges: [
    edge("per-task-review-create-review", "per-task-review-link-discovered-from", {
      edge_type: "success",
      label: "success",
    }),
    edge("per-task-review-create-review", "per-task-review-done", {
      edge_type: "failure",
      label: "failure",
    }),
  ],
};
