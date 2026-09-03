"""Typed response models for the Playbook V2 semantic-graph surface.

Deliberately separate from ``src/api/models/playbook.py`` (V1): the two share
no field, and Package 7 deletes the V1 module wholesale.

This module is the frozen interface contract of
``docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`` §4 — the
parallelism unlock that lets the backend and dashboard slices of Package 5
proceed independently.  A backend task may **add** an optional field with a
default; it may not rename, retype, or remove one without amending §4 and
re-running every dashboard suite.

Conventions (all enforced by ``tests/test_playbook_v2_api_dtos.py``):

* every model sets ``model_config = ConfigDict(extra="forbid")``;
* optional blocks are ``X | None = None`` and serialize as explicit ``null`` —
  the V2 commands are **not** added to ``src.api.codegen.RESPONSE_EXCLUDE_NONE``;
* timestamps are ``float`` POSIX seconds, except ``ArtifactRefDTO.compiled_at``
  which is an ISO-8601 string (matching ``PlaybookGraphIdentity.compiled_at``);
* hashes are the full ``"sha256:<64 lowercase hex>"`` form, never truncated
  server-side; the UI truncates for display;
* every free-text field is already redacted server-side.  There is no
  client-side redaction anywhere in this package.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class V2Model(BaseModel):
    """Strict base — an unknown key is a contract break, not a warning."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# §4.1 Shared identity and value primitives
# ---------------------------------------------------------------------------


class ArtifactRefDTO(V2Model):
    """Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
    immutable artifact; every graph, diff and overlay response carries one."""

    playbook_id: str
    artifact_sha256: str  # "sha256:<64 hex>"
    schema_generation: int  # PlaybookDefinition.schema_version
    contract_fingerprint: str  # canonical digest of compiled_against.commands
    source_digest: str  # "sha256:<64 hex>" of the Markdown source
    compiler_build: str  # compiler build identity
    compiled_at: str | None = None  # ISO-8601 UTC
    version: int = 0  # monotonic per playbook, display only


class SourceRefDTO(V2Model):
    """Where in the authoring Markdown this element came from."""

    #: Vault-relative, e.g. ``"system/playbooks/default-pipeline.md"`` — system
    #: playbooks live under ``vault/system/playbooks/`` (see
    #: ``src/commands/playbook_commands.py`` ``_vault_playbook_dirs``).
    path: str
    start_line: int
    end_line: int
    heading: str | None = None
    excerpt: str | None = None  # <= 400 chars, redaction-clean


ValueKind = Literal[
    "literal",
    "event_ref",
    "binding_ref",
    "loop_ref",
    "template",
    "expression",
    "redacted",
    "unresolved",
]


class ExplanationValueDTO(V2Model):
    """One typed value, in both its human and canonical forms.

    ``display`` is always present and always safe to render.  ``canonical`` is
    the Advanced-view payload and is ``None`` whenever ``redacted`` is true.
    """

    kind: ValueKind
    display: str
    canonical: Any | None = None
    redacted: bool = False
    type_name: str | None = None  # declared type, e.g. "string", "TaskRef"


ValueSource = Literal[
    "event",
    "binding",
    "loop",
    "literal",
    "template",
    "profile",
    "policy",
    "derived",
]


class ExplanationRowDTO(V2Model):
    """A labelled input/output row: ``Project -> this event's project``."""

    label: str
    value: ExplanationValueDTO
    source: ValueSource
    required: bool = True
    description: str | None = None


# ---------------------------------------------------------------------------
# §4.2 Explanation (Package 1's payload, projected 1:1)
# ---------------------------------------------------------------------------


EffectKind = Literal[
    "creates",
    "updates",
    "deletes",
    "reads",
    "sends",
    "schedules",
    "waits",
    "branches",
    "binds",
    "invokes_ai",
    "delegates",
    "noop",
]


class EffectClauseDTO(V2Model):
    """One typed effect clause from the command contract.

    ``detail`` is rendered by the backend from the clause and its resolved
    inputs.  The frontend lays this out; it never re-derives command meaning
    (design spec: "The frontend lays out this structure but does not
    reinterpret command semantics")."""

    kind: EffectKind
    subject: str  # "task", "gate", "message", ...
    detail: str
    arguments: list[ExplanationRowDTO] = []
    conditional_on: str | None = None  # rendered condition, when conditional


