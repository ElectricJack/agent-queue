"""SSE session stream endpoint (S3, Task B3).

``GET /api/sessions/{session_id}/stream`` returns Server-Sent Events:

1. Replay of the transcript's normalized history — one ``data:`` frame per
   :class:`~src.sessions.transcripts.base.TranscriptEntry`, in file order.
2. Live tail — the endpoint polls the transcript at
   ``sessions.transcript_poll_seconds`` cadence and emits each new entry.
3. Heartbeat comments (``: heartbeat\\n\\n``) every 15 s so intermediaries
   do not close the connection during a quiet turn.

When the session's harness has no transcript reader, or the transcript
file cannot be resolved, the endpoint emits a peek-diff fallback frame
(``source: "peek"``) rather than 404-ing — the fallback is honest about
what it is showing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.sessions.provider import Cap as _Cap
from src.sessions.transcripts import resolve_reader
from src.sessions.transcripts.base import TranscriptEntry

logger = logging.getLogger(__name__)

__all__ = ["build_sessions_router"]


_DEFAULT_HEARTBEAT_SECONDS = 15.0
_DEFAULT_POLL_INTERVAL = 2.0


def _entry_to_frame(entry: TranscriptEntry, *, source: str = "transcript") -> bytes:
    """Encode one ``TranscriptEntry`` as one SSE ``data:`` frame."""
    payload = {
        "source": source,
        "uuid": entry.uuid,
        "parent_uuid": entry.parent_uuid,
        "type": entry.type,
        "text": entry.text,
        "model": entry.model,
        "usage": entry.usage,
        "ts": entry.ts,
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _peek_frame(text: str) -> bytes:
    payload = {"source": "peek", "text": text, "ts": time.time()}
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _best_effort_peek(
    session, session_providers, config
) -> str | None:
    """Ask the session provider for a peek snapshot; ``None`` on any failure.

    Kept best-effort: SSE's peek fallback is a nicety, not a contract, and
    a raise here would drop the whole stream connection.
    """
    if session_providers is None:
        return None
    try:
        provider = session_providers.create(session.provider, config)
    except Exception:
        return None
    if provider is None:
        return None
    try:
        if not provider.supports(_Cap.PEEK):
            return None
    except Exception:
        return None
    try:
        from src.sessions.provider import SessionHandle
        handle = SessionHandle(
            name=session.name,
            provider=session.provider,
            instance_token=session.instance_token,
        )
        return await provider.peek(handle, 60)
    except Exception:
        return None


def build_sessions_router(
    *,
    db,
    base_dir: Path | None = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    heartbeat_interval: float = _DEFAULT_HEARTBEAT_SECONDS,
    session_providers=None,
    config=None,
) -> APIRouter:
    """Router factory so tests can wire a lightweight db without the daemon."""

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request,
                      max_seconds: float | None = None,
                      replay_only: int = 0) -> StreamingResponse:
        session = await db.get_session(session_id)
        if session is None:
            session = await db.get_session_by_name(session_id)
        if session is None:
            raise HTTPException(status_code=404,
                                 detail=f"No session '{session_id}'")

        reader = resolve_reader(session.harness, base_dir=base_dir)
        started_at = time.monotonic()

        async def gen():
            # ---------- replay ----------
            offset = 0
            path = None
            if reader is not None:
                path = reader.resolve_path(session.work_dir, session.session_key)
            if reader is None or path is None:
                # Peek-diff fallback: no transcript yet.  Prefer real pane
                # peek from the session provider; if that is unavailable
                # or fails, keep the honest "no transcript" placeholder so
                # the client sees *something* labelled ``source: "peek"``.
                peek_text = None
                if reader is not None:
                    peek_text = await _best_effort_peek(
                        session, session_providers, config
                    )
                if peek_text:
                    yield _peek_frame(peek_text)
                else:
                    yield _peek_frame(
                        f"(no transcript available for session {session_id}; "
                        "falling back to peek)"
                    )
                if replay_only:
                    return
            else:
                entries, offset = await reader.read_new(path, 0)
                for e in entries:
                    yield _entry_to_frame(e)

            if replay_only:
                return

            # ---------- tail ----------
            last_heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                if max_seconds is not None and \
                        time.monotonic() - started_at > max_seconds:
                    return

                # Re-resolve the path each iteration while we do not yet
                # have one — the transcript file often appears seconds
                # after the harness boots.  Without this, connecting
                # before the JSONL exists would spin forever emitting
                # nothing.  Once resolved, replay from offset 0, then
                # continue tailing at the returned offset.
                if reader is not None and path is None:
                    path = reader.resolve_path(
                        session.work_dir, session.session_key
                    )
                    if path is not None:
                        try:
                            entries, offset = await reader.read_new(path, 0)
                        except Exception:
                            logger.debug(
                                "sse late-resolve replay failed for %s",
                                session_id, exc_info=True,
                            )
                            entries = []
                        for e in entries:
                            yield _entry_to_frame(e)
                            last_heartbeat = time.monotonic()

                if reader is not None and path is not None:
                    try:
                        entries, offset = await reader.read_new(path, offset)
                    except Exception:
                        logger.debug(
                            "sse tail read failed for %s", session_id,
                            exc_info=True,
                        )
                        entries = []
                    for e in entries:
                        yield _entry_to_frame(e)
                        last_heartbeat = time.monotonic()

                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_interval:
                    yield b": heartbeat\n\n"
                    last_heartbeat = now

                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db."""
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request,
                      max_seconds: float | None = None,
                      replay_only: int = 0) -> StreamingResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        # Pull the same reader base_dir the watcher used (may be None →
        # spec default of ``Path.home()``).
        base_dir = getattr(orch, "transcript_base_dir", None)
        poll = getattr(
            getattr(orch, "config", None), "sessions",
            None,
        )
        poll_seconds = float(getattr(poll, "transcript_poll_seconds", 2) or 2)
        inner = build_sessions_router(
            db=orch.db,
            base_dir=base_dir,
            poll_interval=poll_seconds,
            session_providers=getattr(orch, "session_providers", None),
            config=getattr(orch, "config", None),
        )
        # Delegate to the built router's handler by extracting its route
        # function — cheaper than double-registering.
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/sessions/{session_id}/stream":
                return await route.endpoint(
                    session_id=session_id, request=request,
                    max_seconds=max_seconds, replay_only=replay_only,
                )
        raise HTTPException(status_code=500,
                             detail="sessions router misconfigured")

    return router


#: The router registered by :func:`src.api.app.create_app`.
router = _build_default_router()
