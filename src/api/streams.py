"""Bounded console viewers over durable managed jobs; never an executor.

The job id is the stream id. Readers can be discarded and reconstructed from
retained output without affecting execution, admission, deadlines or pins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from src.jobs.policy import JobError, TERMINAL
from src.jobs.adapters import finite_command
from src.jobs.artifacts import OutputStore, job_directory
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

__all__ = [
    "ConsoleFrame",
    "StreamHandle",
    "StreamRegistry",
    "StreamStartRequest",
    "StreamStartResponse",
    "StreamMetadata",
    "build_streams_router",
    "router",
]

StreamStatus = Literal["running", "exited", "killed"]
FrameStream = Literal["stdout", "stderr"]
FrameType = Literal["line", "exit", "killed", "gap"]


@dataclass
class ConsoleFrame:
    seq: int
    type: FrameType
    stream: FrameStream | None = None
    text: str | None = None
    rc: int | None = None
    ts: float = field(default_factory=time.time)
    after: int | None = None
    next: int | None = None

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "seq": self.seq, "ts": self.ts}
        if self.stream is not None:
            d["stream"] = self.stream
        if self.text is not None:
            d["text"] = self.text
        if self.rc is not None:
            d["rc"] = self.rc
        if self.after is not None:
            d.update(after=self.after, next=self.next)
        return d


def _frame_bytes(frame: ConsoleFrame) -> int:
    """Payload size of a buffered frame, for the ``buffer_max_bytes`` cap.

    Only the text payload is counted — the fixed per-frame overhead is small
    and constant, and ``buffer_max_lines`` already bounds the frame count.
    """
    return len(frame.text.encode("utf-8", "replace")) if frame.text else 0


@dataclass
class StreamHandle:
    stream_id: str
    title: str
    session_id: str
    project_id: str | None
    command: list[str]
    cwd: str
    status: StreamStatus = "running"
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    buffer: "deque[ConsoleFrame]" = field(default_factory=lambda: deque(maxlen=5000))
    buffer_max_bytes: int | None = None
    job_id: str | None = None
    viewer: "asyncio.Task | None" = None
    output_cursor: int = 0
    finished: bool = False
    reader_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    subscriber_owners: dict = field(default_factory=dict)
    truncated: bool = False
    _next_seq: int = field(default=0, repr=False)
    _buffer_bytes: int = field(default=0, repr=False)

    def next_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    @property
    def buffer_bytes(self) -> int:
        """Payload bytes currently held by the ring buffer (see ``append``)."""
        return self._buffer_bytes

    def append(self, frame: ConsoleFrame) -> None:
        # Two caps, both from ``streams:``: ``buffer_max_lines`` is the deque's
        # own ``maxlen``, ``buffer_max_bytes`` is accounted here. Without the
        # byte cap a handful of pathologically long lines can pin
        # ``buffer_max_lines`` x line-length bytes in memory (design §8.1).
        if self.buffer.maxlen is not None and len(self.buffer) == self.buffer.maxlen:
            # ``deque.append`` is about to drop the oldest frame for us.
            self._buffer_bytes -= _frame_bytes(self.buffer[0])
            self.truncated = True
        self.buffer.append(frame)
        self._buffer_bytes += _frame_bytes(frame)
        if self.buffer_max_bytes is not None:
            # Always keep the newest frame, even when it alone busts the cap:
            # dropping it would lose an ``exit``/``killed`` frame and leave
            # subscribers replaying a stream that never terminates.
            while len(self.buffer) > 1 and self._buffer_bytes > self.buffer_max_bytes:
                self._buffer_bytes -= _frame_bytes(self.buffer.popleft())
                self.truncated = True
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                first = q.get_nowait()
                while not q.empty():
                    q.get_nowait()
                cursor = first.after if first.after is not None else max(0, first.seq - 1)
                q.put_nowait(ConsoleFrame(seq=cursor, type="gap", after=cursor, next=cursor,
                                         text="slow reader disconnected; reconnect to resume"))
                self.unsubscribe(q)

    def subscribe(self, principal="local") -> "asyncio.Queue[ConsoleFrame]":
        if sum(owner == principal for owner in self.subscriber_owners.values()) >= 2:
            raise ValueError("too many attachments")
        q: "asyncio.Queue[ConsoleFrame]" = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        self.subscriber_owners[q] = principal
        return q

    def unsubscribe(self, q: "asyncio.Queue[ConsoleFrame]") -> None:
        self.subscribers.discard(q)
        self.subscriber_owners.pop(q, None)

    def replay_from(self, after_seq: int) -> list[ConsoleFrame]:
        return [f for f in self.buffer if f.seq > after_seq]


class StreamRegistry:
    """Bounded viewers keyed by the canonical job id; owns no execution."""

    def __init__(
        self,
        *,
        buffer_max_lines: int = 5000,
        buffer_max_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._buffer_max_lines = buffer_max_lines
        self._buffer_max_bytes = buffer_max_bytes
        self._streams: dict[str, StreamHandle] = {}
        self._concurrency: dict[str, int] = {}

    def create(
        self, *, title: str, session_id: str, project_id: str | None,
        command: list[str], cwd: str, job_id: str | None = None,
    ) -> StreamHandle:
        stream_id = job_id or uuid.uuid4().hex
        if stream_id in self._streams:
            return self._streams[stream_id]
        handle = StreamHandle(
            stream_id=stream_id, job_id=job_id, title=title, session_id=session_id,
            project_id=project_id, command=command, cwd=cwd,
            buffer=deque(maxlen=self._buffer_max_lines),
            buffer_max_bytes=self._buffer_max_bytes,
        )
        self._streams[stream_id] = handle
        self._concurrency[session_id] = self._concurrency.get(session_id, 0) + 1
        return handle

    def get(self, stream_id: str) -> StreamHandle | None:
        return self._streams.get(stream_id)

    def concurrent_count(self, session_id: str) -> int:
        return self._concurrency.get(session_id, 0)

    def finish(self, handle: StreamHandle) -> None:
        if handle.finished:
            return
        handle.finished = True
        self._concurrency[handle.session_id] = max(
            0, self._concurrency.get(handle.session_id, 1) - 1
        )

    def evict(self, stream_id: str) -> None:
        handle = self._streams.pop(stream_id, None)
        if handle:
            self.finish(handle)
            if handle.viewer:
                handle.viewer.cancel()

    def all_finished_before(self, cutoff: float) -> list[str]:
        return [
            sid
            for sid, h in self._streams.items()
            if h.status != "running" and h.ended_at is not None and h.ended_at < cutoff
        ]


_HEARTBEAT_SECONDS = 15.0


class StreamStartRequest(BaseModel):
    # Deliberately not ``list[str]``: ANY non-list ``command`` shape (a raw
    # shell string, a number, null, an object, ...) must reach the handler
    # so it can be rejected as a uniform 400 ("command must be a non-empty
    # list of strings") rather than FastAPI's automatic 422
    # request-validation error, which would fire before the handler runs
    # for anything Pydantic can't coerce into a narrower type.
    command: Any
    cwd: str
    title: str | None = None
    session_id: str
    project_id: str | None = None
    idempotency_key: str | None = None


class StreamStartResponse(BaseModel):
    stream_id: str
    status: str


class StreamKillResponse(BaseModel):
    stream_id: str
    status: str


class StreamMetadata(BaseModel):
    stream_id: str
    title: str
    status: str
    exit_code: int | None
    started_at: float
    ended_at: float | None
    session_id: str
    project_id: str | None
    # Server-owned reconnect budget for the console-stream pane's SSE
    # backoff loop (``streams.client_reconnect_attempts``, design §7.2) —
    # the browser has no other way to read the daemon's config.
    client_reconnect_attempts: int = 5


def _can_start(scope) -> bool:
    return scope.kind == "local" or scope.elevated


def _output_frames(output):
    records = [(c["offset"], "line", c) for c in output["chunks"]]
    records += [(g["after"], "gap", g) for g in output["gaps"]]
    for offset, kind, record in sorted(records, key=lambda r: r[0]):
        if kind == "gap":
            cursor = record["next"]
            yield ConsoleFrame(seq=cursor, type="gap", after=offset, next=cursor,
                               text=f"[output gap: {offset}..{cursor}]")
        else:
            # Preserve console rows while limiting even giant lines to the
            # store's bounded read chunk. Offsets count original bytes.
            for line in record["data"].splitlines(keepends=True):
                cursor = offset + len(line)
                yield ConsoleFrame(seq=cursor, type="line", stream="stdout",
                                   text=line.decode("utf-8", "replace").rstrip("\r\n"),
                                   after=offset, next=cursor)
                offset = cursor


async def _read_output(handle, job, config, after):
    def read():
        store = OutputStore(
            job_directory(Path(config.data_dir), job["id"]),
            head_bytes=job["contract"]["head_bytes"],
            tail_bytes=job["contract"]["tail_bytes"], readonly=True,
        )
        try:
            return store.read(after, 65536)
        finally:
            store.close()

    # Live and replay attachments share the same serialized reader.
    async with handle.reader_lock:
        try:
            if job["output_retention"] != "expired":
                return await asyncio.to_thread(read)
        except FileNotFoundError:
            return None  # queued jobs have no output yet


async def _view_job(handle: StreamHandle, registry: StreamRegistry, *, db, config) -> None:
    """One bounded reader per job. Dropping it never affects execution."""
    try:
        while True:
            job = await db.get_job(handle.job_id)
            if not job:
                handle.status = "exited"
                handle.ended_at = time.time()
                registry.finish(handle)
                return

            output = await _read_output(handle, job, config, handle.output_cursor)
            if output:
                for frame in _output_frames(output):
                    if frame.type == "gap":
                        handle.truncated = True
                    handle.append(frame)
                handle.output_cursor = output["next"]
                if output["next"] < output["seen"]:
                    await asyncio.sleep(0)
                    continue
            if job["state"] in TERMINAL:
                handle.status = "killed" if job["state"] == "cancelled" else "exited"
                handle.exit_code = job.get("exit_code")
                handle.ended_at = job.get("ended_at") or time.time()
                handle.append(ConsoleFrame(
                    seq=handle.output_cursor + 1,
                    type="killed" if handle.status == "killed" else "exit",
                    rc=handle.exit_code,
                    text=(job.get("result") or {}).get("infra_reason"),
                ))
                registry.finish(handle)
                return
            await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Viewer errors are not execution failures. A later attachment can
        # resume the durable store; never signal or cancel the producer here.
        logger.warning("job output reader %s stopped", handle.job_id, exc_info=True)


_sweep_task: "asyncio.Task | None" = None


def _start_retention_sweep(registry: StreamRegistry, retention_seconds: float) -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(30.0)
            cutoff = time.time() - retention_seconds
            for stream_id in registry.all_finished_before(cutoff):
                registry.evict(stream_id)

    _sweep_task = asyncio.create_task(_loop())


def build_streams_router(
    *, db, config, workspace_dir: str, registry: StreamRegistry | None = None,
    command_handler=None,
) -> APIRouter:
    """Router factory so tests can wire a lightweight db without the daemon."""

    router = APIRouter()
    reg = registry if registry is not None else StreamRegistry(
        buffer_max_lines=getattr(config.streams, "buffer_max_lines", 5000),
        buffer_max_bytes=getattr(config.streams, "buffer_max_bytes", 2 * 1024 * 1024),
    )

    def handler():
        if command_handler is not None:
            return command_handler
        from src.api.dependencies import get_command_handler

        return get_command_handler()

    async def command(request, name, args):
        response = await handler().execute(name, {**args, "_scope": asdict(request.state.scope)})
        if not response.get("success"):
            error = response.get("error_code") or response.get("error", "job request refused")
            code = 404 if error == "not_found" else 503 if error == "jobs.disabled" else 400
            raise HTTPException(status_code=code, detail=error)
        return response

    def watch(job, *, title="Console"):
        handle = reg.create(
            title=title, session_id=job.get("submitter_session_id") or "",
            project_id=job["project_id"], command=job["argv"], cwd=job["contract"]["cwd"],
            job_id=job["id"],
        )
        handle.started_at = job.get("started_at") or job["submitted_at"]
        if job["state"] in TERMINAL:
            handle.status = "killed" if job["state"] == "cancelled" else "exited"
            handle.exit_code = job.get("exit_code")
            handle.ended_at = job.get("ended_at")
        if handle.viewer is None or handle.viewer.done():
            if not handle.finished:
                handle.viewer = asyncio.create_task(_view_job(handle, reg, db=db, config=config))
        return handle

    async def resolve(stream_id, request, *, require_logs=True):
        # Consult durable scope on every route, even for cached handles: a
        # deleted task revokes reads immediately, and restart cannot widen it.
        job = (await command(request, "job_get", {"job_id": stream_id}))["job"]
        if require_logs and job["output_retention"] == "expired":
            raise HTTPException(status_code=410, detail={"error": "logs_expired", "result": job["result"]})
        return watch(job)

    @router.post("/api/streams", response_model=StreamStartResponse)
    async def start(body: StreamStartRequest, request: Request) -> StreamStartResponse:
        scope = request.state.scope
        if not _can_start(scope):
            raise HTTPException(
                status_code=403,
                detail="out of scope: stream start requires local or elevated scope",
            )
        if (
            not isinstance(body.command, list)
            or not body.command
            or not all(isinstance(c, str) for c in body.command)
        ):
            raise HTTPException(status_code=400, detail="command must be a non-empty list of strings")

        try:
            preset, argv = finite_command(body.command)
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session_record = await db.get_session(body.session_id)
        session = asdict(session_record) if session_record else None
        if not session or not session.get("task_id"):
            raise HTTPException(status_code=400, detail="a held task session is required")
        project_id = body.project_id or session["project_id"]
        if project_id != session["project_id"]:
            raise HTTPException(status_code=403, detail="out of scope: project_id mismatch")
        task = await db.get_task(session["task_id"])
        ws = await db.get_workspace_for_task(session["task_id"])
        if not task or not ws or os.path.realpath(body.cwd) != os.path.realpath(ws.workspace_path):
            raise HTTPException(status_code=403, detail="cwd must be the held task workspace")
        if not body.idempotency_key:
            raise HTTPException(status_code=400, detail="idempotency_key is required")
        cap = getattr(config.streams, "max_concurrent_per_session", 3)
        if reg.concurrent_count(body.session_id) >= cap:
            replay = await db.list_jobs(project_id=project_id, task_id=task.id, limit=1000)
            if not any(j["idempotency_key"] == body.idempotency_key for j in replay):
                raise HTTPException(status_code=429, detail="too many concurrent streams")
        job = (await command(request, "job_submit", {
            "project_id": project_id, "task_id": task.id, "session_id": body.session_id,
            "claim_epoch": task.claim_epoch, "preset": preset, "argv": argv,
            "idempotency_key": body.idempotency_key,
        }))["job"]
        handle = watch(job, title=body.title or "Console")
        start_status = handle.status
        try:
            await db.log_event(
                "stream.started", project_id=project_id,
                payload=json.dumps({
                    "stream_id": handle.stream_id, "command": handle.command,
                    "scope": "global_admin" if (scope.elevated and scope.project_id is None) else "session",
                }),
            )
        except Exception:
            logger.debug("stream.started log_event failed", exc_info=True)

        _start_retention_sweep(reg, getattr(config.streams, "retention_seconds", 300))
        return StreamStartResponse(stream_id=handle.stream_id, status=start_status)

    @router.get("/api/streams/{stream_id}", response_model=StreamMetadata)
    async def metadata(stream_id: str, request: Request) -> StreamMetadata:
        handle = await resolve(stream_id, request, require_logs=False)
        return StreamMetadata(
            stream_id=handle.stream_id, title=handle.title, status=handle.status,
            exit_code=handle.exit_code, started_at=handle.started_at,
            ended_at=handle.ended_at, session_id=handle.session_id,
            project_id=handle.project_id,
            client_reconnect_attempts=getattr(
                config.streams, "client_reconnect_attempts", 5
            ),
        )

    @router.get("/api/streams/{stream_id}/subscribe")
    async def subscribe(stream_id: str, request: Request, after_seq: int = -1) -> StreamingResponse:
        handle = await resolve(stream_id, request)

        try:
            q = handle.subscribe(request.state.scope.session_id or "local")
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

        async def gen():
            try:
                last_seq = max(0, after_seq)
                # Reconnect always reads retained ranges from the store, even
                # when the in-memory viewer has evicted older frames.
                while True:
                    job = (await command(request, "job_get", {"job_id": stream_id}))["job"]
                    output = await _read_output(handle, job, config, last_seq)
                    if output:
                        for frame in _output_frames(output):
                            yield f"data: {json.dumps(frame.to_dict())}\n\n".encode()
                        last_seq = output["next"]
                        if last_seq < output["seen"]:
                            continue
                    if job["state"] in TERMINAL:
                        terminal = ConsoleFrame(
                            seq=last_seq + 1,
                            type="killed" if job["state"] == "cancelled" else "exit",
                            rc=job.get("exit_code"),
                            text=(job.get("result") or {}).get("infra_reason"),
                        )
                        yield f"data: {json.dumps(terminal.to_dict())}\n\n".encode()
                        return
                    break

                last_heartbeat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=1.0)
                        if frame.seq <= last_seq and frame.text != (
                            "slow reader disconnected; reconnect to resume"
                        ):
                            continue
                        last_seq = frame.seq
                        yield f"data: {json.dumps(frame.to_dict())}\n\n".encode()
                        last_heartbeat = time.monotonic()
                        if frame.type in ("exit", "killed") or frame.text == (
                            "slow reader disconnected; reconnect to resume"
                        ):
                            return
                    except asyncio.TimeoutError:
                        pass
                    now = time.monotonic()
                    if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                        yield b": heartbeat\n\n"
                        last_heartbeat = now
            finally:
                handle.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/api/streams/{stream_id}/tail")
    async def tail(stream_id: str, request: Request, after_seq: int = -1) -> dict:
        handle = await resolve(stream_id, request)
        job = (await command(request, "job_get", {"job_id": stream_id}))["job"]
        output = await _read_output(handle, job, config, max(0, after_seq))
        frames = list(_output_frames(output)) if output else []
        status = "running"
        if job["state"] in TERMINAL and (not output or output["next"] == output["seen"]):
            status = "killed" if job["state"] == "cancelled" else "exited"
            frames.append(ConsoleFrame(seq=(output or {}).get("next", 0) + 1,
                                       type="killed" if status == "killed" else "exit",
                                       rc=job.get("exit_code")))
        return {
            "frames": [f.to_dict() for f in frames], "status": status,
            "exit_code": job.get("exit_code"),
        }

    @router.post("/api/streams/{stream_id}/kill", response_model=StreamKillResponse)
    async def kill(stream_id: str, request: Request) -> dict:
        handle = await resolve(stream_id, request, require_logs=False)
        if handle.status != "running":
            return {"stream_id": stream_id, "status": handle.status}
        await command(request, "job_cancel", {"job_id": handle.job_id})
        try:
            await db.log_event(
                "stream.killed", project_id=handle.project_id,
                payload=json.dumps({"stream_id": stream_id}),
            )
        except Exception:
            logger.debug("stream.killed log_event failed", exc_info=True)
        return {"stream_id": stream_id, "status": handle.status}

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db/config."""
    from src.api import dependencies as deps

    router = APIRouter()

    @router.post("/api/streams", response_model=StreamStartResponse)
    async def start(body: StreamStartRequest, request: Request) -> StreamStartResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000),
                buffer_max_bytes=getattr(
                    orch.config.streams, "buffer_max_bytes", 2 * 1024 * 1024
                ),
            )
            orch.stream_registry = registry
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams" and "POST" in route.methods:
                return await route.endpoint(body=body, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    @router.get("/api/streams/{stream_id}", response_model=StreamMetadata)
    async def metadata(stream_id: str, request: Request) -> StreamMetadata:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = orch.stream_registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000),
                buffer_max_bytes=getattr(orch.config.streams, "buffer_max_bytes", 2 * 1024 * 1024),
            )
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}" and "GET" in route.methods:
                return await route.endpoint(stream_id=stream_id, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    @router.get("/api/streams/{stream_id}/subscribe")
    async def subscribe(stream_id: str, request: Request, after_seq: int = -1) -> StreamingResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = orch.stream_registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000),
                buffer_max_bytes=getattr(orch.config.streams, "buffer_max_bytes", 2 * 1024 * 1024),
            )
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/subscribe":
                return await route.endpoint(stream_id=stream_id, request=request, after_seq=after_seq)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    @router.get("/api/streams/{stream_id}/tail")
    async def tail(stream_id: str, request: Request, after_seq: int = -1) -> dict:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = orch.stream_registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000),
                buffer_max_bytes=getattr(orch.config.streams, "buffer_max_bytes", 2 * 1024 * 1024),
            )
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/tail":
                return await route.endpoint(stream_id=stream_id, request=request, after_seq=after_seq)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    @router.post("/api/streams/{stream_id}/kill", response_model=StreamKillResponse)
    async def kill(stream_id: str, request: Request) -> dict:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = orch.stream_registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000),
                buffer_max_bytes=getattr(orch.config.streams, "buffer_max_bytes", 2 * 1024 * 1024),
            )
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/kill":
                return await route.endpoint(stream_id=stream_id, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    @router.get("/api/jobs/{job_id}/output")
    async def output(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
        if after < 0:
            raise HTTPException(status_code=400, detail="invalid output cursor")
        return await subscribe(stream_id=job_id, request=request, after_seq=after)

    return router


#: The router registered by :func:`src.api.app.create_app`.
router = _build_default_router()
