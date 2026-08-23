"""In-memory streamable-command registry backing the console-stream pane view.

Not a `tables.py` row: a stream is short-lived and its output can be large.
Mirrors src/api/sessions.py's SSE shape but backs a live subprocess instead
of a transcript file. See
docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §8.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
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
FrameType = Literal["line", "exit", "killed"]


@dataclass
class ConsoleFrame:
    seq: int
    type: FrameType
    stream: FrameStream | None = None
    text: str | None = None
    rc: int | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "seq": self.seq, "ts": self.ts}
        if self.stream is not None:
            d["stream"] = self.stream
        if self.text is not None:
            d["text"] = self.text
        if self.rc is not None:
            d["rc"] = self.rc
        return d


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
    process: "asyncio.subprocess.Process | None" = None
    subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    truncated: bool = False
    _next_seq: int = field(default=0, repr=False)

    def next_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def append(self, frame: ConsoleFrame) -> None:
        if self.buffer.maxlen is not None and len(self.buffer) == self.buffer.maxlen:
            self.truncated = True
        self.buffer.append(frame)
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> "asyncio.Queue[ConsoleFrame]":
        q: "asyncio.Queue[ConsoleFrame]" = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[ConsoleFrame]") -> None:
        self.subscribers.discard(q)

    def replay_from(self, after_seq: int) -> list[ConsoleFrame]:
        return [f for f in self.buffer if f.seq > after_seq]


class StreamRegistry:
    """``dict[str, StreamHandle]`` keyed by uuid4, hung off the orchestrator."""

    def __init__(self, *, buffer_max_lines: int = 5000) -> None:
        self._buffer_max_lines = buffer_max_lines
        self._streams: dict[str, StreamHandle] = {}
        self._concurrency: dict[str, int] = {}

    def create(
        self, *, title: str, session_id: str, project_id: str | None,
        command: list[str], cwd: str,
    ) -> StreamHandle:
        stream_id = uuid.uuid4().hex
        handle = StreamHandle(
            stream_id=stream_id, title=title, session_id=session_id,
            project_id=project_id, command=command, cwd=cwd,
            buffer=deque(maxlen=self._buffer_max_lines),
        )
        self._streams[stream_id] = handle
        self._concurrency[session_id] = self._concurrency.get(session_id, 0) + 1
        return handle

    def get(self, stream_id: str) -> StreamHandle | None:
        return self._streams.get(stream_id)

    def concurrent_count(self, session_id: str) -> int:
        return self._concurrency.get(session_id, 0)

    def finish(self, handle: StreamHandle) -> None:
        self._concurrency[handle.session_id] = max(
            0, self._concurrency.get(handle.session_id, 1) - 1
        )

    def evict(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)

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


class StreamStartResponse(BaseModel):
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


async def _validate_cwd(cwd: str, *, db, workspace_dir: str) -> str | None:
    """Mirrors ``CommandHandler._validate_path`` (src/commands/handler.py:526)
    without depending on a live ``CommandHandler`` instance — this router
    factory, like ``build_sessions_router``, takes ``db``/``config`` directly.
    """
    real = os.path.realpath(cwd)
    workspace_real = os.path.realpath(workspace_dir)
    if real.startswith(workspace_real + os.sep) or real == workspace_real:
        return real
    repos = await db.list_repos()
    for repo in repos:
        if repo.source_path:
            repo_real = os.path.realpath(repo.source_path)
            if real.startswith(repo_real + os.sep) or real == repo_real:
                return real
    workspaces = await db.list_workspaces()
    for ws in workspaces:
        ws_real = os.path.realpath(ws.workspace_path)
        if real.startswith(ws_real + os.sep) or real == ws_real:
            return real
    return None


def _can_start(scope) -> bool:
    return scope.kind == "local" or scope.elevated


def _can_access(scope, handle: StreamHandle) -> bool:
    if scope.kind == "local":
        return True
    if scope.session_id == handle.session_id:
        return True
    if scope.elevated and scope.project_id in (None, handle.project_id):
        return True
    return False


async def _spawn_and_pump(handle: StreamHandle, registry: StreamRegistry) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *handle.command,
            cwd=handle.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        handle.status = "exited"
        handle.exit_code = -1
        handle.ended_at = time.time()
        handle.append(
            ConsoleFrame(seq=handle.next_seq(), type="exit", rc=-1, text=f"failed to start: {exc}")
        )
        registry.finish(handle)
        return

    handle.process = proc

    async def _pump(stream_name: FrameStream, pipe) -> None:
        if pipe is None:
            return
        while True:
            line = await pipe.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            handle.append(ConsoleFrame(seq=handle.next_seq(), type="line", stream=stream_name, text=text))

    stdout_task = asyncio.create_task(_pump("stdout", proc.stdout))
    stderr_task = asyncio.create_task(_pump("stderr", proc.stderr))
    try:
        # A pump (or proc.wait) failure must never leak the stream as
        # permanently "running" nor leak its concurrency slot — the
        # ``finally`` below always marks a terminal status and always
        # calls registry.finish, whether this succeeds or raises.
        rc: int | None = None
        exc: Exception | None = None
        try:
            rc = await proc.wait()
            await asyncio.gather(stdout_task, stderr_task)
        except Exception as e:  # noqa: BLE001 - must not leak the stream/slot
            logger.warning("stream %s pump failed", handle.stream_id, exc_info=True)
            exc = e
            stdout_task.cancel()
            stderr_task.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            rc = proc.returncode if proc.returncode is not None else -1

        handle.ended_at = time.time()
        if handle.status != "killed":
            handle.status = "exited"
            handle.exit_code = rc
            frame_kwargs = {"seq": handle.next_seq(), "type": "exit", "rc": rc}
            if exc is not None:
                frame_kwargs["text"] = f"stream pump failed: {exc}"
            handle.append(ConsoleFrame(**frame_kwargs))
    finally:
        registry.finish(handle)


async def _kill(handle: StreamHandle, *, grace_seconds: float) -> None:
    if handle.status != "running" or handle.process is None:
        return
    handle.status = "killed"
    proc = handle.process
    stage_seconds = max(0.1, grace_seconds / 3)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGKILL):
        if proc.returncode is not None:
            break
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            break
        try:
            await asyncio.wait_for(proc.wait(), timeout=stage_seconds)
            break
        except asyncio.TimeoutError:
            continue
    handle.append(ConsoleFrame(seq=handle.next_seq(), type="killed"))


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
) -> APIRouter:
    """Router factory so tests can wire a lightweight db without the daemon."""

    router = APIRouter()
    reg = registry if registry is not None else StreamRegistry(
        buffer_max_lines=getattr(config.streams, "buffer_max_lines", 5000)
    )

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

        project_id = body.project_id
        if scope.elevated and scope.project_id is not None:
            if project_id is None:
                project_id = scope.project_id
            elif project_id != scope.project_id:
                raise HTTPException(status_code=403, detail="out of scope: project_id mismatch")

        real_cwd = await _validate_cwd(body.cwd, db=db, workspace_dir=workspace_dir)
        if real_cwd is None:
            raise HTTPException(status_code=403, detail="cwd is outside any accessible workspace")

        cap = getattr(config.streams, "max_concurrent_per_session", 3)
        if reg.concurrent_count(body.session_id) >= cap:
            raise HTTPException(status_code=429, detail="too many concurrent streams")

        handle = reg.create(
            title=body.title or "Console", session_id=body.session_id,
            project_id=project_id, command=list(body.command), cwd=real_cwd,
        )
        # Snapshot the just-created status ("running") rather than reading
        # handle.status after the awaits below: the spawned task can race
        # ahead and finish (e.g. a fast "echo") before this handler resumes,
        # which would otherwise make the start response non-deterministic.
        start_status = handle.status
        asyncio.create_task(_spawn_and_pump(handle, reg))

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
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")
        return StreamMetadata(
            stream_id=handle.stream_id, title=handle.title, status=handle.status,
            exit_code=handle.exit_code, started_at=handle.started_at,
            ended_at=handle.ended_at, session_id=handle.session_id,
            project_id=handle.project_id,
        )

    @router.get("/api/streams/{stream_id}/subscribe")
    async def subscribe(stream_id: str, request: Request, after_seq: int = -1) -> StreamingResponse:
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")

        async def gen():
            replayed = handle.replay_from(after_seq)
            first = True
            for frame in replayed:
                d = frame.to_dict()
                if first and handle.truncated and after_seq < 0:
                    d = {**d, "truncated": True}
                first = False
                yield f"data: {json.dumps(d)}\n\n".encode()
                if frame.type in ("exit", "killed"):
                    return

            if handle.status != "running":
                return

            q = handle.subscribe()
            last_heartbeat = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=1.0)
                        yield f"data: {json.dumps(frame.to_dict())}\n\n".encode()
                        last_heartbeat = time.monotonic()
                        if frame.type in ("exit", "killed"):
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
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")
        frames = handle.replay_from(after_seq)
        return {
            "frames": [f.to_dict() for f in frames],
            "status": handle.status,
            "exit_code": handle.exit_code,
        }

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
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000)
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
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
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
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
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
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/tail":
                return await route.endpoint(stream_id=stream_id, request=request, after_seq=after_seq)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    return router


#: The router registered by :func:`src.api.app.create_app`.
router = _build_default_router()
