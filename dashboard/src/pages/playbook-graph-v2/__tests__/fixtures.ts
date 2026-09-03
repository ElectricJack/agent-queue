import type {
  ActivationStateDTO,
  ArtifactRefDTO,
  ExplanationRowDTO,
  ExplanationValueDTO,
  GraphEdgeDTO,
  GraphNodeDTO,
  OutcomeExplanationDTO,
  PlaybookV2GraphResponse,
  RuleClusterDTO,
  SourceRefDTO,
} from "../../../api/client";

/** The §10.1 `review-pipeline` artifact as the backend projects it: two rule
 *  clusters over two events, thirteen steps, one of every step kind, and the
 *  `check-gate` case/default pair that shares a `(source, target)` and must
 *  stay independently selectable.
 *
 *  Hand-authored rather than generated: `src/playbooks/graph_projection.py`
 *  binds to Package 2's `PlaybookDefinition`, which is still in flight, so the
 *  dashboard slice is written against the frozen §4 DTOs instead. Every object
 *  below is typed against the generated client, so a DTO change breaks the
 *  typecheck rather than the runtime. */

const SOURCE = "system/playbooks/default-pipeline.md";

function src(start: number, end: number, heading?: string): SourceRefDTO {
  return { path: SOURCE, start_line: start, end_line: end, heading: heading ?? null };
}

export function value(
  display: string,
  overrides: Partial<ExplanationValueDTO> = {},
): ExplanationValueDTO {
  return { kind: "literal", display, canonical: display, redacted: false, ...overrides };
}

export function row(
  label: string,
  display: string,
  overrides: Partial<ExplanationRowDTO> = {},
): ExplanationRowDTO {
  return { label, value: value(display), source: "literal", required: true, ...overrides };
}

function outcome(
  name: string,
  target: string | null,
  overrides: Partial<OutcomeExplanationDTO> = {},
): OutcomeExplanationDTO {
  return {
    outcome: name,
    label: name.replace(/_/g, " "),
    target_step_id: target,
    target_title: target ? TITLES[target] ?? target : null,
    reserved: false,
    terminal_outcome: null,
    ...overrides,
  };
}

const TITLES: Record<string, string> = {
  "ensure-review-task": "Ensure a review task",
  "classify-risk": "Classify review risk",
  escalate: "Escalate to a senior reviewer",
  "await-approval": "Wait for human approval",
  "review-unavailable": "Review unavailable",
  "cancelled-end": "Cancelled",
  done: "Review complete",
  "list-downstream": "List downstream tasks",
  "for-each-task": "For each downstream task",
  "open-gate": "Open a spec-ingest gate",
  "check-gate": "Was the gate already open?",
  "sweep-done": "Sweep complete",
  "sweep-failed": "Sweep failed",
};

/** Every node carries a populated `advanced`; the Advanced view is a
 *  disclosure, never a different fetch. */
function advanced(typed: Record<string, unknown>, overrides: Partial<GraphNodeDTO["advanced"]> = {}) {
  return {
    typed_step: typed,
    resolved_inputs: [],
    result_schema: null,
    retry: null,
    idempotency: null,
    redaction: [],
    execution_fingerprint: null,
    ...overrides,
  };
}

