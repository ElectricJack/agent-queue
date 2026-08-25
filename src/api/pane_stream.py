"""SSE pane stream — ``GET /api/sessions/{session_id}/pane``.

Live ``capture-pane`` screens for one session, served from the shared
:class:`~src.sessions.pane_broadcaster.PaneBroadcaster` so N viewers of one
session still cost one poll loop.

Deliberately separate from ``/api/sessions/{id}/stream`` (transcript): the
lifecycles differ — a broadcaster-backed fan-out versus a per-connection
file tail — and keeping the transcript endpoint untouched means it cannot
regress behind this feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.sessions.pane_broadcaster import PaneBroadcaster, PaneStreamRefused
from src.sessions.provider import CapabilityUnsupported

logger = logging.getLogger(__name__)

__all__ = ["build_pane_router", "router", "get_broadcaster", "shutdown_broadcaster"]

_HEARTBEAT_SECONDS = 15.0


def _sse(frame: dict) -> bytes:
    return f"data: {json.dumps(frame)}\n\n".encode()


def build_pane_router(*, db, broadcaster: PaneBroadcaster) -> APIRouter:
    """Router factory so tests wire a lightweight db + FakeProvider."""

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/pane")
    async def pane(session_id: str, request: Request,
                    max_seconds: float | None = None) -> StreamingResponse:
        session = await db.get_session(session_id)
        if session is None:
            session = await db.get_session_by_name(session_id)
        if session is None:
            raise HTTPException(status_code=404,
                                 detail=f"No session '{session_id}'")
        try:
            queue = await broadcaster.subscribe(session)
        except CapabilityUnsupported as exc:
            raise HTTPException(
                status_code=409,
                detail=f"provider '{exc.provider}' cannot peek; no pane stream",
            ) from exc
        except PaneStreamRefused as exc:
            raise HTTPException(status_code=429, detail=exc.message) from exc

        started_at = time.monotonic()

        async def gen():
            last_beat = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if max_seconds is not None and \
                            time.monotonic() - started_at > max_seconds:
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except (TimeoutError, asyncio.TimeoutError):
                        frame = None
                    if frame is not None:
                        yield _sse(frame)
                        last_beat = time.monotonic()
                        if frame.get("type") in ("stopped", "error"):
                            return
                        continue
                    now = time.monotonic()
                    if now - last_beat >= _HEARTBEAT_SECONDS:
                        yield b": heartbeat\n\n"
                        last_beat = now
            finally:
                with contextlib.suppress(Exception):
                    await broadcaster.unsubscribe(session.name, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


# -- default wiring --------------------------------------------------------

_broadcaster: PaneBroadcaster | None = None


def get_broadcaster(orch) -> PaneBroadcaster:
    """Process-wide broadcaster, built on first use from the orchestrator."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = PaneBroadcaster(
            orch.session_providers, getattr(orch, "config", None)
        )
    return _broadcaster


async def shutdown_broadcaster() -> None:
    """Cancel every poll loop.  Registered on FastAPI shutdown."""
    global _broadcaster
    if _broadcaster is not None:
        await _broadcaster.shutdown()
        _broadcaster = None


def _build_default_router() -> APIRouter:
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/pane")
    async def pane(session_id: str, request: Request,
                    max_seconds: float | None = None) -> StreamingResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        inner = build_pane_router(db=orch.db, broadcaster=get_broadcaster(orch))
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/sessions/{session_id}/pane":
                return await route.endpoint(
                    session_id=session_id, request=request,
                    max_seconds=max_seconds,
                )
        raise HTTPException(status_code=500, detail="pane router misconfigured")

    return router


router = _build_default_router()
