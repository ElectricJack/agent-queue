"""Response models for playbook commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class PlaybookLastRun(BaseModel):
    run_id: str
    status: str
    started_at: float | None = None
    completed_at: float | None = None
    tokens_used: int = 0


class PlaybookSummary(BaseModel):
    id: str
    scope: str
    triggers: list[str] = []
    version: int = 0
    compiled_at: str | None = None  # ISO 8601 timestamp
    node_count: int = 0
    status: str = "active"
    running_count: int = 0
    scope_identifier: str | None = None
    agent_type: str | None = None
    cooldown_seconds: int | None = None
    cooldown_remaining: float | None = None
    max_tokens: int | None = None
    enabled: bool = True
    last_run: PlaybookLastRun | None = None


class ListPlaybooksResponse(BaseModel):
    playbooks: list[PlaybookSummary] = []
    count: int = 0


class PlaybookRunPathEntry(BaseModel):
    node_id: str
    status: str


class PlaybookRunSummary(BaseModel):
    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: str | None = None
    tokens_used: int = 0
    started_at: float | None = None
    completed_at: float | None = None
    path: list[PlaybookRunPathEntry] = []
    duration_seconds: float | None = None
    error: str | None = None


class ListPlaybookRunsResponse(BaseModel):
    runs: list[PlaybookRunSummary] = []
    count: int = 0


class InspectPlaybookRunResponse(BaseModel):
    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    tokens_used: int = 0
    node_trace: list[dict[str, Any]] = []
    node_count: int = 0
    conversation_history: list[dict[str, Any]] = []
    message_count: int = 0
    trigger_event: dict[str, Any] = {}
    error: str | None = None
    paused_at: float | None = None
    waiting_for_event: str | None = None
    total_duration_seconds: float | None = None
    graph: dict[str, Any] | None = None


class ResumePlaybookResponse(BaseModel):
    resumed: str
    playbook_id: str
    status: str
    tokens_used: int = 0


class CancelPlaybookRunResponse(BaseModel):
    cancelled: str
    playbook_id: str
    status: str


class RecoverWorkflowResponse(BaseModel):
    """Shape mirrors OrphanWorkflowRecovery.recover_workflow output."""

    success: bool = False
    workflow_id: str = ""
    action: str | None = None
    message: str | None = None
    error: str | None = None


class CompilePlaybookResponse(BaseModel):
    compiled: bool = False
    playbook_id: str = ""
    version: int = 0
    source_hash: str = ""
    skipped: bool = False
    retries_used: int = 0
    node_count: int | None = None
    triggers: list[str] | None = None
    scope: str | None = None
    errors: list[str] | None = None


class ShowPlaybookGraphResponse(BaseModel):
    playbook_id: str
    format: str
    graph: str
    node_count: int = 0
    version: int = 0


class RunPlaybookResponse(BaseModel):
    run_id: str
    playbook_id: str
    version: int = 0
    status: str
    tokens_used: int = 0
    node_count: int = 0
    node_trace: list[dict[str, Any]] = []
    error: str | None = None
    final_response: str | None = None


class DryRunPlaybookResponse(BaseModel):
    dry_run: bool = True
    playbook_id: str
    version: int = 0
    status: str
    node_trace: list[dict[str, Any]] = []
    node_count: int = 0
    tokens_used: int = 0
    mock_event: dict[str, Any] = {}


class PlaybookHealthResponse(BaseModel):
    """Loose shape — compute_playbook_health returns a rich dynamic dict."""

    playbook_id: str | None = None
    run_count: int = 0
    success_rate: float = 0.0
    avg_tokens: float = 0.0
    avg_duration_seconds: float = 0.0
    metrics: dict[str, Any] = {}


class PlaybookGraphTrigger(BaseModel):
    """One compiled trigger on the visualized playbook."""

    event_type: str
    filter: dict[str, Any] | None = None


class PlaybookGraphIdentity(BaseModel):
    """Identity block of the graph view — the compiled playbook itself."""

    id: str
    version: int = 0
    scope: str = ""
    triggers: list[PlaybookGraphTrigger] = []
    node_count: int = 0
    compiled_at: str | None = None


class PlaybookGraphPosition(BaseModel):
    """A stable grid coordinate produced by the backend layout."""

    x: int = 0
    y: int = 0


class PlaybookGraphNodeColors(BaseModel):
    fill: str
    stroke: str
    text: str


class PlaybookTransitionDetail(BaseModel):
    """One compiled transition as serialized by ``PlaybookTransition.to_dict``."""

    goto: str
    #: Natural-language condition (str) or structured check (dict).
    when: str | dict[str, Any] | None = None
    otherwise: bool | None = None


class PlaybookNodeLlmConfig(BaseModel):
    """``LlmConfig.to_dict()`` — every field omitted when unset."""

    provider: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class CompiledPlaybookNode(BaseModel):
    """The serializable fields produced by ``PlaybookNode.to_dict()``.

    Each field is optional according to the compiled-node rules: a key is
    present only when the compiler set it.  This is what the dashboard node
    inspector renders, so the prompt here is the full untruncated text.
    """

    prompt: str | None = None
    entry: bool | None = None
    terminal: bool | None = None
    transitions: list[PlaybookTransitionDetail] | None = None
    goto: str | None = None
    wait_for_human: bool | None = None
    timeout_seconds: int | None = None
    pause_timeout_seconds: int | None = None
    on_timeout: str | None = None
    llm_config: PlaybookNodeLlmConfig | None = None
    transition_llm_config: PlaybookNodeLlmConfig | None = None
    for_each: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class PlaybookGraphNode(BaseModel):
    """One positioned, classified node in the compiled graph."""

    id: str
    #: entry | entry+decision | terminal | checkpoint | decision | action
    type: str
    symbol: str = ""
    label: str = ""
    position: PlaybookGraphPosition = PlaybookGraphPosition()
    colors: PlaybookGraphNodeColors
    entry: bool = False
    terminal: bool = False
    wait_for_human: bool = False
    prompt_preview: str | None = None
    timeout_seconds: int | None = None
    on_timeout: str | None = None
    out_degree: int = 0
    details: CompiledPlaybookNode


class PlaybookGraphEdge(BaseModel):
    """One directed, labelled edge between two compiled nodes."""

    source: str
    target: str
    label: str = ""
    edge_type: Literal[
        "goto", "condition", "otherwise", "timeout", "success", "failure"
    ]


class PlaybookGraphNodesEdges(BaseModel):
    nodes: list[PlaybookGraphNode] = []
    edges: list[PlaybookGraphEdge] = []


class PlaybookGraphLayout(BaseModel):
    #: "TD" (top-down) or "LR" (left-right).
    direction: str = "TD"
    grid_positions: dict[str, PlaybookGraphPosition] = {}


class PlaybookGraphViewResponse(BaseModel):
    """``build_graph_view`` output — the nested shape the builder actually
    produces (design spec §4).

    The overlay blocks (``live_state``, ``run_overlay``, ``run_history``,
    ``node_metrics``) stay loosely typed: they are opt-in, richly dynamic,
    and not part of the first Graph tab.  They are declared here so the
    response model never silently drops them.
    """

    success: bool = True
    playbook: PlaybookGraphIdentity
    graph: PlaybookGraphNodesEdges = PlaybookGraphNodesEdges()
    layout: PlaybookGraphLayout = PlaybookGraphLayout()
    legend: dict[str, Any] = {}
    live_state: dict[str, Any] | None = None
    run_overlay: dict[str, Any] | None = None
    run_history: list[dict[str, Any]] | None = None
    node_metrics: dict[str, Any] | None = None


class GetPlaybookSourceResponse(BaseModel):
    playbook_id: str
    path: str
    markdown: str
    source_hash: str


class UpdatePlaybookSourceResponse(BaseModel):
    playbook_id: str
    source_hash: str
    compiled: bool = False
    version: int | None = None
    node_count: int | None = None
    scope: str | None = None
    triggers: list[str] | None = None
    errors: list[str] | None = None
    retries_used: int | None = None
    # Conflict response (HTTP 409 surfaced inline)
    error: str | None = None
    reason: str | None = None
    current_source_hash: str | None = None
    expected_source_hash: str | None = None


class CreatePlaybookResponse(BaseModel):
    created: bool = True
    playbook_id: str
    path: str
    source_hash: str


class DeletePlaybookResponse(BaseModel):
    deleted: bool = True
    playbook_id: str
    archived_path: str | None = None
    removed_from_registry: bool = False


class SetPlaybookEnabledResponse(BaseModel):
    playbook_id: str
    enabled: bool
    compiled: bool = False
    noop: bool = False
    source_hash: str | None = None
    errors: list[str] | None = None


class PlaybookValidationError(BaseModel):
    """One structured validation failure.

    ``node`` is the compiled node the error belongs to, or None for a
    whole-file problem (missing frontmatter, path outside the vault).
    """

    node: str | None = None
    field: str | None = None
    message: str = ""


class PlaybookValidateResponse(BaseModel):
    success: bool = True
    errors: list[PlaybookValidationError] = []
    #: True for a ``.md`` source: frontmatter checked, compile still owed.
    requires_compile: bool = False


class PlaybookInstallResponse(BaseModel):
    success: bool = True
    errors: list[PlaybookValidationError] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_playbooks": ListPlaybooksResponse,
    "playbook_validate": PlaybookValidateResponse,
    "playbook_install": PlaybookInstallResponse,
    "list_playbook_runs": ListPlaybookRunsResponse,
    "inspect_playbook_run": InspectPlaybookRunResponse,
    "resume_playbook": ResumePlaybookResponse,
    "cancel_playbook_run": CancelPlaybookRunResponse,
    "recover_workflow": RecoverWorkflowResponse,
    "compile_playbook": CompilePlaybookResponse,
    "show_playbook_graph": ShowPlaybookGraphResponse,
    "run_playbook": RunPlaybookResponse,
    "dry_run_playbook": DryRunPlaybookResponse,
    "playbook_health": PlaybookHealthResponse,
    "playbook_graph_view": PlaybookGraphViewResponse,
    "get_playbook_source": GetPlaybookSourceResponse,
    "update_playbook_source": UpdatePlaybookSourceResponse,
    "create_playbook": CreatePlaybookResponse,
    "delete_playbook": DeletePlaybookResponse,
    "set_playbook_enabled": SetPlaybookEnabledResponse,
}