class OutcomeExplanationDTO(V2Model):
    """One legal outcome of a step and where it goes."""

    outcome: str  # exact typed outcome, e.g. "success", "approve"
    label: str  # human label, presentation-only
    target_step_id: str | None = None  # None only for a terminal outcome
    target_title: str | None = None
    reserved: bool = False  # engine-reserved rather than business outcome
    terminal_outcome: str | None = None  # set when the outcome ends the rule


ExplanationRenderer = Literal["contract", "canonical"]


class StepExplanationDTO(V2Model):
    """The contract-derived intent card.  Node card and inspector consume
    this same object (design spec UI invariant).

    ``renderer="canonical"`` is the spec's lossless fallback: presentation
    metadata was absent, so every executable field is shown as a field/value
    pair.  It is a display fact, never a reason to hide a field, and never
    blocks activation.
    """

    title: str
    effect_summary: str
    effects: list[EffectClauseDTO] = []
    inputs: list[ExplanationRowDTO] = []
    result: ExplanationRowDTO | None = None
    outcomes: list[OutcomeExplanationDTO] = []
    contract_fingerprint: str | None = None  # None for non-command steps
    renderer: ExplanationRenderer = "contract"


# ---------------------------------------------------------------------------
# §4.4 Activation and health
#
# Defined *above* the graph block on purpose: ``PlaybookV2GraphResponse``
# references ``ActivationStateDTO``, and a plain forward-free ordering avoids
# ``model_rebuild()`` and produces cleaner generated TypeScript.
# ---------------------------------------------------------------------------


ActivationHealthValue = Literal[
    "ready",
    "question_required",
    "invalid",
    "disabled",
    "stale_contract",
    "unavailable",
]


class ActivationHealthReasonDTO(V2Model):
    code: str  # e.g. "command_contract_changed"
    message: str
    subject: str | None = None  # command name / profile id / file path
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


class ActivationStateDTO(V2Model):
    """``enabled`` and ``health`` are independent (design spec).  A disabled
    activation still reports its computed health; ``health="disabled"`` is used
    only when there is no active artifact at all."""

    playbook_id: str
    scope: str  # "system" | "project" | "agent_type"
    scope_identifier: str | None = None
    enabled: bool = False
    active_artifact_sha256: str | None = None
    health: ActivationHealthValue = "disabled"
    reasons: list[ActivationHealthReasonDTO] = []
    activated_at: float | None = None
    activated_by: str | None = None
    pending_event_count: int = 0
    running_count: int = 0


class PlaybookActivationHealthResponse(V2Model):
    success: bool = True
    activations: list[ActivationStateDTO] = []
    count: int = 0
    by_health: dict[str, int] = {}  # ActivationHealthValue -> count


class SetPlaybookActivationResponse(V2Model):
    success: bool = True
    activation: ActivationStateDTO
    previous_artifact_sha256: str | None = None
    changed: bool = False
    blocked: bool = False
    blockers: list[str] = []  # non-empty only when blocked


# ---------------------------------------------------------------------------
# §4.3 Graph
# ---------------------------------------------------------------------------


StepKind = Literal["command", "llm", "agent_task", "decision", "wait", "foreach", "terminal"]

EdgeKind = Literal[
    "success",
    "failure",
    "decision_case",
    "decision_default",
    "loop_body",
    "loop_exit",
    "loop_back",
    "timeout",
    "wait_matched",
    "runtime_error",
    "cancelled",
    "terminal",
]

DiagnosticSeverity = Literal["error", "warning", "question", "info"]


class GraphDiagnosticDTO(V2Model):
    """A compile question, invalid reference, stale contract or disabled
    activation.  Diagnostics annotate the graph; they never hide it."""

    severity: DiagnosticSeverity
    code: str  # stable machine code, e.g. "stale_contract"
    message: str
    rule_id: str | None = None
    step_id: str | None = None
    source: SourceRefDTO | None = None


class GridPositionDTO(V2Model):
    x: int = 0
    y: int = 0


class ClusterBoundsDTO(V2Model):
    """Grid-unit bounding box of one rule cluster."""

    x: int
    y: int
    width: int
    height: int