export const ensureReviewTask: GraphNodeDTO = {
  id: "ensure-review-task",
  rule_id: "review-on-task-completed",
  step_kind: "command",
  title: TITLES["ensure-review-task"]!,
  description: "Idempotent on the review dedup key.",
  entry: true,
  terminal_outcome: null,
  explanation: {
    title: "Ensure a review task",
    effect_summary: "Create or reuse the matching review task",
    effects: [
      {
        kind: "creates",
        subject: "task",
        detail: "Creates a review task for the completed task, or reuses the existing one",
        arguments: [row("Dedup key", "review-of-<event.task_id>")],
        conditional_on: null,
      },
      {
        kind: "binds",
        subject: "review",
        detail: "Binds the resulting task as review",
        arguments: [],
        conditional_on: null,
      },
    ],
    inputs: [
      row("Project", "this event's project", {
        value: value("this event's project", { kind: "event_ref", canonical: { type: "event_ref", path: "project_id" } }),
        source: "event",
      }),
      row("Title", "Review: <event.title>", {
        value: value("Review: <event.title>", { kind: "template", canonical: { type: "template" } }),
        source: "template",
      }),
      row("Dedup key", "review-of-<event.task_id>", {
        value: value("review-of-<event.task_id>", { kind: "template", canonical: { type: "template" } }),
        source: "template",
      }),
    ],
    result: row("Saved as", "review", { source: "derived" }),
    outcomes: [
      outcome("created", "classify-risk"),
      outcome("reused", "classify-risk"),
      outcome("rejected", "review-unavailable"),
      outcome("runtime_error", "review-unavailable", { reserved: true }),
    ],
    contract_fingerprint: "sha256:c0ffee",
    renderer: "contract",
  },
  badges: [
    { kind: "idempotency", label: "idempotent", value: "dedup_key" },
    { kind: "retry", label: "retry", value: "2 attempts" },
  ],
  ai: null,
  loop: null,
  wait: null,
  source: src(20, 27, "Open review for a completed task"),
  advanced: advanced(
    { type: "command", command: "ensure_task", save_result_as: "review" },
    {
      resolved_inputs: [
        row("Project", "proj-7", { source: "event" }),
        row("Auth token", "«redacted»", {
          value: { kind: "redacted", display: "«redacted»", canonical: null, redacted: true },
          source: "policy",
        }),
      ],
      result_schema: { type: "object", properties: { task_id: { type: "string" } } },
      retry: { max_attempts: 2, backoff_seconds: 5, retry_on: ["runtime_error"] },
      idempotency: { supported: true, key_template: "<run_id>:<step_id>:<attempt>", retry_safe: true },
      redaction: [
        { field: "title", policy: "safe" },
        { field: "auth_token", policy: "redacted" },
      ],
      execution_fingerprint: "sha256:ensure-task-v3",
    },
  ),
  diagnostics: [],
  out_degree: 4,
  position: { x: 0, y: 0 },
};

export const classifyRisk: GraphNodeDTO = {
  id: "classify-risk",
  rule_id: "review-on-task-completed",
  step_kind: "llm",
  title: TITLES["classify-risk"]!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "Classify review risk",
    effect_summary: "Ask the reviewer model to classify the risk of this change",
    effects: [
      {
        kind: "invokes_ai",
        subject: "reviewer",
        detail: "Invokes the reviewer profile and binds the structured result as risk",
        arguments: [],
        conditional_on: null,
      },
    ],
    inputs: [row("Prompt", "Assess the review risk of task <event.title>", { source: "template" })],
    result: row("Saved as", "risk", { source: "derived" }),
    outcomes: [
      outcome("low", "await-approval"),
      outcome("high", "escalate"),
      outcome("invalid_output", "review-unavailable", { reserved: true }),
      outcome("budget_exceeded", "review-unavailable", { reserved: true }),
      outcome("provider_error", "review-unavailable", { reserved: true }),
      outcome("timed_out", "review-unavailable", { reserved: true }),
      outcome("cancelled", "cancelled-end", { reserved: true }),
      outcome("runtime_error", "review-unavailable", { reserved: true }),
    ],
    contract_fingerprint: null,
    renderer: "contract",
  },
  badges: [
    { kind: "profile", label: "profile", value: "reviewer" },
    { kind: "budget", label: "budget", value: "8000 tokens" },
  ],
  ai: {
    profile_id: "reviewer",
    intelligence_class: "deep",
    provider: "anthropic",
    model: "claude-opus-5",
    capabilities: {
      harness_tools: ["Read", "Grep"],
      aq_commands: ["task_show"],
      plugin_tools: [],
    },
    capability_fingerprint: "sha256:cap-reviewer-1",
    budget: { max_calls: 2, max_output_tokens: 1024, max_total_tokens: 8000, timeout_seconds: 120 },
    output_schema: { type: "object", properties: { risk: { enum: ["low", "high"] } }, required: ["risk"] },
    tool_use_enabled: false,
    delegation: null,
  },
  loop: null,
  wait: null,
  source: src(29, 34),
  advanced: advanced({ type: "llm", profile_id: "reviewer", outcome_field: "risk" }),
  diagnostics: [],
  out_degree: 8,
  position: { x: 0, y: 1 },
};

