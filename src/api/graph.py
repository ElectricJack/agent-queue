"""Aggregate project-graph endpoint (Phase 4 — Command Center canvas).

Mirrors the router-factory pattern of :mod:`src.api.sessions` so tests can
wire a lightweight ``db`` without booting the full daemon.

Deviation from the plan's draft: ``Database.get_all_dependencies`` returns
``dict[str, set[str]]`` (task_id -> depended-on task ids) with no
``dep_type`` in the payload — it collapses typed edges into a plain
readiness graph. To surface the typed ``dep_type`` the graph payload
requires, this router instead calls ``get_typed_dependencies(task_id)``
per task in the project (returns ``[(depends_on_task_id, dep_type), ...]``
for that task's *outgoing* edges) and keeps edges whose "from" task is in
this project — same semantics the plan described, different helper.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.models.graph import (
    GraphAgent,
    GraphEdge,
    GraphGate,
    GraphTaskNode,
    ProjectGraphResponse,
)

__all__ = ["build_graph_router", "router"]


def build_graph_router(*, db) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/projects/{project_id}/graph",
        response_model=ProjectGraphResponse,
    )
    async def get_project_graph(project_id: str) -> ProjectGraphResponse:
        project = await db.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'")

        tasks = await db.list_tasks(project_id=project_id)
        task_ids = {t.id for t in tasks}

        # Edges — for each task in this project, pull its typed outgoing
        # edges. Cross-project edges land on the client side only when the
        # peer project's graph is also loaded (spec §9.2).
        edges: list[GraphEdge] = []
        for t in tasks:
            for edge in await db.get_typed_dependencies_detailed(t.id):
                edges.append(GraphEdge(
                    from_task_id=t.id,
                    to_task_id=edge["depends_on_task_id"],
                    dep_type=edge["dep_type"],
                    description=edge["description"],
                ))

        gate_rows = await db.list_gates(project_id=project_id)
        gates: list[GraphGate] = []
        for g in gate_rows:
            waiters = await db.get_gate_waiters(g["id"])
            gates.append(GraphGate(
                id=g["id"],
                gate_type=g["gate_type"],
                status=g["status"],
                task_ids=sorted(waiters),
            ))

        # Agents currently assigned to a task in this project.
        all_agents = await db.list_agents()
        agents = [
            GraphAgent(
                id=a.id,
                name=a.name,
                profile_id=a.profile_id,
                current_task_id=a.current_task_id,
                session_id=getattr(a, "session_id", None),
            )
            for a in all_agents
            if a.current_task_id in task_ids
        ]

        return ProjectGraphResponse(
            tasks=[
                GraphTaskNode(
                    id=t.id,
                    title=t.title,
                    status=t.status.value if hasattr(t.status, "value") else str(t.status),
                    priority=t.priority,
                    is_blocked=t.is_blocked,
                    profile_id=t.profile_id,
                    intelligence_class=getattr(t, "intelligence_class", None),
                    assigned_agent_id=t.assigned_agent_id,
                    branch_name=t.branch_name,
                    pr_url=t.pr_url,
                    playbook_run_id=(
                        t.dedup_key.removeprefix("playbook-run:")
                        if t.dedup_key and t.dedup_key.startswith("playbook-run:")
                        else None
                    ),
                )
                for t in tasks
            ],
            edges=edges,
            gates=gates,
            agents=agents,
        )

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db.

    Closes directly over ``deps._orchestrator.db`` at request time. The
    original wrapper re-built a fresh ``APIRouter`` per request and used a
    ``Request`` parameter, which broke OpenAPI schema generation because
    Pydantic could not resolve the ``ForwardRef``. This shape mirrors
    ``src/api/task_files.py`` and has no such introspection footprint.
    """
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get(
        "/api/projects/{project_id}/graph",
        response_model=ProjectGraphResponse,
    )
    async def get_project_graph(project_id: str) -> ProjectGraphResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        # Delegate to the router-factory implementation for behaviour parity.
        inner = build_graph_router(db=orch.db)
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/projects/{project_id}/graph":
                return await route.endpoint(project_id=project_id)
        raise HTTPException(status_code=500, detail="graph router misconfigured")

    return router


router = _build_default_router()