class GraphLayoutDTO(V2Model):
    direction: Literal["TD", "LR"] = "TD"
    grid_positions: dict[str, GridPositionDTO] = {}
    cluster_bounds: dict[str, ClusterBoundsDTO] = {}  # rule_id -> bounds


class CapabilityNamespacesDTO(V2Model):
    """``CapabilityPolicy`` projected.  Sorted; empty list means deny-all."""

    harness_tools: list[str] = []
    aq_commands: list[str] = []
    plugin_tools: list[str] = []


class AiBudgetDTO(V2Model):
    max_calls: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    timeout_seconds: int | None = None


class CapabilityNarrowingDTO(V2Model):
    """``AgentTaskStep.capability_narrowing`` projected.

    Unlike :class:`CapabilityNamespacesDTO` every namespace is nullable, because
    the narrowing's ``None`` (this step narrows nothing here) and ``[]`` (none)
    are different instructions and the card has to be able to say which one the
    author wrote.  Lists are sorted for a stable card.
    """

    harness_tools: list[str] | None = None
    aq_commands: list[str] | None = None
    plugin_tools: list[str] | None = None


class DelegationPolicyDTO(V2Model):
    """AgentTaskStep only."""

    child_profile_id: str
    wait_for_completion: bool = True
    cancel_child: bool = False
    narrowed_from: str | None = None  # parent principal provenance, human-readable
    #: Roadmap §2's third intersection term: the child principal is
    #: ``parent ∩ child profile ∩ this``.  ``None`` means the step narrows
    #: nothing beyond the first two terms.
    capability_narrowing: CapabilityNarrowingDTO | None = None


class AiNodeDetailDTO(V2Model):
    """Everything an operator needs about an AI state (design spec: "AI cards
    show the profile, resolved capability namespaces, capability fingerprint,
    budgets, and delegation policy")."""

    profile_id: str
    intelligence_class: str | None = None
    provider: str | None = None
    model: str | None = None
    capabilities: CapabilityNamespacesDTO
    capability_fingerprint: str
    budget: AiBudgetDTO
    output_schema: dict[str, Any] | None = None
    tool_use_enabled: bool = False
    delegation: DelegationPolicyDTO | None = None


class LoopNodeDetailDTO(V2Model):
    """ForEachStep only."""

    collection: ExplanationValueDTO
    item_binding: str
    failure_policy: Literal["halt", "continue", "collect"]
    body_entry_step_id: str
    continuation_step_id: str | None = None


class WaitNodeDetailDTO(V2Model):
    """WaitStep only."""

    wait_kind: Literal["event", "human", "task", "timer"]
    awaited: str  # event type, gate title, or task reference
    correlation_key: ExplanationValueDTO
    timeout_seconds: int | None = None
    timeout_step_id: str | None = None


class RetryPolicyDTO(V2Model):
    max_attempts: int = 1
    backoff_seconds: float | None = None
    retry_on: list[str] = []  # outcomes that retry rather than transition


class IdempotencyDTO(V2Model):
    supported: bool = False
    key_template: str | None = None  # e.g. "<run_id>:<step_id>:<attempt>"
    retry_safe: bool = False  # False -> operator_decision_required on ambiguity


class RedactionRowDTO(V2Model):
    field: str
    policy: Literal["safe", "summarized", "opaque_handle", "redacted"]


class NodeAdvancedDTO(V2Model):
    """Advanced view.  Canonical data, never the default explanation."""

    typed_step: dict[str, Any]  # the exact step object from the artifact
    resolved_inputs: list[ExplanationRowDTO] = []
    result_schema: dict[str, Any] | None = None
    retry: RetryPolicyDTO | None = None
    idempotency: IdempotencyDTO | None = None
    redaction: list[RedactionRowDTO] = []
    execution_fingerprint: str | None = None


class NodeBadgeDTO(V2Model):
    """One compact chip on the card.  Ordered by the backend."""

    kind: Literal[
        "profile",
        "budget",
        "capability",
        "timeout",
        "retry",
        "idempotency",
        "loop",
        "wait",
        "redaction",
        "diagnostic",
    ]
    label: str
    value: str