export const escalateNode: GraphNodeDTO = {
  id: "escalate",
  rule_id: "review-on-task-completed",
  step_kind: "agent_task",
  title: TITLES.escalate!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "Escalate to a senior reviewer",
    effect_summary: "Re-review the change and record the riskiest line",
    effects: [
      {
        kind: "delegates",
        subject: "reviewer",
        detail: "Delegates a child agent task and waits for it",
        arguments: [],
        conditional_on: null,
      },
    ],
    inputs: [row("Objective", "Re-review the change and record the riskiest line")],
    result: row("Saved as", "escalation", { source: "derived" }),
    outcomes: [
      outcome("completed", "await-approval"),
      outcome("failed", "review-unavailable"),
      outcome("timed_out", "review-unavailable", { reserved: true }),
      outcome("cancelled", "cancelled-end", { reserved: true }),
      outcome("runtime_error", "review-unavailable", { reserved: true }),
    ],
    contract_fingerprint: null,
    renderer: "contract",
  },
  badges: [
    { kind: "profile", label: "profile", value: "reviewer" },
    { kind: "wait", label: "wait", value: "waits for completion" },
    { kind: "timeout", label: "timeout", value: "3600s" },
  ],
  ai: {
    profile_id: "reviewer",
    intelligence_class: null,
    provider: null,
    model: null,
    capabilities: { harness_tools: ["Read"], aq_commands: [], plugin_tools: [] },
    capability_fingerprint: "sha256:cap-escalate-1",
    budget: { max_calls: null, max_output_tokens: null, max_total_tokens: null, timeout_seconds: 3600 },
    output_schema: null,
    tool_use_enabled: true,
    delegation: {
      child_profile_id: "reviewer",
      wait_for_completion: true,
      cancel_child: false,
      narrowed_from: "supervisor principal",
      capability_narrowing: { harness_tools: ["Read"], aq_commands: [], plugin_tools: null },
    },
  },
  loop: null,
  wait: null,
  source: src(36, 39),
  advanced: advanced({ type: "agent_task", profile_id: "reviewer", wait_for_completion: true }),
  diagnostics: [],
  out_degree: 5,
  position: { x: 1, y: 2 },
};

export const awaitApproval: GraphNodeDTO = {
  id: "await-approval",
  rule_id: "review-on-task-completed",
  step_kind: "wait",
  title: TITLES["await-approval"]!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "Wait for human approval",
    effect_summary: "Wait for a human decision on the review gate",
    effects: [
      {
        kind: "waits",
        subject: "gate",
        detail: "Waits for a human approve/revise decision, correlated on the review task",
        arguments: [],
        conditional_on: null,
      },
    ],
    inputs: [row("Awaited", "Approve the review")],
    result: row("Saved as", "approval", { source: "derived" }),
    outcomes: [
      outcome("approve", "done"),
      outcome("revise", "ensure-review-task"),
      outcome("timed_out", "review-unavailable", { reserved: true }),
      outcome("runtime_error", "review-unavailable", { reserved: true }),
    ],
    contract_fingerprint: null,
    renderer: "contract",
  },
  badges: [{ kind: "timeout", label: "timeout", value: "86400s" }],
  ai: null,
  loop: null,
  wait: {
    wait_kind: "human",
    awaited: "Approve the review",
    correlation_key: value("review.task_id", { kind: "binding_ref" }),
    timeout_seconds: 86400,
    timeout_step_id: "review-unavailable",
  },
  source: src(41, 44),
  advanced: advanced({ type: "wait", wait_kind: "human" }),
  diagnostics: [],
  out_degree: 4,
  position: { x: 0, y: 3 },
};

function terminal(id: string, outcomeName: string, x: number, y: number): GraphNodeDTO {
  return {
    id,
    rule_id: id.startsWith("sweep") ? "sweep-on-spec-approved" : "review-on-task-completed",
    step_kind: "terminal",
    title: TITLES[id]!,
    description: null,
    entry: false,
    terminal_outcome: outcomeName,
    explanation: {
      title: TITLES[id]!,
      effect_summary: `Ends the rule with outcome ${outcomeName}`,
      effects: [{ kind: "noop", subject: "rule", detail: `Ends the rule as ${outcomeName}`, arguments: [], conditional_on: null }],
      inputs: [],
      result: null,
      outcomes: [],
      contract_fingerprint: null,
      renderer: "contract",
    },
    badges: [],
    ai: null,
    loop: null,
    wait: null,
    source: src(45, 45),
    advanced: advanced({ type: "terminal", outcome: outcomeName }),
    diagnostics: [],
    out_degree: 0,
    position: { x, y },
  };
}

export const reviewUnavailable = terminal("review-unavailable", "failed", 1, 4);
export const cancelledEnd = terminal("cancelled-end", "cancelled", 2, 4);
export const doneNode = terminal("done", "completed", 0, 4);

