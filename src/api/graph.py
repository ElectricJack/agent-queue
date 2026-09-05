"""Aggregate project-graph endpoint (Phase 4 — Command Center canvas).

Mirrors the router-factory pattern of :mod:`src.api.sessions` so tests can
wire a lightweight ``db`` without booting the full daemon.

Deviation from the plan's draft: ``Database.get_all_dependencies`` returns
``dict[str, set[str]]`` (task_id -> depended-on task ids) with no
``dep_type`` in the payload — it collapses typed edges into a plain
readiness graph. To surface the typed ``dep_type`` the graph payload
requires, this router instead reads edges with ``list_project_edges``,
which returns every outgoing edge for tasks in the project in a single
statement (``task_id``, ``depends_on_task_id``, ``dep_type``,
``description``) — same semantics the plan described, different helper,
and no longer one statement per task.
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

        rows = await db.list_graph_task_rows(project_id)
        task_ids = {r["id"] for r in rows}

        # One statement for every outgoing edge of every task in the project
        # (cross-project edges land client-side when the peer project's graph
        # is also loaded, spec §9.2) — was one statement per task.
        edges = [
            GraphEdge(
                from_task_id=e["task_id"],
                to_task_id=e["depends_on_task_id"],
                dep_type=e["dep_type"],
                description=e["description"],
            )
            for e in await db.list_project_edges(project_id)
        ]

        waiters_by_gate = await db.list_gate_waiters_for_project(project_id)
        gates = [
            GraphGate(
                id=g["id"],
                gate_type=g["gate_type"],
                status=g["status"],
                task_ids=waiters_by_gate.get(g["id"], []),
            )
            for g in await db.list_gates(project_id=project_id)
        ]

        agents = [
            GraphAgent(
                id=a.id,
                name=a.name,
                profile_id=a.profile_id,
                current_task_id=a.current_task_id,
                session_id=getattr(a, "session_id", None),
            )
            for a in await db.list_agents()
            if a.current_task_id in task_ids
        ]

        def _run_id(dedup_key: str | None) -> str | None:
            if dedup_key and dedup_key.startswith("playbook-run:"):
                return dedup_key.removeprefix("playbook-run:")
            return None

        return ProjectGraphResponse(
            tasks=[
                GraphTaskNode(
                    id=r["id"],
                    title=r["title"],
                    status=r["status"],
                    priority=r["priority"],
                    is_blocked=r["is_blocked"],
                    profile_id=r["profile_id"],
                    intelligence_class=r["intelligence_class"],
                    assigned_agent_id=r["assigned_agent_id"],
                    branch_name=r["branch_name"],
                    pr_url=r["pr_url"],
                    playbook_run_id=_run_id(r["dedup_key"]),
                )
                for r in rows
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
