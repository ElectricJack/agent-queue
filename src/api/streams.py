"""In-memory streamable-command registry backing the console-stream pane view.

Not a `tables.py` row: a stream is short-lived and its output can be large.
Mirrors src/api/sessions.py's SSE shape but backs a live subprocess instead
of a transcript file. See
docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §8.1.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["ConsoleFrame", "StreamHandle", "StreamRegistry"]

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
