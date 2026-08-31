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
