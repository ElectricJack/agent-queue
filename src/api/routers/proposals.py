"""GET /api/proposals/{id} — ghost-overlay read for the Command Center.

Phase 6 Task 4 (design §8, spec ingestion).  Mirrors the router-factory
pattern of :mod:`src.api.graph` so tests can wire a lightweight ``db``
without booting the full daemon.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.database.queries import proposal_queries

__all__ = ["build_proposals_router", "router"]


def build_proposals_router(*, db) -> APIRouter:
    router = APIRouter()

    @router.get("/api/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str) -> dict:
        row = await proposal_queries.get_proposal(db, proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {
            "proposal_id": row["id"],
            "project_id": row["project_id"],
            "source": row["source"],
            "tasks": row["payload"].get("tasks", []),
            "edges": row["payload"].get("edges", []),
            "status": row["status"],
        }

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db.

    Closes directly over ``deps._orchestrator.db`` at request time
    instead of rebuilding a fresh inner ``APIRouter`` per call. The prior
    per-request build was fine functionally but did unnecessary work on
    every hit and mirrored a shape ``graph.py`` had already moved off.
    """
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str) -> dict:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        row = await proposal_queries.get_proposal(orch.db, proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {
            "proposal_id": row["id"],
            "project_id": row["project_id"],
            "source": row["source"],
            "tasks": row["payload"].get("tasks", []),
            "edges": row["payload"].get("edges", []),
            "status": row["status"],
        }

    return router


router = _build_default_router()