class GraphNodeDTO(V2Model):
    id: str  # artifact-local step id
    rule_id: str
    step_kind: StepKind
    title: str
    description: str | None = None
    entry: bool = False
    terminal_outcome: str | None = None
    explanation: StepExplanationDTO
    badges: list[NodeBadgeDTO] = []
    ai: AiNodeDetailDTO | None = None
    loop: LoopNodeDetailDTO | None = None
    wait: WaitNodeDetailDTO | None = None
    source: SourceRefDTO
    advanced: NodeAdvancedDTO
    diagnostics: list[GraphDiagnosticDTO] = []
    out_degree: int = 0
    position: GridPositionDTO = GridPositionDTO()


class GraphEdgeDTO(V2Model):
    """One transition record.  ``id`` is derived from artifact content, so it
    is stable across recompiles that do not change the transition, and unique
    within the artifact: ``f"{rule_id}::{source}::{outcome}"``."""

    id: str
    rule_id: str
    source: str
    source_port: str  # == outcome; the card anchors ports by this
    target: str
    outcome: str
    label: str  # presentation label; defaults to ``outcome``
    kind: EdgeKind
    reserved: bool = False
    condition: str | None = None  # rendered case condition, decision edges only


class RuleClusterDTO(V2Model):
    """One first-class rule.  A rule owns a closed subgraph — no edge in
    ``GraphEdgeDTO`` ever crosses ``rule_id``."""

    rule_id: str
    name: str
    event_type: str
    trigger_filter: dict[str, Any] | None = None
    entry_step_id: str
    step_ids: list[str] = []
    source: SourceRefDTO
    diagnostics: list[GraphDiagnosticDTO] = []


class EventGroupDTO(V2Model):
    event_type: str
    rule_ids: list[str] = []
    node_count: int = 0
    edge_count: int = 0


class GraphLegendDTO(V2Model):
    step_kinds: dict[str, str] = {}  # StepKind -> label
    edge_kinds: dict[str, str] = {}  # EdgeKind  -> label


class PlaybookV2GraphResponse(V2Model):
    """Filtering is server-side and lossless.  ``playbook_v2_graph(event_type=...)``
    narrows ``rules``/``nodes``/``edges`` to the rules triggered by that event and
    every node reachable from them.  ``event_groups`` always lists all events, so
    the selector never depends on the current filter."""

    success: bool = True
    artifact: ArtifactRefDTO
    activation: ActivationStateDTO
    purpose: str = "routine"  # "routine" | "assignment_routing"
    event_groups: list[EventGroupDTO] = []
    rules: list[RuleClusterDTO] = []
    nodes: list[GraphNodeDTO] = []
    edges: list[GraphEdgeDTO] = []
    layout: GraphLayoutDTO = GraphLayoutDTO()
    diagnostics: list[GraphDiagnosticDTO] = []
    legend: GraphLegendDTO = GraphLegendDTO()


# ---------------------------------------------------------------------------
# §4.5 Semantic diff
# ---------------------------------------------------------------------------


DiffChange = Literal["added", "removed", "modified", "unchanged"]


class FieldChangeDTO(V2Model):
    path: str  # JSON pointer within the step or rule
    before: ExplanationValueDTO | None = None
    after: ExplanationValueDTO | None = None
    executable: bool = True  # False => presentation-only (labels, help text)


class StepDiffDTO(V2Model):
    step_id: str
    rule_id: str | None = None
    change: DiffChange
    step_kind: StepKind | None = None
    title_before: str | None = None
    title_after: str | None = None
    field_changes: list[FieldChangeDTO] = []
    explanation_before: StepExplanationDTO | None = None
    explanation_after: StepExplanationDTO | None = None


class EdgeDiffDTO(V2Model):
    edge_id: str
    rule_id: str
    source: str
    target: str
    outcome: str
    change: DiffChange


class RuleDiffDTO(V2Model):
    rule_id: str
    change: DiffChange
    event_type_before: str | None = None
    event_type_after: str | None = None
    step_ids_added: list[str] = []
    step_ids_removed: list[str] = []


class ContractChangeDTO(V2Model):
    command: str
    fingerprint_before: str | None = None
    fingerprint_after: str | None = None
    change: DiffChange