export const listDownstream: GraphNodeDTO = {
  id: "list-downstream",
  rule_id: "sweep-on-spec-approved",
  step_kind: "command",
  title: TITLES["list-downstream"]!,
  description: null,
  entry: true,
  terminal_outcome: null,
  explanation: {
    title: "List downstream tasks",
    effect_summary: "Read the ready tasks in this event's project",
    effects: [{ kind: "reads", subject: "task", detail: "Reads every READY task in the project", arguments: [], conditional_on: null }],
    inputs: [row("Status", "READY")],
    result: row("Saved as", "downstream", { source: "derived" }),
    outcomes: [outcome("listed", "for-each-task"), outcome("runtime_error", "sweep-failed", { reserved: true })],
    contract_fingerprint: "sha256:list-tasks-v1",
    renderer: "contract",
  },
  badges: [],
  ai: null,
  loop: null,
  wait: null,
  source: src(50, 55, "Sweep downstream tasks for an approved spec"),
  advanced: advanced({ type: "command", command: "list_tasks" }),
  diagnostics: [],
  out_degree: 2,
  position: { x: 4, y: 0 },
};

export const forEachTask: GraphNodeDTO = {
  id: "for-each-task",
  rule_id: "sweep-on-spec-approved",
  step_kind: "foreach",
  title: TITLES["for-each-task"]!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "For each downstream task",
    effect_summary: "Repeat the gate body once per downstream task",
    effects: [{ kind: "branches", subject: "loop", detail: "Runs the body once per item, collecting failures", arguments: [], conditional_on: null }],
    inputs: [row("Collection", "downstream.tasks", { source: "binding" })],
    result: null,
    outcomes: [
      outcome("body", "open-gate", { label: "each task" }),
      outcome("completed", "sweep-done"),
      outcome("failed", "sweep-failed"),
      outcome("runtime_error", "sweep-failed", { reserved: true }),
    ],
    contract_fingerprint: null,
    renderer: "contract",
  },
  badges: [{ kind: "loop", label: "failure policy", value: "collect" }],
  ai: null,
  loop: {
    collection: value("downstream.tasks", { kind: "binding_ref" }),
    item_binding: "task",
    failure_policy: "collect",
    body_entry_step_id: "open-gate",
    continuation_step_id: "sweep-done",
  },
  wait: null,
  source: src(57, 62),
  advanced: advanced({ type: "foreach", item_binding: "task", failure_policy: "collect" }),
  diagnostics: [],
  out_degree: 4,
  position: { x: 4, y: 1 },
};

export const openGate: GraphNodeDTO = {
  id: "open-gate",
  rule_id: "sweep-on-spec-approved",
  step_kind: "command",
  title: TITLES["open-gate"]!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "Open a spec-ingest gate",
    effect_summary: "Create or reuse a review gate for this task",
    effects: [{ kind: "creates", subject: "gate", detail: "Creates a review gate titled after the loop item", arguments: [], conditional_on: null }],
    inputs: [row("Title", "Spec ingest: <task.title>", { source: "loop" })],
    result: row("Saved as", "gate", { source: "derived" }),
    outcomes: [
      outcome("created", "check-gate"),
      outcome("reused", "check-gate"),
      outcome("skipped", "check-gate"),
      outcome("rejected", "sweep-failed"),
      outcome("runtime_error", "sweep-failed", { reserved: true }),
    ],
    contract_fingerprint: "sha256:gate-create-v2",
    renderer: "contract",
  },
  badges: [{ kind: "idempotency", label: "idempotent", value: "gate title" }],
  ai: null,
  loop: null,
  wait: null,
  source: src(64, 66),
  advanced: advanced({ type: "command", command: "gate_create" }),
  diagnostics: [],
  out_degree: 5,
  position: { x: 5, y: 2 },
};

export const checkGate: GraphNodeDTO = {
  id: "check-gate",
  rule_id: "sweep-on-spec-approved",
  step_kind: "decision",
  title: TITLES["check-gate"]!,
  description: null,
  entry: false,
  terminal_outcome: null,
  explanation: {
    title: "Was the gate already open?",
    effect_summary: "Branch on whether the gate was newly created",
    effects: [{ kind: "branches", subject: "gate", detail: "Chooses the next step from gate.created", arguments: [], conditional_on: null }],
    inputs: [row("Condition", "gate.created == false", { source: "binding" })],
    result: null,
    outcomes: [
      outcome("already open", "for-each-task", { outcome: "case:0", label: "already open" }),
      outcome("default", "for-each-task"),
    ],
    contract_fingerprint: null,
    renderer: "contract",
  },
  badges: [],
  ai: null,
  loop: null,
  wait: null,
  source: src(67, 69),
  advanced: advanced({ type: "decision", cases: [{ label: "already open" }] }),
  diagnostics: [
    {
      severity: "question",
      code: "compile_question",
      message: "Both branches return to the loop head — is the case intentional?",
      rule_id: "sweep-on-spec-approved",
      step_id: "check-gate",
      source: null,
    },
  ],
  out_degree: 2,
  position: { x: 5, y: 3 },
};

