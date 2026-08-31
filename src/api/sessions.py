"""SSE session stream endpoint (S3, Task B3).

``GET /api/sessions/{session_id}/stream`` returns Server-Sent Events:

1. Replay of the transcript's normalized history — one ``data:`` frame per
   :class:`~src.sessions.transcripts.base.TranscriptEntry`, in file order.
2. Live tail — the endpoint polls the transcript at
   ``sessions.transcript_poll_seconds`` cadence and emits each new entry.
3. Heartbeat comments (``: heartbeat\n\n``) every 15 s so intermediaries
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
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.api.auth import LOCAL_SCOPE
from src.api.scope import check_command_scope
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
                      replay_only: int = 0,
                      attempt_id: str | None = None) -> StreamingResponse:
        current = await db.get_session(session_id)
        if current is None:
            current = await db.get_session_by_name(session_id)
        attempt = await db.get_task_session_attempt(attempt_id) if attempt_id else None
        if not attempt_id and current is not None and current.task_id and current.state in {"stopped", "sleeping", "quarantined"}:
            history = await db.list_task_session_attempts(current.task_id)
            retained = next((item for item in history if item["session_id"] == current.id), None)
            if retained is not None:
                attempt = await db.get_task_session_attempt(retained["id"])
        if attempt_id and (
            attempt is None
            or attempt["session_id"] != (current.id if current else session_id)
        ):
            raise HTTPException(status_code=404, detail="No attempt for this session")
        if current is None and attempt is None:
            raise HTTPException(status_code=404, detail=f"No session '{session_id}'")

        # Historical snapshots remain authoritative after a pool is reassigned
        # or its original session row is removed.
        session = current
        if attempt is not None:
            session = SimpleNamespace(
                **{**attempt, "id": attempt["session_id"],
                   "started_at": attempt["session_started_at"]}
            )
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        # A resolved projectless record is not an omitted request filter.
        if scope.kind != "local" and scope.project_id is not None and scope.project_id != session.project_id:
            raise HTTPException(status_code=403, detail="Session project is out of scope")
        scope_error = check_command_scope("session_show", {
            "session_id": session.id,
            "project_id": session.project_id,
            "task_id": session.task_id,
        }, scope)
        if scope_error:
            raise HTTPException(status_code=403, detail=scope_error)

        reader = resolve_reader(session.harness, base_dir=base_dir)
        started_at = time.monotonic()

        def is_current():
            return bool(
                current is not None
                and current.state in {"starting", "running", "draining"}
                and (attempt is None or (
                    attempt["ended_at"] is None
                    and attempt["state"] in {"starting", "running", "draining"}
                    and current.task_id == attempt["task_id"]
                    and current.started_at == attempt["session_started_at"]
                ))
            )

        def visible(entry):
            if attempt is not None and entry.ts < attempt["started_at"]:
                return False
            if attempt and attempt["ended_at"] is None and attempt.get("transcript_end_at") is not None:
                return 0 < entry.ts < attempt["transcript_end_at"]
            end = attempt["ended_at"] if attempt else getattr(current or session, "ended_at", None)
            return end is None or 0 < entry.ts <= end

        async def gen():
            nonlocal attempt, current
            offset = 0
            end = (attempt["ended_at"] or attempt.get("transcript_end_at")) if attempt else getattr(session, "ended_at", None)
            if not is_current() and end is None:
                payload = {"text": "This legacy attempt has no reliable recording end boundary; its transcript cannot be safely isolated."}
                yield f"event: unavailable\ndata: {json.dumps(payload)}\n\n".encode()
                return
            path = await asyncio.to_thread(reader.resolve_session, session) if reader else None
            if path is None:
                if not is_current():
                    payload = {"text": "The recording for this session attempt is unavailable."}
                    yield f"event: unavailable\ndata: {json.dumps(payload)}\n\n".encode()
                    return
                peek_text = await _best_effort_peek(current, session_providers, config) if reader else None
                yield _peek_frame(peek_text or f"(no transcript available for session {session_id}; falling back to peek)")
            else:
                entries, offset = await reader.read_new(path, 0)
                if is_current():
                    # Reading can yield while a retry starts. Refresh the end
                    # boundary before publishing even the initial replay.
                    current = await db.get_session(session.id)
                    if attempt_id:
                        fresh = await db.get_task_session_attempt(attempt_id)
                        if fresh is None:
                            return
                        attempt = fresh
                    if current is None and attempt is None:
                        yield b"event: complete\ndata: {}\n\n"
                        return
                for entry in entries:
                    if visible(entry):
                        yield _entry_to_frame(entry)

            if replay_only or not is_current():
                yield b"event: complete\ndata: {}\n\n"
                return

            last_heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                if max_seconds is not None and time.monotonic() - started_at > max_seconds:
                    return
                current = await db.get_session(session.id)
                if attempt_id:
                    fresh = await db.get_task_session_attempt(attempt_id)
                    if fresh is None:
                        return
                    attempt = fresh
                # A final bounded read captures the last output, but never
                # crosses into the next task claimed by this worker.
                if reader is not None:
                    if path is None:
                        path = await asyncio.to_thread(reader.resolve_session, session)
                    if path is not None:
                        try:
                            entries, offset = await reader.read_new(path, offset)
                        except Exception:
                            logger.debug("sse tail read failed for %s", session_id, exc_info=True)
                            entries = []
                        # File IO yields: a stop or claim release can commit
                        # during that read, so fence this batch afterwards too.
                        current = await db.get_session(session.id)
                        if attempt_id:
                            fresh = await db.get_task_session_attempt(attempt_id)
                            if fresh is None:
                                return
                            attempt = fresh
                        if current is None and attempt is None:
                            yield b"event: complete\ndata: {}\n\n"
                            return
                        for entry in entries:
                            if visible(entry):
                                yield _entry_to_frame(entry)
                                last_heartbeat = time.monotonic()
                if not is_current():
                    yield b"event: complete\ndata: {}\n\n"
                    return
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
                      replay_only: int = 0,
                      attempt_id: str | None = None) -> StreamingResponse:
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
                    max_seconds=max_seconds, replay_only=replay_only, attempt_id=attempt_id,
                )
        raise HTTPException(status_code=500,
                             detail="sessions router misconfigured")

    return router


#: The router registered by :func:`src.api.app.create_app`.
router = _build_default_router()