class PlaybookArtifactDiffResponse(V2Model):
    """``executable_change=False`` with ``presentation_change_count>0`` is the
    spec's "a label or help-text improvement does not block activation or change
    an execution fingerprint".  The diff is computed from the two
    ``PlaybookDefinition`` objects, **not** from their JSON bytes."""

    success: bool = True
    base: ArtifactRefDTO | None = None  # None when activating the first artifact
    target: ArtifactRefDTO
    executable_change: bool = False
    semantic_change_count: int = 0
    presentation_change_count: int = 0
    rules: list[RuleDiffDTO] = []
    steps: list[StepDiffDTO] = []
    edges: list[EdgeDiffDTO] = []
    contracts: list[ContractChangeDTO] = []
    diagnostics: list[GraphDiagnosticDTO] = []
    activation_blocked: bool = False
    activation_blockers: list[str] = []


# ---------------------------------------------------------------------------
# §4.6 Pending events
# ---------------------------------------------------------------------------


PendingReason = Literal[
    "stale_contract",
    "invalid_artifact",
    "disabled",
    "unavailable",
    "question_required",
]


class PendingEventDTO(V2Model):
    pending_event_id: str
    playbook_id: str
    event_type: str
    event: dict[str, Any] = {}  # redacted projection, never the raw payload
    received_at: float
    reason: PendingReason
    attempts: int = 0
    last_error: str | None = None
    expires_at: float | None = None  # retention deadline (7 days by default)


class ListPlaybookPendingEventsResponse(V2Model):
    success: bool = True
    events: list[PendingEventDTO] = []
    count: int = 0
    oldest_received_at: float | None = None
    by_reason: dict[str, int] = {}


PendingAction = Literal["dispatch", "discard"]


class PlaybookPendingEventActionResponse(V2Model):
    success: bool = True
    action: PendingAction
    requested: int = 0
    dispatched_run_ids: list[str] = []
    discarded_ids: list[str] = []
    skipped: list[str] = []  # ids that no longer exist or already resolved
    errors: list[str] = []


# ---------------------------------------------------------------------------
# §4.7 Run overlay
# ---------------------------------------------------------------------------


RunLifecycle = Literal[
    "running",
    "paused",
    "cancelling",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
]

NodeRunState = Literal[
    "not_visited",
    "running",
    "completed",
    "failed",
    "paused",
    "cancelled",
    "timed_out",
    "skipped",
]


class TokenUsageDTO(V2Model):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False  # True when the provider reported no usage


class WaitFactsDTO(V2Model):
    wait_kind: Literal["event", "human", "task", "timer"]
    correlation_key: str
    registered_at: float
    deadline_at: float | None = None
    deadline_source: Literal["wait", "run"] | None = None
    matched_at: float | None = None
    matched_event_id: str | None = None


class CancellationFactsDTO(V2Model):
    requested_at: float
    acknowledged_at: float | None = None
    cancelled_child: bool = False


class ReceiptDTO(V2Model):
    receipt_id: str
    step_id: str
    rule_id: str
    step_kind: StepKind
    attempt: int = 1
    iteration_index: int | None = None  # set only inside a foreach body
    outcome: str
    selected_edge_id: str | None = None  # joins GraphEdgeDTO.id
    started_at: float
    completed_at: float | None = None
    duration_seconds: float | None = None
    inputs: list[ExplanationRowDTO] = []  # contract-redacted, default-deny
    result: ExplanationValueDTO | None = None
    token_usage: TokenUsageDTO | None = None
    idempotency_key: str | None = None
    principal_fingerprint: str | None = None
    profile_id: str | None = None
    contract_fingerprint: str | None = None
    error: str | None = None
    wait: WaitFactsDTO | None = None
    cancellation: CancellationFactsDTO | None = None


class LoopIterationOverlayDTO(V2Model):
    index: int
    item_display: str
    outcome: str | None = None
    receipt_ids: list[str] = []
    started_at: float | None = None
    completed_at: float | None = None


class NodeOverlayDTO(V2Model):
    step_id: str
    state: NodeRunState = "not_visited"
    visit_count: int = 0
    last_outcome: str | None = None
    receipt_ids: list[str] = []
    iterations: list[LoopIterationOverlayDTO] = []


class EdgeOverlayDTO(V2Model):
    edge_id: str
    traversal_count: int = 0
    last_traversed_at: float | None = None


