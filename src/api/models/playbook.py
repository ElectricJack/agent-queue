"""Response models for playbook commands."""

from __future__ import annotations

from typing import Any

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
    total_duration_seconds: float | None = None


class ResumePlaybookResponse(BaseModel):
    resumed: str
    playbook_id: str
    status: str
    tokens_used: int = 0


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


class PlaybookGraphViewResponse(BaseModel):
    """Build-graph-view output — keeps the wire shape loose because the
    payload is consumed wholesale by the dashboard renderer."""

    success: bool = True
    playbook_id: str = ""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    direction: str = "TD"
    overlays: dict[str, Any] = {}


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


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_playbooks": ListPlaybooksResponse,
    "list_playbook_runs": ListPlaybookRunsResponse,
    "inspect_playbook_run": InspectPlaybookRunResponse,
    "resume_playbook": ResumePlaybookResponse,
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
}
