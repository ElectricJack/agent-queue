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

/* Contract-derived intent fixtures. Transcribed from the Package 1 child plan's
 * golden explanations (§10.2, §10.3) so the dashboard asserts the same rendered
 * strings the backend suite pins. `node()` above deliberately leaves
 * `explanation` undefined, so every pre-existing test keeps exercising the
 * uncontracted fallback path — which is also the flag-off path. */

/** §10.2 — `ensure_task` under the per-task-review rule. */
export const createReviewExplanation: NodeExplanation = {
  kind: "command",
  title: "Ensure a review task exists",
  command: "ensure_task",
  capability: "ensure_task",
  effects: [
    {
      operation: "create_or_reuse",
      text: 'Create or reuse a task keyed by "dedup_key"',
      condition: null,
      subject: "task",
    },
  ],
  inputs: [
    {
      field: "project_id",
      label: "Project",
      required: false,
      value: {
        kind: "event_ref",
        text: "this event's project",
        raw: "{{event.project_id}}",
        redacted: false,
      },
    },
    {
      field: "dedup_key",
      label: "Deduplication key",
      required: true,
      value: {
        kind: "template",
        text: '"review:task:" + this event\'s task',
        raw: "review:task:{{event.task_id}}",
        redacted: false,
      },
    },
    {
      field: "title",
      label: "Title",
      required: true,
      value: {
        kind: "template",
        text: '"Review: " + this event\'s title',
        raw: "Review: {{event.title}}",
        redacted: false,
      },
    },
    {
      field: "description",
      label: "Description",
      required: false,
      value: {
        kind: "template",
        text: '"Branch: " + this event\'s task branch',
        raw: "Branch: {{event.task.branch_name}}",
        redacted: false,
      },
    },
    {
      field: "profile_id",
      label: "Agent profile",
      required: false,
      value: { kind: "literal", text: "reviewer", raw: null, redacted: false },
    },
  ],
  result: { name: "review", fields: ["task_id", "created"] },
  outcomes: [
    {
      outcome: "success",
      label: "Success",
      classification: "success",
      target_node_id: "per-task-review-link-discovered-from",
      target_label: "per-task-review-link-discovered-from",
    },
    {
      outcome: "failure",
      label: "Failure",
      classification: "failure",
      target_node_id: "per-task-review-done",
      target_label: "per-task-review-done",
    },
  ],
  loop: null,
  idempotency: "Repeating with the same deduplication key reuses the existing task",
  retry: "Safe to retry",
  unrendered_fields: [],
};

/** §10.3 — the `for_each` gate node: a loop, a conditional effect, a
 *  `loop_ref` value, no result binding, and an unrendered argument. */
export const gateDownstreamExplanation: NodeExplanation = {
  kind: "command",
  title: "Open a gate for each downstream task",
  command: "gate_create",
  capability: "gate_create",
  effects: [
    {
      operation: "create_or_reuse",
      text: 'Create or reuse a gate keyed by "await_id"',
      condition: null,
      subject: "gate",
    },
    {
      operation: "link",
      text: "Block the waiting tasks until the gate resolves",
      condition: "when waiter_task_ids is provided",
      subject: "gate",
    },
  ],
  inputs: [
    {
      field: "await_id",
      label: "Await id",
      required: true,
      value: {
        kind: "template",
        text: '"gate:review:" + each dep\'s id',
        raw: "gate:review:{{dep.id}}",
        redacted: false,
      },
    },
    {
      field: "waiter_task_ids",
      label: "Waiting tasks",
      required: false,
      value: { kind: "loop_ref", text: "each dep's id", raw: "{{dep.id}}", redacted: false },
    },
  ],
  result: null,
  outcomes: [
    {
      outcome: "success",
      label: "Success",
      classification: "success",
      target_node_id: "gate-downstream-done",
      target_label: "gate-downstream-done",
    },
  ],
  loop: {
    source_text: "each item in downstream.tasks",
    item_binding: "dep",
    source_raw: "outputs.downstream.tasks",
  },
  idempotency: "Repeating with the same await_id reuses the existing gate",
  retry: "Not safe to retry",
  unrendered_fields: ["reason"],
};

/** A contract whose argument the registry marks sensitive. The placeholder is
 *  the only thing the payload carries; there is no secret to leak. */
export const redactedExplanation: NodeExplanation = {
  kind: "command",
  title: "Route the task to an agent",
  command: "task_route",
  capability: "task_route",
  effects: [{ operation: "update", text: "Route the task to a profile", subject: "task" }],
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
  ...node("gate-downstream-open-gate", {
    out_degree: 1,
    details: {
      action: {
        command: "gate_create",
        args: { await_id: "gate:review:{{dep.id}}", waiter_task_ids: "{{dep.id}}", reason: "review" },
        on_success: "gate-downstream-done",
      },
      for_each: { items: "outputs.downstream.tasks", as: "dep" },
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
