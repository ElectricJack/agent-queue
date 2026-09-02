"""Viewport-bounded layout endpoints (spatial-layout design §5).

Mirrors the router-factory pattern of :mod:`src.api.graph` so tests can
wire a lightweight ``db`` without booting the full daemon.

The gates loop calls ``get_gate_waiters`` once per gate.  That is the one
non-bulk call in this module and it matches what the existing
``/api/projects/{id}/graph`` endpoint already does; gates per project are
few, so it is left as-is rather than given a bulk query of its own.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.api.models.graph import GraphGate
from src.api.models.graph_layout import (
    ExtentResponse,
    LayoutEdge,
    LayoutJob,
    LayoutNode,
    LayoutStub,
    LayoutWorker,
    StubOverflow,
    TilesRequest,
    TilesResponse,
)
from src.task_graph.layout.constants import CELL_SIZE, FINISHED_STATUSES, VARIANTS
from src.task_graph.layout.view import (
    ancestors_of,
    cap_stubs,
    dock_workers,
    forced_expansion_for,
    owner_map,
    remap_edges,
    resolve_visible,
)

RECT_CAP = 64.0
EXPANDED_CAP = 2000
LIST_CAP = 200
LOCATE_CAP = 200

__all__ = ["build_graph_layout_router", "router"]


def _cells_for_rect(x0, y0, x1, y1):
    cx0, cy0 = math.floor(x0 / CELL_SIZE), math.floor(y0 / CELL_SIZE)
    cx1, cy1 = math.ceil(x1 / CELL_SIZE) - 1, math.ceil(y1 / CELL_SIZE) - 1
    return [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]


def _intersects(r, x0, y0, x1, y1) -> bool:
    return r.abs_x < x1 and r.abs_x + r.w > x0 and r.abs_y < y1 and r.abs_y + r.h > y0


def _node(row, task, kind, context_only=False) -> LayoutNode:
    return LayoutNode(
        **task,
        x=row.abs_x,
        y=row.abs_y,
        w=row.w,
        h=row.h,
        depth=row.depth,
        container_id=row.container_id,
        kind=kind,
        context_only=context_only,
        agg_children=row.agg_children,
        agg_descendants=row.agg_descendants,
        agg_completed=row.agg_completed,
        agg_running=row.agg_running,
        agg_blocked=row.agg_blocked,
        agg_active=row.agg_active,
    )


def build_graph_layout_router(*, db, command_handler=None) -> APIRouter:
    router = APIRouter()

    async def _project_or_404(project_id: str):
        if await db.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'")

    async def _meta_or_pending(project_id: str, variant: str):
        meta = await db.get_layout_meta(project_id, variant)
        if meta is None:
            await db.enqueue_layout_job(project_id, variant, "backfill")
            return None
        return meta

    def _variant(v: str) -> str:
        if v not in VARIANTS:
            raise HTTPException(status_code=400, detail=f"variant must be one of {VARIANTS}")
        return v

    def _status(v: str | None) -> str:
        """Normalize a status filter.

        Task statuses are stored upper-case; clients may send any case.
        """
        return (v or "").strip().upper()

    @router.get(
        "/api/projects/{project_id}/graph/extent",
        response_model=ExtentResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_extent(project_id: str, variant: str = "active"):
        await _project_or_404(project_id)
        variant = _variant(variant)
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        jobs = await db.list_layout_jobs(project_id, variant, statuses=("queued", "running"))
        job = LayoutJob(**jobs[0]) if jobs else None
        return ExtentResponse(
            layout_version=meta["layout_version"],
            extent_w=meta["extent_w"],
            extent_h=meta["extent_h"],
            node_count=meta["node_count"],
            job=job,
        )

    async def _resolve(project_id: str, req: TilesRequest):
        """Resolve one tiles request into a :class:`TilesResponse`.

        Returns ``None`` when the variant has no published layout yet (the
        caller answers 202 and a backfill job has been enqueued).
        """
        # Every request check runs BEFORE ``_meta_or_pending``: a malformed
        # request must answer 400 and must never enqueue a backfill job.
        variant = _variant(req.variant)
        status = _status(req.status)
        rect = req.rect
        for v in (rect.x0, rect.y0, rect.x1, rect.y1):
            if not math.isfinite(v):
                raise HTTPException(status_code=400, detail="rect must be finite")
        if rect.x0 > rect.x1 or rect.y0 > rect.y1:
            raise HTTPException(status_code=400, detail="rect must be ordered")
        if req.root is None and (rect.x1 - rect.x0 > RECT_CAP or rect.y1 - rect.y0 > RECT_CAP):
            raise HTTPException(status_code=400, detail=f"rect larger than {RECT_CAP} units")
        if len(req.expanded) > EXPANDED_CAP:
            raise HTTPException(status_code=400, detail=f"expanded exceeds {EXPANDED_CAP}")
        if req.root is not None or status in FINISHED_STATUSES:
            variant = "all"
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return None

        # Candidate rows: everything in the rect's cells (or the whole subtree in focus).
        if req.root is not None:
            root_rows = await db.load_layout_rows(project_id, variant, [req.root])
            if req.root not in root_rows:
                raise HTTPException(status_code=404, detail=f"No layout node '{req.root}'")
            cand = await db.load_rows_by_prefixes(project_id, variant, [root_rows[req.root].path])
        else:
            # Every row in the rect's cells, NOT only the rows that intersect
            # the rect: the rect cull happens after visibility is resolved
            # (below), so that a collapsed container just off-screen is still
            # resolved as collapsed and can own the edges into its subtree.
            cand = await db.load_rows_in_cells(
                project_id, variant, _cells_for_rect(rect.x0, rect.y0, rect.x1, rect.y1)
            )
        # Ancestors of candidates are needed to decide visibility.
        anc_ids = {a for r in cand.values() for a in ancestors_of(r.path)} - set(cand)
        if anc_ids:
            cand.update(await db.load_layout_rows(project_id, variant, list(anc_ids)))

        # Filtering: matches anywhere force their ancestors open; non-matches vanish.
        matches: set[str] | None = None
        forced: set[str] = set()
        if req.q.strip() or status:
            matches = await db.load_matching_ids(
                project_id, variant, q=req.q.strip(), status=status
            )
            match_rows = await db.load_layout_rows(project_id, variant, list(matches))
            forced = forced_expansion_for(matches, match_rows)
            if forced - set(cand):
                cand.update(
                    await db.load_layout_rows(project_id, variant, list(forced - set(cand)))
                )

        # Focus mode shows the whole subtree at the client's expanded state:
        # ``max_depth`` is ignored under ``root``, the same way the rect cap is.
        vis = resolve_visible(
            cand,
            expanded=set(req.expanded),
            max_depth=None if req.root is not None else req.max_depth,
            root=req.root,
            forced_expanded=forced,
        )
        # The collapsed set as RESOLVED, before any culling.  An edge into a
        # collapsed subtree that the rect (or the filter) then removes must
        # still remap onto its container -- which surfaces as a stub carrying
        # the container's title -- instead of exposing the inner task.
        collapsed_resolved = dict(vis.collapsed_paths)
        # rect membership again, now over resolved rows (ancestors may lie outside).
        if req.root is None:
            for tid in list(vis.visible):
                r = cand[tid]
                if not _intersects(r, rect.x0, rect.y0, rect.x1, rect.y1):
                    del vis.visible[tid]
                    vis.collapsed_paths.pop(tid, None)
        context_only: set[str] = set()
        if matches is not None:
            for tid in list(vis.visible):
                if tid in matches:
                    continue
                if tid in forced:
                    context_only.add(tid)
                else:
                    del vis.visible[tid]
                    vis.collapsed_paths.pop(tid, None)

        # Edges: touching visible ids or anything inside a visible collapsed subtree.
        hidden_rows = await db.load_rows_by_prefixes(
            project_id, variant, list(collapsed_resolved.values())
        )
        hidden_owner = owner_map(hidden_rows, collapsed_resolved)
        touching = set(vis.visible) | set(hidden_owner)
        raw_edges = await db.load_edges_touching(touching)
        wire, _orphans = remap_edges(raw_edges, vis.visible, hidden_owner)
        # Stub candidates are every wire endpoint that is not visible: plain
        # orphans, plus containers an edge was remapped onto that the rect or
        # the filter then culled away.
        far_ids = {x for e in wire for x in (e["from"], e["to"])} - set(vis.visible)
        stub_rows = await db.load_layout_rows(project_id, variant, list(far_ids))
        kept, stubs, more = cap_stubs(wire, stub_rows, set(vis.visible))
        stub_titles = await db.load_rows_with_tasks(project_id, variant, [s["id"] for s in stubs])
        stubs_out = [
            LayoutStub(
                project_id=project_id,
                title=stub_titles[s["id"]][1]["title"] if s["id"] in stub_titles else "",
                **s,
            )
            for s in stubs
        ]

        # Workers and gates.
        agents = [
            {"id": a.id, "name": a.name, "current_task_id": a.current_task_id}
            for a in await db.list_agents()
        ]
        docked = dock_workers(agents, set(vis.visible), hidden_owner)
        workers = [
            LayoutWorker(
                agent_id=d["agent"]["id"],
                name=d["agent"]["name"],
                docked_at=d["docked_at"],
                in_collapsed=d["in_collapsed"],
            )
            for d in docked
        ]
        gates_out: list[GraphGate] = []
        for g in await db.list_gates(project_id=project_id):
            waiters = await db.get_gate_waiters(g["id"])
            ids = sorted(w for w in waiters if w in vis.visible)
            if ids:
                gates_out.append(
                    GraphGate(
                        id=g["id"], gate_type=g["gate_type"], status=g["status"], task_ids=ids
                    )
                )

        with_tasks = await db.load_rows_with_tasks(project_id, variant, list(vis.visible))
        nodes = [
            _node(with_tasks[t][0], with_tasks[t][1], kind, t in context_only)
            for t, kind in vis.visible.items()
            if t in with_tasks
        ]
        nodes.sort(key=lambda n: (n.depth, n.y, n.x))
        edges = [LayoutEdge(**e) for e in kept]
        return TilesResponse(
            nodes=nodes,
            edges=edges,
            stubs=stubs_out,
            stub_overflow=[StubOverflow(**m) for m in more],
            workers=workers,
            gates=gates_out,
            layout_version=meta["layout_version"],
        )

    @router.post(
        "/api/projects/{project_id}/graph/tiles",
        response_model=TilesResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_tiles(project_id: str, req: TilesRequest):
        await _project_or_404(project_id)
        res = await _resolve(project_id, req)
        if res is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        return res

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db.

    Same shape as :func:`src.api.graph._build_default_router`: the routes
    are declared statically (so OpenAPI can introspect them) and delegate
    to a factory-built router bound to ``deps._orchestrator`` at request
    time.
    """
    from src.api import dependencies as deps

    router = APIRouter()

    def _inner() -> APIRouter:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        return build_graph_layout_router(db=orch.db, command_handler=orch.command_handler)

    async def _call(path: str, method: str, **kwargs):
        for route in _inner().routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
                return await route.endpoint(**kwargs)
        raise HTTPException(status_code=500, detail="graph layout router misconfigured")

    @router.get(
        "/api/projects/{project_id}/graph/extent",
        response_model=ExtentResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_extent(project_id: str, variant: str = "active"):
        return await _call(
            "/api/projects/{project_id}/graph/extent",
            "GET",
            project_id=project_id,
            variant=variant,
        )

    @router.post(
        "/api/projects/{project_id}/graph/tiles",
        response_model=TilesResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_tiles(project_id: str, req: TilesRequest):
        return await _call(
            "/api/projects/{project_id}/graph/tiles", "POST", project_id=project_id, req=req
        )

    return router


router = _build_default_router()
