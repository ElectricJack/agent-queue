"""Process-table scanning and fenced kills (POSIX ``/proc``).

Two jobs:

``scan_by_env_marker``
    Find every process carrying an ``AQ_*`` marker in its environment —
    the adoption path's ground truth.  Session names get reused and PIDs
    get recycled; the env marker plus the kernel's start time is the only
    identity that survives both.

``kill_tree``
    Signal a process and its descendants, *fenced*: before every signal
    the target's start time is re-read and compared against the value
    captured when the kill was decided, and (when available) its
    ``AQ_INSTANCE_TOKEN`` must match.  A recycled PID or a same-named
    successor is never hit.  This was one of the Gas City post-mortem's
    expensive lessons.

Everything here does blocking ``/proc`` reads, so the public functions are
async wrappers over :func:`asyncio.to_thread` — never call the ``_sync``
internals from the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ProcEntry", "scan_by_env_marker", "kill_tree", "read_start_ticks"]

_PROC = Path("/proc")

#: The env key every fenced kill compares.  Matches
#: :data:`src.sessions.env.AQ_MARKER_KEYS`.
_TOKEN_KEY = "AQ_INSTANCE_TOKEN"


@dataclass(frozen=True)
class ProcEntry:
    """One observed process, identified strongly enough to act on later."""

    pid: int
    ppid: int
    comm: str
    #: Kernel start time in clock ticks since boot (``/proc/<pid>/stat``
    #: field 22).  Together with the PID this survives PID recycling.
    start_ticks: int
    #: Value of the requested env marker, when the scan asked for one.
    marker: str | None = None


# ---------------------------------------------------------------------------
# Sync internals (thread-side)
# ---------------------------------------------------------------------------


def _read_stat_sync(pid: int) -> tuple[int, str, int] | None:
    """Return ``(ppid, comm, start_ticks)`` or ``None`` when gone."""
    try:
        raw = (_PROC / str(pid) / "stat").read_bytes().decode("ascii", errors="replace")
    except OSError:
        return None
    # comm may contain spaces/parens; it is bounded by the *last* ')'.
    lparen = raw.find("(")
    rparen = raw.rfind(")")
    if lparen < 0 or rparen < 0:
        return None
    comm = raw[lparen + 1 : rparen]
    rest = raw[rparen + 2 :].split()
    try:
        ppid = int(rest[1])  # field 4
        start_ticks = int(rest[19])  # field 22
    except (IndexError, ValueError):
        return None
    return ppid, comm, start_ticks


def _read_environ_sync(pid: int) -> dict[str, str] | None:
    """The process's environment, or ``None`` when unreadable."""
    try:
        raw = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    env: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            k, _, v = chunk.partition(b"=")
            env[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
    return env


def _scan_sync(marker_key: str | None) -> list[ProcEntry]:
    entries: list[ProcEntry] = []
    try:
        pids = [int(p.name) for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return entries
    for pid in pids:
        stat = _read_stat_sync(pid)
        if stat is None:
            continue
        ppid, comm, start_ticks = stat
        marker = None
        if marker_key is not None:
            env = _read_environ_sync(pid)
            if not env or marker_key not in env:
                continue
            marker = env[marker_key]
        entries.append(
            ProcEntry(pid=pid, ppid=ppid, comm=comm, start_ticks=start_ticks, marker=marker)
        )
    return entries


def _descendants_sync(root: int) -> list[ProcEntry]:
    """*root*'s subtree (excluding *root*), leaves last is not guaranteed."""
    everything = _scan_sync(None)
    children: dict[int, list[ProcEntry]] = {}
    for e in everything:
        children.setdefault(e.ppid, []).append(e)
    out: list[ProcEntry] = []
    frontier = [root]
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            out.append(child)
            frontier.append(child.pid)
    return out


def _fence_ok_sync(entry: ProcEntry, instance_token: str | None) -> bool:
    """Re-verify *entry* still names the same process we decided to kill."""
    stat = _read_stat_sync(entry.pid)
    if stat is None:
        return False  # already gone — nothing to signal
    _, _, start_ticks = stat
    if start_ticks != entry.start_ticks:
        logger.warning(
            "PID %d was recycled (start %d -> %d) — refusing to signal",
            entry.pid,
            entry.start_ticks,
            start_ticks,
        )
        return False
    if instance_token:
        env = _read_environ_sync(entry.pid)
        if env is not None and env.get(_TOKEN_KEY) not in (None, instance_token):
            logger.warning(
                "PID %d carries a different %s — refusing to signal", entry.pid, _TOKEN_KEY
            )
            return False
        # Unreadable environ (or a marker-less descendant like `sh`) falls
        # back to the start-time fence alone, which already passed.
    return True


def _signal_sync(entry: ProcEntry, sig: int, instance_token: str | None) -> None:
    if not _fence_ok_sync(entry, instance_token):
        return
    try:
        os.kill(entry.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _alive_sync(entry: ProcEntry) -> bool:
    stat = _read_stat_sync(entry.pid)
    return stat is not None and stat[2] == entry.start_ticks


# ---------------------------------------------------------------------------
# Async surface
# ---------------------------------------------------------------------------


async def scan_by_env_marker(marker_key: str) -> list[ProcEntry]:
    """Every live process whose environment carries *marker_key*."""
    return await asyncio.to_thread(_scan_sync, marker_key)


async def read_start_ticks(pid: int) -> int | None:
    """Kernel start time for *pid*, or ``None`` when the process is gone."""
    stat = await asyncio.to_thread(_read_stat_sync, pid)
    return None if stat is None else stat[2]


async def kill_tree(
    pid: int,
    *,
    instance_token: str | None = None,
    grace: float = 2.0,
) -> None:
    """SIGTERM *pid* + descendants, wait *grace*, SIGKILL survivors.

    Every signal is individually fenced by start-time (and, where the
    environ is readable, by ``AQ_INSTANCE_TOKEN``).  A *grace* of 2 s is
    deliberate — 100 ms orphans Claude.
    """
    stat = await asyncio.to_thread(_read_stat_sync, pid)
    if stat is None:
        return
    ppid, comm, start_ticks = stat
    root = ProcEntry(pid=pid, ppid=ppid, comm=comm, start_ticks=start_ticks)
    if not await asyncio.to_thread(_fence_ok_sync, root, instance_token):
        return

    targets = [root, *await asyncio.to_thread(_descendants_sync, pid)]

    for entry in targets:
        await asyncio.to_thread(_signal_sync, entry, signal.SIGTERM, instance_token)

    deadline = time.monotonic() + max(grace, 0.0)
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(lambda: any(_alive_sync(e) for e in targets)):
            return
        await asyncio.sleep(0.1)

    for entry in targets:
        if await asyncio.to_thread(_alive_sync, entry):
            await asyncio.to_thread(_signal_sync, entry, signal.SIGKILL, instance_token)