export const sweepDone = terminal("sweep-done", "completed", 4, 4);
export const sweepFailed = terminal("sweep-failed", "failed", 5, 4);

export const nodes: GraphNodeDTO[] = [
  ensureReviewTask,
  classifyRisk,
  escalateNode,
  awaitApproval,
  reviewUnavailable,
  cancelledEnd,
  doneNode,
  listDownstream,
  forEachTask,
  openGate,
  checkGate,
  sweepDone,
  sweepFailed,
];

function e(
  ruleId: string,
  source: string,
  outcomeName: string,
  target: string,
  kind: GraphEdgeDTO["kind"],
  overrides: Partial<GraphEdgeDTO> = {},
): GraphEdgeDTO {
  return {
    id: `${ruleId}::${source}::${outcomeName}`,
    rule_id: ruleId,
    source,
    source_port: outcomeName,
    target,
    outcome: outcomeName,
    label: outcomeName.replace(/_/g, " "),
    kind,
    reserved: false,
    condition: null,
    ...overrides,
  };
}

const R1 = "review-on-task-completed";
const R2 = "sweep-on-spec-approved";

export const edges: GraphEdgeDTO[] = [
  e(R1, "ensure-review-task", "created", "classify-risk", "success"),
  e(R1, "ensure-review-task", "reused", "classify-risk", "success"),
  e(R1, "ensure-review-task", "rejected", "review-unavailable", "failure"),
  e(R1, "ensure-review-task", "runtime_error", "review-unavailable", "runtime_error", { reserved: true }),
  e(R1, "classify-risk", "low", "await-approval", "success"),
  e(R1, "classify-risk", "high", "escalate", "success"),
  e(R1, "classify-risk", "invalid_output", "review-unavailable", "failure", { reserved: true }),
  e(R1, "classify-risk", "budget_exceeded", "review-unavailable", "failure", { reserved: true }),
  e(R1, "classify-risk", "provider_error", "review-unavailable", "failure", { reserved: true }),
  e(R1, "classify-risk", "timed_out", "review-unavailable", "timeout", { reserved: true }),
  e(R1, "classify-risk", "cancelled", "cancelled-end", "cancelled", { reserved: true }),
  e(R1, "classify-risk", "runtime_error", "review-unavailable", "runtime_error", { reserved: true }),
  e(R1, "escalate", "completed", "await-approval", "success"),
  e(R1, "escalate", "failed", "review-unavailable", "failure"),
  e(R1, "escalate", "timed_out", "review-unavailable", "timeout", { reserved: true }),
  e(R1, "escalate", "cancelled", "cancelled-end", "cancelled", { reserved: true }),
  e(R1, "escalate", "runtime_error", "review-unavailable", "runtime_error", { reserved: true }),
  e(R1, "await-approval", "approve", "done", "wait_matched"),
  e(R1, "await-approval", "revise", "ensure-review-task", "loop_back"),
  e(R1, "await-approval", "timed_out", "review-unavailable", "timeout", { reserved: true }),
  e(R1, "await-approval", "runtime_error", "review-unavailable", "runtime_error", { reserved: true }),
  e(R2, "list-downstream", "listed", "for-each-task", "success"),
  e(R2, "list-downstream", "runtime_error", "sweep-failed", "runtime_error", { reserved: true }),
  e(R2, "for-each-task", "body", "open-gate", "loop_body", { label: "each task" }),
  e(R2, "for-each-task", "completed", "sweep-done", "loop_exit"),
  e(R2, "for-each-task", "failed", "sweep-failed", "failure"),
  e(R2, "for-each-task", "runtime_error", "sweep-failed", "runtime_error", { reserved: true }),
  e(R2, "open-gate", "created", "check-gate", "success"),
  e(R2, "open-gate", "reused", "check-gate", "success"),
  e(R2, "open-gate", "skipped", "check-gate", "success"),
  e(R2, "open-gate", "rejected", "sweep-failed", "failure"),
  e(R2, "open-gate", "runtime_error", "sweep-failed", "runtime_error", { reserved: true }),
  e(R2, "check-gate", "case:0", "for-each-task", "decision_case", {
    label: "already open",
    condition: "gate.created == false",
  }),
  e(R2, "check-gate", "default", "for-each-task", "decision_default", { label: "otherwise" }),
];

