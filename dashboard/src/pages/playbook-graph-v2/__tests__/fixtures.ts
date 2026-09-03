import type {
  GraphNodeDTO,
  PlaybookRunOverlayResponse,
  PlaybookV2GraphResponse,
} from "../../../api/client";
import { projectedGraph, projectedNode } from "./projected";

/** The §10.1 `review-pipeline` artifact as the backend projects it: two rule
 *  clusters over two events, thirteen steps, one of every step kind, and the
 *  `check-gate` case/default pair that shares a `(source, target)` and must
 *  stay independently selectable.
 *
 *  Every node here comes out of `graph.fixture.json`, which is written by
 *  `python -m tests.playbook_v2_helpers` and asserted byte for byte against a
 *  live `project_graph` call by `tests/test_playbook_v2_api_dtos.py`. Nothing
 *  in this file re-states a semantic field: a component test that reads it is
 *  testing the payload the daemon actually serves, which is the whole point of
 *  §16.10 deviation 1 — a hand-authored node can agree with a component while
 *  disagreeing with the projector, which is how the compact card came to omit
 *  its key inputs and its output binding for a whole package
 *  (`solid-harbor.49` pass 2).
 *
 *  Both command branches are read, not built: the stub contract registry
 *  models `ensure_task` and `gate_create` — and declares `dedup_key` sensitive
 *  on the first, because no shipped command declares a sensitive argument —
 *  while `list_tasks` stays unregistered. So `ensureReviewTask` is a projected
 *  contract node, with both fingerprints, its retry policy, its key template
 *  and a redacted input, and `listDownstream` is a projected *un*contracted
 *  one, with an `unknown_command` diagnostic and unresolved values.
 *
 *  Two kinds of thing are still built here rather than read:
 *
 *  * run overlays, which `project_graph` does not produce at all — they are
 *    `playbook_run_overlay`'s response, pinned to the projected artifact so
 *    the canvas' artifact check is exercised against the real hash;
 *  * `unroutedEscalateNode` below, a projected node with the exact fields
 *    `src/playbooks/graph_projection.py` writes down a branch this artifact
 *    does not reach (a profile the lookup resolves no routing for).
 */

export const graph: PlaybookV2GraphResponse = projectedGraph;

export const artifact = projectedGraph.artifact;
export const activation = projectedGraph.activation;
export const nodes = projectedGraph.nodes!;
export const edges = projectedGraph.edges!;
export const rules = projectedGraph.rules!;

export const ensureReviewTask = projectedNode("ensure-review-task");
export const classifyRisk = projectedNode("classify-risk");
export const escalateNode = projectedNode("escalate");
export const awaitApproval = projectedNode("await-approval");
export const reviewUnavailable = projectedNode("review-unavailable");
export const cancelledEnd = projectedNode("cancelled-end");
export const doneNode = projectedNode("done");
export const listDownstream = projectedNode("list-downstream");
export const forEachTask = projectedNode("for-each-task");
export const openGate = projectedNode("open-gate");
export const checkGate = projectedNode("check-gate");
export const sweepDone = projectedNode("sweep-done");
export const sweepFailed = projectedNode("sweep-failed");

const R1 = "review-on-task-completed";
const R2 = "sweep-on-spec-approved";

/** The artifact this projection is of, reported as the active one.
 *
 *  `project_golden_graph` projects with no activation row, so the fixture's
 *  `activation` is the disabled default (`ActivationStateDTO`'s field
 *  defaults). This is the same DTO with the fields an activated artifact
 *  carries, which is the state the header's "active" branch is for. */
export const activeGraph: PlaybookV2GraphResponse = {
  ...graph,
  activation: {
    ...activation,
    enabled: true,
    active_artifact_sha256: artifact.artifact_sha256,
    health: "ready",
    activated_at: 1_756_000_000,
    activated_by: "user:dashboard",
    running_count: 1,
  },
};

/** `escalate` as the projector renders it when the profile lookup resolves no
 *  routing: `_ai_detail` reads `intelligence_class` / `provider` / `model` off
 *  the lookup's `routing()` answer, and every one of them is `None` when the
 *  lookup has none (`graph_projection.py:_routing`). */
export const unroutedEscalateNode: GraphNodeDTO = {
  ...escalateNode,
  ai: { ...escalateNode.ai!, intelligence_class: null, provider: null, model: null },
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

/** One completed run of the `sweep-on-spec-approved` rule against the exact
 *  artifact above: it listed three downstream tasks, went round the foreach
 *  body three times, and left through the loop's exit. The
 *  `review-on-task-completed` cluster was never entered, which is what makes
 *  this fixture useful — half the graph must stay visibly untouched. */
export const runOverlay: PlaybookRunOverlayResponse = {
  success: true,
  run_id: "run-42",
  artifact,
  artifact_is_active: false,
  rule_id: R2,
  lifecycle: "completed",
  current_step_id: null,
  started_at: 1_756_000_100,
  completed_at: 1_756_000_400,
  nodes: [
    { step_id: "list-downstream", state: "completed", visit_count: 1, last_outcome: "listed", receipt_ids: ["r-list"] },
    {
      step_id: "for-each-task",
      state: "completed",
      visit_count: 3,
      last_outcome: "completed",
      receipt_ids: ["r-loop"],
      iterations: [
        { index: 0, item_display: "task-a", outcome: "created", receipt_ids: ["r-a"] },
        { index: 1, item_display: "task-b", outcome: "reused", receipt_ids: ["r-b"] },
        { index: 2, item_display: "task-c", outcome: "created", receipt_ids: ["r-c"] },
      ],
    },
    { step_id: "open-gate", state: "completed", visit_count: 3, last_outcome: "created", receipt_ids: ["r-a", "r-b", "r-c"] },
    { step_id: "check-gate", state: "completed", visit_count: 3, last_outcome: "default", receipt_ids: [] },
    { step_id: "sweep-done", state: "completed", visit_count: 1, last_outcome: "completed", receipt_ids: [] },
  ],
  edges: [
    { edge_id: `${R2}::list-downstream::listed`, traversal_count: 1, last_traversed_at: 1_756_000_110 },
    { edge_id: `${R2}::for-each-task::body`, traversal_count: 3, last_traversed_at: 1_756_000_300 },
    { edge_id: `${R2}::open-gate::created`, traversal_count: 3, last_traversed_at: 1_756_000_320 },
    { edge_id: `${R2}::check-gate::default`, traversal_count: 3, last_traversed_at: 1_756_000_340 },
    { edge_id: `${R2}::for-each-task::completed`, traversal_count: 1, last_traversed_at: 1_756_000_390 },
  ],
  receipts: [],
  bindings: [],
  truncated: false,
  receipt_total: 0,
};

/** The same run, reported against an artifact this projection is not of. */
export const foreignRunOverlay: PlaybookRunOverlayResponse = {
  ...runOverlay,
  run_id: "run-43",
  artifact: { ...artifact, artifact_sha256: `sha256:${"c3".repeat(32)}`, version: 4 },
};
