"""In-memory session provider for tests and dry runs.

Importable from production code on purpose: ``sessions.provider: fake`` is a
supported config value, so a daemon can be brought up on the full session
path with nothing actually spawned.  Every reconciler, scheduler and cascade
test runs against this.

Test knobs (all keyed by session *name*, settable before or after start):

``script_death(name, after_s=0.0)``
    The session's process reports dead once *after_s* has elapsed since the
    call.  ``after_s=0`` is immediate.
``script_ready(name)`` / ``script_startup_death(name)``
    Force :meth:`start` to succeed immediately, or to raise
    :class:`~src.sessions.provider.SessionDiedDuringStartup`.
``swallow_next_nudge(name)``
    The next :meth:`nudge` raises :class:`NotSubmitted` — the
    pasted-but-not-submitted race, on demand.
``script_partial_list(exc)``
    The next :meth:`list_running` raises :class:`PartialListError`.
``sent_nudges``
    Every accepted ``(name, text)`` in order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar

from src.sessions.provider import (
    Cap,
    NotSubmitted,
    PartialListError,
    SessionDiedDuringStartup,
    SessionHandle,
    SessionProvider,
    SessionSpec,
)

__all__ = ["FakeProvider", "FakeSession"]


@dataclass
class FakeSession:
    """One in-memory session."""

    spec: SessionSpec
    handle: SessionHandle
    started_at: float
    alive: bool = True
    process_alive: bool = True
    dead_at: float | None = None
    meta: dict[str, str] = field(default_factory=dict)
    output: list[str] = field(default_factory=list)
    activity: float = 0.0
    interrupts: int = 0
    stopped_at: float | None = None


class FakeProvider(SessionProvider):
    """Dict-backed :class:`SessionProvider` with scriptable behavior."""

    name: ClassVar[str] = "fake"
    capabilities: ClassVar[frozenset[Cap]] = frozenset(
        {Cap.PEEK, Cap.NUDGE, Cap.ACTIVITY, Cap.ATTACH}
    )

    def __init__(self, config=None):
        self.config = config
        self.sessions: dict[str, FakeSession] = {}
        self.sent_nudges: list[tuple[str, str]] = []
        self.starts: list[SessionSpec] = []
        self._swallow: set[str] = set()
        self._startup_death: set[str] = set()
        self._partial_list: Exception | None = None
        #: Names whose ``start`` should raise a generic failure.
        self._start_error: dict[str, Exception] = {}

    # -- test knobs --------------------------------------------------------

    def script_death(self, name: str, after_s: float = 0.0) -> None:
        s = self.sessions.get(name)
        if s is not None:
            s.dead_at = time.time() + after_s
            if after_s <= 0:
                s.process_alive = False

    def script_ready(self, name: str) -> None:
        s = self.sessions.get(name)
        if s is not None:
            s.alive = True
            s.process_alive = True
            s.dead_at = None

    def script_startup_death(self, name: str) -> None:
        self._startup_death.add(name)

    def script_start_error(self, name: str, exc: Exception) -> None:
        self._start_error[name] = exc

    def swallow_next_nudge(self, name: str) -> None:
        self._swallow.add(name)

    def script_partial_list(self, exc: Exception | None = None) -> None:
        self._partial_list = exc or RuntimeError("no server")

    def feed_output(self, name: str, text: str, *, activity: bool = True) -> None:
        """Append pane text (and, by default, bump activity)."""
        s = self.sessions.get(name)
        if s is None:
            return
        s.output.append(text)
        if activity:
            s.activity = time.time()

    # -- internals ---------------------------------------------------------

    def _get(self, h: SessionHandle) -> FakeSession | None:
        s = self.sessions.get(h.name)
        if s is None:
            return None
        # Instance-token fence: a same-named successor is a different
        # session, and an operation aimed at the predecessor must miss.
        if h.instance_token and s.handle.instance_token != h.instance_token:
            return None
        return s

    def _settle(self, s: FakeSession) -> None:
        if s.dead_at is not None and time.time() >= s.dead_at:
            s.process_alive = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self, spec: SessionSpec) -> SessionHandle:
        if spec.session_name in self._start_error:
            raise self._start_error.pop(spec.session_name)
        handle = SessionHandle(
            name=spec.session_name,
            provider=self.name,
            instance_token=spec.instance_token,
        )
        if spec.session_name in self._startup_death:
            self._startup_death.discard(spec.session_name)
            raise SessionDiedDuringStartup(spec.session_name, detail="scripted startup death")
        now = time.time()
        self.sessions[spec.session_name] = FakeSession(
            spec=spec,
            handle=handle,
            started_at=now,
            activity=now,
        )
        self.starts.append(spec)
        return handle

    async def stop(self, h: SessionHandle, *, grace: float = 2.0) -> None:
        s = self._get(h)
        if s is None:
            return  # idempotent, and fenced
        s.alive = False
        s.process_alive = False
        s.stopped_at = time.time()
        self.sessions.pop(h.name, None)

    async def interrupt(self, h: SessionHandle) -> None:
        s = self._get(h)
        if s is not None:
            s.interrupts += 1

    # -- observation -------------------------------------------------------

    async def is_running(self, h: SessionHandle) -> bool:
        s = self._get(h)
        return bool(s and s.alive)

    async def process_alive(self, h: SessionHandle, process_names: tuple[str, ...] = ()) -> bool:
        s = self._get(h)
        if s is None:
            return False
        self._settle(s)
        return s.process_alive

    async def list_running(self, prefix: str) -> list[SessionHandle]:
        handles = [s.handle for name, s in sorted(self.sessions.items()) if name.startswith(prefix)]
        if self._partial_list is not None:
            exc = self._partial_list
            self._partial_list = None
            raise PartialListError(handles, exc)
        return handles

    async def last_activity(self, h: SessionHandle) -> float | None:
        s = self._get(h)
        return s.activity if s else None

    async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str:
        s = self._get(h)
        if s is None:
            return ""
        return "\n".join(s.output[-lines:])

    # -- interaction -------------------------------------------------------

    async def nudge(self, h: SessionHandle, text: str) -> None:
        s = self._get(h)
        if s is None:
            raise NotSubmitted(f"session {h.name!r} is gone")
        if h.name in self._swallow:
            self._swallow.discard(h.name)
            raise NotSubmitted(f"submit unconfirmed for {h.name!r}")
        self.sent_nudges.append((h.name, text))
        s.output.append(text)
        # Deliberately does *not* bump ``activity``: our own poke must not
        # look like agent progress (poke discounting, design §4.5).

    async def attach_command(self, h: SessionHandle) -> str:
        return f"# fake session {h.name} (nothing to attach to)"

    # -- metadata ----------------------------------------------------------

    async def set_meta(self, h: SessionHandle, key: str, value: str) -> None:
        s = self._get(h)
        if s is not None:
            s.meta[key] = value

    async def get_meta(self, h: SessionHandle, key: str) -> str | None:
        s = self._get(h)
        return s.meta.get(key) if s else None
