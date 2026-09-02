"""``GET /api/metrics/series`` — history for the dashboard Metrics tab.

The tab loads history once per range change and then follows the
``metrics.tick`` WebSocket frames, so this route is a cold-start and zoom
path, not a poll.  It mirrors the router-factory shape of :mod:`src.api.graph`
and :mod:`src.api.routers.proposals` so a test can wire a bare ``db`` without
booting the daemon.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from src.api.models.metrics import MetricsSample, MetricsSeriesResponse
from src.metrics.sampler import RESOLUTIONS

__all__ = ["build_metrics_router", "choose_step", "router"]

#: Most points one response will return.  A 7-day span at 1-minute steps is
#: ~10k points, which uPlot draws without complaint; beyond that the answer
#: is a coarser step, not a bigger payload.
MAX_POINTS = 12_000

#: Resolutions coarsest-last — ``choose_step`` walks this order.
_ORDER = ("1s", "1m", "1h")


def choose_step(from_ts: float, to_ts: float, requested: str) -> tuple[str, bool]:
    """Pick the resolution to serve, and say whether it was downgraded.

    ``auto`` (the default) picks the finest tier whose point count fits
    :data:`MAX_POINTS`.  An explicit step is honoured unless it would blow
    that budget, in which case it is coarsened and ``truncated`` is set —
    silently returning a truncated window would make a chart lie about the
    range it is labelled with.
    """
    span = max(0.0, to_ts - from_ts)
    candidates = _ORDER if requested == "auto" else _ORDER[_ORDER.index(requested) :]
    for step in candidates:
        if span / RESOLUTIONS[step] <= MAX_POINTS:
            return step, step != requested and requested != "auto"
    return _ORDER[-1], requested != _ORDER[-1]


async def _series(db, from_ts: float | None, to_ts: float | None, step: str):
    if step not in ("auto", *_ORDER):
        raise HTTPException(
            status_code=422, detail=f"step must be auto, 1s, 1m or 1h (got {step!r})"
        )
    now = time.time()
    to_value = float(to_ts) if to_ts is not None else now
    # Default window is the finest tier's own retention: the range the
    # per-second series can actually answer for.
    from_value = float(from_ts) if from_ts is not None else to_value - 3600.0
    if from_value > to_value:
        raise HTTPException(status_code=422, detail="from must be <= to")

    resolution, truncated = choose_step(from_value, to_value, step)
    rows = await db.read_metrics_samples(
        resolution, from_value, to_value, limit=MAX_POINTS
    )
    return MetricsSeriesResponse(
        step=resolution,
        from_ts=from_value,
        to_ts=to_value,
        truncated=truncated or len(rows) >= MAX_POINTS,
        samples=[MetricsSample.model_validate(row) for row in rows],
    )


def build_metrics_router(*, db) -> APIRouter:
    """Router bound to an explicit ``db`` — the seam tests use."""
    router = APIRouter()

    @router.get("/api/metrics/series", response_model=MetricsSeriesResponse)
    async def get_metrics_series(
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        step: str = Query("auto"),
    ) -> MetricsSeriesResponse:
        return await _series(db, from_ts, to_ts, step)

    return router


def _build_default_router() -> APIRouter:
    """Registered in ``create_app`` — resolves the shared db per request."""
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/metrics/series", response_model=MetricsSeriesResponse)
    async def get_metrics_series(
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        step: str = Query("auto"),
    ) -> MetricsSeriesResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        return await _series(orch.db, from_ts, to_ts, step)

    return router


router = _build_default_router()
