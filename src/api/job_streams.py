"""Console stream compatibility views; managed jobs own finite execution."""

from __future__ import annotations

import json
import base64
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from src.api.auth import LOCAL_SCOPE
from src.api.job_output import job_output
from src.jobs.adapters import finite_command
from src.jobs.output import output_frames
from src.jobs.policy import JobError, TERMINAL
from src.jobs.result import clean


def is_job_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def console_frame(frame: dict) -> dict:
    kind = frame["type"]
    result = {"seq": max(0, frame.get("next", 0) - 1), "ts": time.time()}
    if kind == "chunk":
        result.update(type="line", stream="stdout", text=clean(frame["data"]))
    elif kind == "gap":
        result.update(
            type="line",
            stream="stdout",
            truncated=True,
            text=f"[output omitted: bytes {frame['after']}..{frame['next']}]",
        )
    elif kind == "terminal":
        result["seq"] = frame.get("next", 0)
        result.update(
            type="killed" if frame["state"] == "cancelled" else "exit",
            rc=(frame.get("result") or {}).get("exit_code"),
        )
    else:
        result.update(type=kind, **{k: v for k, v in frame.items() if k != "type"})
    return result


def console_frames(frame: dict) -> list[dict]:
    if frame["type"] != "chunk":
        return [console_frame(frame)]
    # Split with the original bytes, so invalid UTF-8 and multibyte text
    # cannot change reconnect offsets. Giant lines are bounded rows too.
    raw = base64.b64decode(frame["data_base64"])
    cursor = frame["offset"]
    frames = []
    for line in raw.splitlines(keepends=True):
        for start in range(0, len(line), 4096):
            piece = line[start : start + 4096]
            cursor += len(piece)
            frames.append(
                console_frame(
                    {
                        "type": "chunk",
                        "next": cursor,
                        "data": piece.decode("utf-8", "replace").rstrip("\r\n"),
                    }
                )
            )
    return frames


class JobStreams:
    def __init__(self, handler, config):
        self.handler, self.config = handler, config

    async def execute(self, request, name, **args):
        scope = asdict(getattr(request.state, "scope", LOCAL_SCOPE))
        return await self.handler.execute(name, {**args, "_scope": scope})

    async def get(self, request, job_id):
        response = await self.execute(request, "job_get", job_id=job_id)
        if not response.get("success"):
            raise HTTPException(status_code=404, detail="not_found")
        return response["job"]

    async def start(self, request, body):
        try:
            preset, args = finite_command(body.command)
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = await self.handler.db.get_session(body.session_id)
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        if (
            not session
            or not session.task_id
            or (scope.project_id and scope.project_id != session.project_id)
            or (body.project_id and body.project_id != session.project_id)
        ):
            raise HTTPException(status_code=404, detail="not_found")
        workspace = await self.handler.db.get_workspace_for_task(session.task_id)
        if not workspace or Path(body.cwd).resolve() != Path(workspace.workspace_path).resolve():
            raise HTTPException(status_code=403, detail="jobs.cwd_invalid")
        task = await self.handler.db.get_task(session.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="not_found")
        response = await self.execute(
            request,
            "job_submit",
            preset=preset,
            argv=args,
            project_id=session.project_id,
            task_id=session.task_id,
            claim_epoch=task.claim_epoch,
            idempotency_key=body.idempotency_key or str(uuid.uuid4()),
        )
        if not response.get("success"):
            raise HTTPException(status_code=400, detail=response.get("error", "jobs.rejected"))
        job = response["job"]
        return {"stream_id": job["id"], "status": self.status(job)}

    @staticmethod
    def status(job):
        return (
            "killed"
            if job["state"] == "cancelled"
            else ("exited" if job["state"] in TERMINAL else "running")
        )

    async def metadata(self, request, job_id):
        job = await self.get(request, job_id)
        return {
            "stream_id": job_id,
            "title": job["preset"],
            "status": self.status(job),
            "exit_code": (job.get("result") or {}).get("exit_code"),
            "started_at": job.get("started_at") or job["submitted_at"],
            "ended_at": job.get("ended_at"),
            "session_id": job.get("submitter_session_id") or "",
            "project_id": job["project_id"],
            "client_reconnect_attempts": self.config.streams.client_reconnect_attempts,
        }

    async def tail(self, request, job_id, after_seq):
        job = await self.get(request, job_id)
        after = max(0, after_seq + 1)
        response = await self.execute(request, "job_logs", job_id=job_id, after=after)
        if not response.get("success"):
            if response.get("error") == "logs_not_ready":
                frames = []
            else:
                raise HTTPException(
                    status_code=410 if response.get("error") == "logs_expired" else 404,
                    detail=response,
                )
        else:
            frames = [
                console
                for frame in output_frames(response, after)
                for console in console_frames(frame)
            ]
            if job["state"] in TERMINAL and response["next"] >= response["seen"]:
                frames.append(
                    console_frame(
                        {
                            "type": "terminal",
                            "next": response["next"],
                            "state": job["state"],
                            "result": job["result"],
                        }
                    )
                )
        return {
            "frames": frames,
            "status": self.status(job),
            "exit_code": (job.get("result") or {}).get("exit_code"),
        }

    async def subscribe(self, request, job_id, after_seq):
        response = await job_output(job_id, request, max(0, after_seq + 1), self.handler)
        if not isinstance(response, StreamingResponse):
            return response

        async def frames():
            async for event in response.body_iterator:
                raw = event.decode() if isinstance(event, bytes) else event
                data = next(
                    (line[6:] for line in raw.splitlines() if line.startswith("data: ")), None
                )
                if data is None:
                    yield event
                else:
                    for frame in console_frames(json.loads(data)):
                        yield f"data: {json.dumps(frame)}\n\n".encode()

        return StreamingResponse(frames(), media_type="text/event-stream", headers=response.headers)

    async def kill(self, request, job_id):
        response = await self.execute(request, "job_cancel", job_id=job_id)
        if not response.get("success"):
            raise HTTPException(status_code=404, detail="not_found")
        return {"stream_id": job_id, "status": self.status(response["job"])}
