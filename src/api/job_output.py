"""Authorized SSE attachments over durable job output; never an executor."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.auth import LOCAL_SCOPE
from src.api.dependencies import get_command_handler
from src.api.models.job import JobErrorResponse
from src.jobs.output import OutputHub

router = APIRouter()


@router.get(
    "/api/jobs/{job_id}/output",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        404: {"description": "Job is absent or inaccessible"},
        410: {
            "description": "Logs expired; immutable result remains available",
            "model": JobErrorResponse,
        },
    },
)
async def job_output(
    job_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    ch=Depends(get_command_handler),
):
    scope = asdict(getattr(request.state, "scope", LOCAL_SCOPE))

    async def execute(name, **args):
        return await ch.execute(name, {"job_id": job_id, **args, "_scope": scope})

    job = await execute("job_get")
    if not job.get("success"):
        raise HTTPException(status_code=404, detail="not_found")
    if job["job"]["output_retention"] == "expired":
        return JSONResponse(
            {"error": "logs_expired", "result": job["job"]["result"]}, status_code=410
        )
    # Check both capabilities before attaching the shared, internal reader.
    logs = await execute("job_logs", after=after, limit=1)
    if not logs.get("success") and logs.get("error") != "logs_not_ready":
        raise HTTPException(status_code=404, detail="output_unavailable")
    hub = getattr(ch, "_job_output_hub", None)
    if hub is None:
        hub = ch._job_output_hub = OutputHub(ch)
    principal = scope.get("session_id") or "local"
    try:
        attachment = hub.subscribe(job_id, principal, after)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    async def generate():
        cursor = after
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    frame = await asyncio.wait_for(attachment.queue.get(), timeout=1)
                except TimeoutError:
                    # Recheck authorization even when the producer is silent.
                    if not (await execute("job_get")).get("success"):
                        return
                    yield b": heartbeat\n\n"
                    continue
                # Deleted owners and changed pool claims revoke a live viewer.
                if not (await execute("job_get")).get("success"):
                    return
                if frame["type"] == "disconnect":
                    frame = {**frame, "after": cursor, "next": cursor}
                else:
                    cursor = frame.get("next", cursor)
                yield f"id: {cursor}\ndata: {json.dumps(frame)}\n\n".encode()
                if frame["type"] in {"disconnect", "terminal", "error"}:
                    return
        finally:
            hub.unsubscribe(job_id, attachment)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
