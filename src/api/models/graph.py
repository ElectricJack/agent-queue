"""Response models for the aggregate project-graph endpoint (Phase 4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GraphTaskNode(BaseModel):
    id: str
    title: str
    status: str
    priority: int = 100
    is_blocked: bool = False
    profile_id: str | None = None
    intelligence_class: str | None = None
    assigned_agent_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    playbook_run_id: str | None = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_task_id: str = Field(alias="from")
    to_task_id: str = Field(alias="to")
    dep_type: str
    description: str | None = None


class GraphGate(BaseModel):
    id: str
    gate_type: str
    status: str
    task_ids: list[str] = []


class GraphAgent(BaseModel):
    id: str
    name: str
    profile_id: str | None = None
    current_task_id: str | None = None
    session_id: str | None = None


class ProjectGraphResponse(BaseModel):
    tasks: list[GraphTaskNode] = []
    edges: list[GraphEdge] = []
    gates: list[GraphGate] = []
    agents: list[GraphAgent] = []


class GraphLayoutResponse(BaseModel):
    """Response for ``graph_layout_rebuild`` / ``graph_tidy`` (spatial-layout design §5.6, §10)."""

    success: bool
    project_id: str | None = None
    versions: dict[str, int] | None = None
    jobs: list[dict] | None = None
    request: dict | None = None
    error: str | None = None


class ReflowFailedScope(BaseModel):
    """A single failed deferred reflow request, as reported by ``graph_reflow_status``."""

    project_id: str
    variant: str
    scope_key: str
    generation: int = 1
    attempts: int = 0
    last_error: str | None = None


class GraphReflowStatus(BaseModel):
    """Payload of a ``graph_reflow_status`` success response (operator diagnostics)."""

    queued: int = 0
    running: int = 0
    failed: int = 0
    failed_scopes: list[ReflowFailedScope] = []


class GraphReflowStatusResponse(BaseModel):
    """Response for ``graph_reflow_status`` (deferred active-layout reflow diagnostics)."""

    success: bool
    project_id: str | None = None
    status: GraphReflowStatus | None = None
    error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "graph_layout_rebuild": GraphLayoutResponse,
    "graph_tidy": GraphLayoutResponse,
    "graph_reflow_status": GraphReflowStatusResponse,
}