export const rules: RuleClusterDTO[] = [
  {
    rule_id: R1,
    name: "Open review for a completed task",
    event_type: "task.completed",
    trigger_filter: { review_task: false },
    entry_step_id: "ensure-review-task",
    step_ids: [
      "ensure-review-task",
      "classify-risk",
      "escalate",
      "await-approval",
      "review-unavailable",
      "cancelled-end",
      "done",
    ],
    source: src(18, 46, "Open review for a completed task"),
    diagnostics: [],
  },
  {
    rule_id: R2,
    name: "Sweep downstream tasks for an approved spec",
    event_type: "spec.approved",
    trigger_filter: null,
    entry_step_id: "list-downstream",
    step_ids: ["list-downstream", "for-each-task", "open-gate", "check-gate", "sweep-done", "sweep-failed"],
    source: src(48, 71, "Sweep downstream tasks for an approved spec"),
    diagnostics: [
      {
        severity: "warning",
        code: "invalid_reference",
        message: "downstream.tasks is not declared by list_tasks' result schema",
        rule_id: R2,
        step_id: "for-each-task",
        source: null,
      },
    ],
  },
];

export const artifact: ArtifactRefDTO = {
  playbook_id: "default-pipeline",
  artifact_sha256: `sha256:${"a1".repeat(32)}`,
  schema_generation: 2,
  contract_fingerprint: "sha256:contracts-v9",
  source_digest: `sha256:${"b2".repeat(32)}`,
  compiler_build: "playbook-compiler/2.0.0",
  compiled_at: "2026-09-01T12:00:00Z",
  version: 5,
};

export const activation: ActivationStateDTO = {
  playbook_id: "default-pipeline",
  scope: "system",
  scope_identifier: null,
  enabled: true,
  active_artifact_sha256: artifact.artifact_sha256,
  health: "question_required",
  reasons: [
    { code: "compile_question", message: "One unresolved compile question", subject: "check-gate", expected_fingerprint: null, actual_fingerprint: null },
  ],
  activated_at: 1_756_000_000,
  activated_by: "user:dashboard",
  pending_event_count: 0,
  running_count: 1,
};

export const graph: PlaybookV2GraphResponse = {
  success: true,
  artifact,
  activation,
  purpose: "routine",
  event_groups: [
    { event_type: "task.completed", rule_ids: [R1], node_count: 7, edge_count: 21 },
    { event_type: "spec.approved", rule_ids: [R2], node_count: 6, edge_count: 13 },
  ],
  rules,
  nodes,
  edges,
  layout: {
    direction: "TD",
    grid_positions: Object.fromEntries(nodes.map((n) => [n.id, n.position!])),
    cluster_bounds: {
      [R1]: { x: 0, y: 0, width: 3, height: 5 },
      [R2]: { x: 4, y: 0, width: 2, height: 5 },
    },
  },
  diagnostics: [
    {
      severity: "warning",
      code: "stale_contract",
      message: "gate_create's contract changed since this artifact was compiled",
      rule_id: R2,
      step_id: "open-gate",
      source: null,
    },
    {
      severity: "info",
      code: "activation_disabled",
      message: "This artifact is not the active one for its scope",
      rule_id: null,
      step_id: null,
      source: null,
    },
  ],
  legend: {
    step_kinds: { command: "command", llm: "AI", terminal: "terminal" },
    edge_kinds: { success: "success", failure: "failure" },
  },
};

/** A single-rule, single-node graph for tests that need the smallest possible
 *  well-formed response. */
export const tinyGraph: PlaybookV2GraphResponse = {
  ...graph,
  event_groups: [{ event_type: "task.completed", rule_ids: [R1], node_count: 1, edge_count: 0 }],
  rules: [{ ...rules[0]!, step_ids: ["done"], entry_step_id: "done" }],
  nodes: [{ ...doneNode, entry: true, position: { x: 0, y: 0 } }],
  edges: [],
  diagnostics: [],
  layout: {
    direction: "TD",
    grid_positions: { done: { x: 0, y: 0 } },
    cluster_bounds: { [R1]: { x: 0, y: 0, width: 1, height: 1 } },
  },
};
