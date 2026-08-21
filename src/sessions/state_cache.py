"""Shared tmux/process observation cache — "peek is not free".

The reconciler asks ``is_running`` / ``process_alive`` / ``list_running``
for every session every tick.  Naively that is one ``tmux`` and one ``ps``
subprocess *per session per question*.  This cache answers every question
of one tick from **one** ``list-panes -a`` plus **one** ``ps``, refreshed
when older than ``ttl`` (2 s by default).

Failure semantics are load-bearing (Gas City post-mortem):

* ``ps`` failing must never look like "everything died" —
  :meth:`procs` returns ``None`` and callers answer optimistically.
* tmux "no server" must never look like an empty listing when sessions
  were known — :meth:`panes` raises :class:`TmuxUnavailable` carrying the
  last-known-good snapshot so callers can defer instead of reaping.
  A *fresh* daemon that never saw a server gets an empty dict instead:
  there is nothing to defer over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.sessions.provider import SessionError

logger = logging.getLogger(__name__)

__all__ = ["PaneState", "TmuxStateCache", "TmuxUnavailable"]


class TmuxUnavailable(SessionError):
    """The tmux server could not be queried; carries last-known-good panes."""

    def __init__(self, last_good: dict[str, "PaneState"], cause: Exception | None = None):
        self.last_good = dict(last_good)
        self.cause = cause
        super().__init__(f"tmux unavailable ({len(self.last_good)} last known): {cause}")


def _is_no_server(exc: Exception) -> bool:
    """A clean "no server" (socket absent), as opposed to a hang/timeout."""
    return "no server running" in str(exc)


@dataclass(frozen=True)
class PaneState:
    """One tmux pane, as one ``list-panes -a`` line describes it."""

    session_name: str
    dead: bool
    pid: int


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    comm: str
    args: str


class TmuxStateCache:
    """One ``list-panes -a`` + one ``ps`` per tick, TTL-cached.

    *tmux_runner* is the provider's ``_tmux`` helper — ``await
    tmux_runner("list-panes", …) -> str`` raising on failure.
    """

    def __init__(
        self,
        tmux_runner: Callable[..., Awaitable[str]],
        *,
        ttl: float = 2.0,
    ):
        self._tmux = tmux_runner
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._panes: dict[str, PaneState] = {}
        self._panes_at: float = 0.0
        self._ever_saw_server = False
        self._procs: dict[int, Proc] | None = None
        self._procs_at: float = 0.0

    def invalidate(self) -> None:
        """Force the next question to re-observe (after start/stop)."""
        self._panes_at = 0.0
        self._procs_at = 0.0

    def forget(self, session_name: str) -> None:
        """Drop *session_name* from last-known-good (we killed it ourselves).

        Killing the last session makes tmux's server exit (``exit-empty``),
        so the next ``list-panes`` fails with "no server".  Without this,
        that legitimate emptiness would defer forever on a stale snapshot.
        """
        self._panes.pop(session_name, None)
        self.invalidate()

    # -- panes -------------------------------------------------------------

    async def panes(self) -> dict[str, PaneState]:
        """Session name → the session's *first* pane state.

        One pane per session is this system's invariant (the provider
        creates single-pane sessions); if an operator splits a pane while
        attached, a session counts as dead only when **all** its panes are
        dead.
        """
        async with self._lock:
            if time.monotonic() - self._panes_at < self._ttl:
                return dict(self._panes)
            try:
                out = await self._tmux(
                    "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}\t#{pane_pid}"
                )
            except Exception as exc:
                if not self._ever_saw_server:
                    # Fresh boot, no server: genuinely nothing running.
                    return {}
                if not self._panes and _is_no_server(exc):
                    # Server exited after the last session was stopped
                    # (``exit-empty``): zero sessions is the truth, and
                    # there is no last-known-good to defer over.
                    self._panes_at = time.monotonic()
                    return {}
                raise TmuxUnavailable(self._panes, exc) from exc

            panes: dict[str, PaneState] = {}
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                name, dead_flag, pid_raw = parts
                try:
                    pid = int(pid_raw)
                except ValueError:
                    continue
                dead = dead_flag == "1"
                prev = panes.get(name)
                if prev is None:
                    panes[name] = PaneState(session_name=name, dead=dead, pid=pid)
                elif prev.dead and not dead:
                    # Any live pane keeps the session alive.
                    panes[name] = PaneState(session_name=name, dead=False, pid=pid)
            self._panes = panes
            self._panes_at = time.monotonic()
            self._ever_saw_server = True
            return dict(panes)

    def mark_server_seen(self) -> None:
        """Record that a server exists (called after a successful create).

        From this point on, "no server" is an *anomaly* to defer on, not a
        fresh boot to report as empty.
        """
        self._ever_saw_server = True

    # -- processes ---------------------------------------------------------

    async def procs(self) -> dict[int, Proc] | None:
        """PID → process, or ``None`` when ``ps`` failed (optimistic-alive)."""
        async with self._lock:
            if time.monotonic() - self._procs_at < self._ttl:
                return dict(self._procs) if self._procs is not None else None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ps",
                    "-eo",
                    "pid,ppid,comm,args",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode != 0:
                    raise RuntimeError(f"ps exited {proc.returncode}")
            except Exception as exc:
                logger.warning("ps failed (%s) — process liveness is unknown, not dead", exc)
                self._procs = None
                self._procs_at = time.monotonic()
                return None

            procs: dict[int, Proc] = {}
            for line in out.decode(errors="replace").splitlines()[1:]:
                fields = line.split(None, 3)
                if len(fields) < 3:
                    continue
                try:
                    pid, ppid = int(fields[0]), int(fields[1])
                except ValueError:
                    continue
                procs[pid] = Proc(
                    pid=pid,
                    ppid=ppid,
                    comm=fields[2],
                    args=fields[3] if len(fields) > 3 else "",
                )
            self._procs = procs
            self._procs_at = time.monotonic()
            return dict(procs)

    @staticmethod
    def descendants(procs: dict[int, Proc], root: int) -> list[Proc]:
        """*root*'s subtree including *root* itself, from one snapshot.

        Walks ``ppid`` links rather than trusting ``pane_current_command``
        — systemd can move panes into ``tmux-spawn-*.scope`` units where
        the pane's visible command is not the agent.
        """
        children: dict[int, list[Proc]] = {}
        for p in procs.values():
            children.setdefault(p.ppid, []).append(p)
        out: list[Proc] = []
        if root in procs:
            out.append(procs[root])
        frontier = [root]
        while frontier:
            pid = frontier.pop()
            for child in children.get(pid, []):
                out.append(child)
                frontier.append(child.pid)
        return out