class OperatorDecisionDTO(V2Model):
    """A run paused with ``operator_decision_required`` after an ambiguous
    interruption of a non-retry-safe command (design spec, run-state §)."""

    step_id: str
    attempt: int
    reason: str
    options: list[Literal["accept_outcome", "retry", "fail", "cancel"]] = []
    raised_at: float


class RunBudgetDTO(V2Model):
    llm_calls: int = 0
    total_tokens: int = 0
    max_total_tokens: int | None = None
    cost_usd: float | None = None


class PlaybookRunOverlayResponse(V2Model):
    """Carries the artifact ref of the **pinned** artifact and nothing else.
    The dashboard fetches the graph for ``overlay.artifact.artifact_sha256``,
    never for the playbook's current activation; ``artifact_is_active=False``
    renders a persistent banner.  This is the single mechanism satisfying "Run
    overlays are pinned to the exact artifact executed"."""

    success: bool = True
    run_id: str
    artifact: ArtifactRefDTO  # the run's PINNED artifact
    artifact_is_active: bool = False  # False => "this run used an older artifact"
    rule_id: str
    lifecycle: RunLifecycle
    current_step_id: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    deadline_at: float | None = None
    trigger_event: dict[str, Any] = {}  # redacted
    nodes: list[NodeOverlayDTO] = []
    edges: list[EdgeOverlayDTO] = []
    receipts: list[ReceiptDTO] = []
    bindings: list[ExplanationRowDTO] = []
    operator_decision: OperatorDecisionDTO | None = None
    budget: RunBudgetDTO | None = None
    diagnostics: list[GraphDiagnosticDTO] = []
    truncated: bool = False  # receipts capped (§5.4)
    receipt_total: int = 0


# ---------------------------------------------------------------------------
# Package 2 review-only compiler responses
# ---------------------------------------------------------------------------


class CompilerDiagnosticCountsDTO(V2Model):
    error: int = 0
    warning: int = 0
    question: int = 0
    info: int = 0


class CompilerDiagnosticDTO(V2Model):
    severity: Literal["error", "warning", "question", "info"]
    code: str
    message: str
    rule_id: str | None = None
    step_id: str | None = None
    field: str | None = None
    source: SourceRefDTO | None = None


class PlaybookV2ValidateResponse(V2Model):
    success: bool
    artifact_sha256: str | None = None
    counts: CompilerDiagnosticCountsDTO
    diagnostics: list[CompilerDiagnosticDTO] = []


class PlaybookV2ProposeResponse(V2Model):
    success: bool
    activatable: bool
    artifact_sha256: str | None = None
    source_digest: str
    contract_fingerprint: str | None = None
    compiler_build: str
    counts: CompilerDiagnosticCountsDTO
    diagnostics: list[CompilerDiagnosticDTO] = []
    semantic_diff: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None


class ShadowCompileRowDTO(V2Model):
    playbook_id: str
    vault_path: str
    kind: str
    lowered: bool
    artifact_sha256: str | None = None
    counts: CompilerDiagnosticCountsDTO
    diagnostics: list[CompilerDiagnosticDTO] = []


class ShadowSourceErrorDTO(V2Model):
    path: str
    errors: list[str] = []


class PlaybookV2ShadowCompileResponse(V2Model):
    success: bool
    total: int
    lowered: int
    clean: int
    rows: list[ShadowCompileRowDTO] = []
    source_errors: list[ShadowSourceErrorDTO] = []


# ---------------------------------------------------------------------------
# §4.8 Registration
# ---------------------------------------------------------------------------


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "playbook_v2_graph": PlaybookV2GraphResponse,
    "playbook_activation_health": PlaybookActivationHealthResponse,
    "playbook_activate": SetPlaybookActivationResponse,
    "playbook_artifact_diff": PlaybookArtifactDiffResponse,
    "playbook_pending_events": ListPlaybookPendingEventsResponse,
    "playbook_pending_event_action": PlaybookPendingEventActionResponse,
    "playbook_run_overlay": PlaybookRunOverlayResponse,
    "playbook_v2_validate": PlaybookV2ValidateResponse,
    "playbook_v2_propose": PlaybookV2ProposeResponse,
    "playbook_v2_shadow_compile": PlaybookV2ShadowCompileResponse,
}
