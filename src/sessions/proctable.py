"""Process-table scanning and fenced kills (POSIX ``/proc``).

Three jobs:

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

``kill_marked``
    Signal every process carrying an env marker value, plus descendants.
    ``kill_tree`` walks parent PIDs, and a detached shell (every harness
    Bash call runs under ``setsid``) is reparented to init the moment its
    harness exits — after which no ppid walk from the pane reaches it.  The
    session's ``AQ_INSTANCE_TOKEN`` is inherited by everything it spawns,
    so the marker still names it.  An ``aq test`` run that outlived its
    session this way kept a box-wide test slot for over an hour.

Everything here does blocking ``/proc`` reads, so the public functions are
async wrappers over :func:`asyncio.to_thread` — never call the ``_sync``
internals from the event loop.  The small ``*_sync`` public surface at the
end of the module exists for synchronous callers that own no event loop
(``aq test`` and its slot reaper, :mod:`src.resources.test_runs`).
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

__all__ = [
    "ProcEntry",
    "file_holders_sync",
    "is_alive_sync",
    "kill_marked",
    "kill_marked_sync",
    "kill_tree",
    "marked_root_sync",
    "read_entry_sync",
    "read_environ_sync",
    "read_start_ticks",
    "scan_by_env_marker",
]

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
    """True while *entry* is running.  A zombie has already exited."""
    try:
        raw = (_PROC / str(entry.pid) / "stat").read_bytes().decode("ascii", errors="replace")
    except OSError:
        return False
    rest = raw[raw.rfind(")") + 2 :].split()
    try:
        return int(rest[19]) == entry.start_ticks and rest[0] not in ("Z", "X")
    except (IndexError, ValueError):
        return False


def _protected_pids_sync() -> set[int]:
    """This process and its ancestors: a sweep must never signal them.

    A daemon launched from inside a session used to inherit that session's
    ``AQ_INSTANCE_TOKEN`` (``aq start`` now strips it), and a sweep that
    matched the daemon would take down every agent it supervises.
    """
    protected: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in protected:
        protected.add(pid)
        stat = _read_stat_sync(pid)
        if stat is None:
            break
        pid = stat[0]
    return protected


def _descendants_of_sync(roots: list[ProcEntry], everything: list[ProcEntry]) -> list[ProcEntry]:
    children: dict[int, list[ProcEntry]] = {}
    for e in everything:
        children.setdefault(e.ppid, []).append(e)
    seen = {r.pid for r in roots}
    out: list[ProcEntry] = []
    frontier = [r.pid for r in roots]
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            out.append(child)
            frontier.append(child.pid)
    return out


def _terminate_sync(targets: list[ProcEntry], *, grace: float, instance_token: str | None) -> None:
    """SIGTERM *targets*, wait *grace*, SIGKILL survivors, wait for them to go."""
    for entry in targets:
        _signal_sync(entry, signal.SIGTERM, instance_token)
    deadline = time.monotonic() + max(grace, 0.0)
    while time.monotonic() < deadline and any(_alive_sync(e) for e in targets):
        time.sleep(0.1)
    survivors = [e for e in targets if _alive_sync(e)]
    for entry in survivors:
        _signal_sync(entry, signal.SIGKILL, instance_token)
    deadline = time.monotonic() + 1.0
    while survivors and time.monotonic() < deadline:
        survivors = [e for e in survivors if _alive_sync(e)]
        if survivors:
            time.sleep(0.05)


def kill_marked_sync(
    key: str,
    value: str,
    *,
    roots: list[ProcEntry] | tuple[ProcEntry, ...] = (),
    grace: float = 2.0,
    instance_token: str | None = None,
) -> list[ProcEntry]:
    """Terminate every process whose env has ``key == value``, plus descendants.

    *roots* are extra processes (already fenced by their start time) to
    take down with their subtrees — the ``aq test`` wrapper, which does not
    itself carry the per-run marker its pytest children do.  Returns the
    processes that were signalled.

    Never signals this process, its ancestors, or a tmux server: the server
    hosts every other agent's session, so taking it down is never the
    right repair for one session's leftovers.  An empty *value* matches
    nothing.
    """
    if not value and not roots:
        return []
    everything = _scan_sync(None)
    marked = [e for e in _scan_sync(key) if e.marker == value] if value else []
    protected = _protected_pids_sync()

    def spared(e: ProcEntry) -> bool:
        return e.pid in protected or e.comm.startswith("tmux")

    # Filter the seeds *before* expanding them: a marked tmux server's
    # subtree is every other session on the box.
    seeds = [e for e in {e.pid: e for e in [*roots, *marked]}.values() if not spared(e)]
    targets = [*seeds, *(e for e in _descendants_of_sync(seeds, everything) if not spared(e))]
    if targets:
        _terminate_sync(targets, grace=grace, instance_token=instance_token)
    return targets


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


async def kill_marked(instance_token: str, *, grace: float = 2.0) -> list[ProcEntry]:
    """Terminate everything still carrying a session's ``AQ_INSTANCE_TOKEN``.

    The sweep :meth:`SessionProvider.stop` runs after (and independently of)
    its ``kill_tree`` on the pane: whatever the session left behind under
    init — a background shell, an ``aq test`` run holding a test slot — is
    still marked with the token.  The token is minted per launch, so a
    same-named successor is never matched.
    """
    if not instance_token:
        return []
    return await asyncio.to_thread(
        kill_marked_sync,
        _TOKEN_KEY,
        instance_token,
        grace=grace,
        instance_token=instance_token,
    )


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


# ---------------------------------------------------------------------------
# Sync surface — for callers that own no event loop
# ---------------------------------------------------------------------------


def read_entry_sync(pid: int) -> ProcEntry | None:
    """*pid* as a fenceable :class:`ProcEntry`, or ``None`` when it is gone."""
    stat = _read_stat_sync(pid)
    if stat is None:
        return None
    ppid, comm, start_ticks = stat
    return ProcEntry(pid=pid, ppid=ppid, comm=comm, start_ticks=start_ticks)


def is_alive_sync(entry: ProcEntry) -> bool:
    """True while *entry* (pid + start time) is still the running process."""
    return _alive_sync(entry)


def read_environ_sync(pid: int) -> dict[str, str] | None:
    """*pid*'s environment, or ``None`` when unreadable or gone."""
    return _read_environ_sync(pid)


def marked_root_sync(pid: int, key: str, value: str) -> ProcEntry | None:
    """The topmost ancestor-or-self of *pid* whose env has ``key == value``.

    Walks up while each parent still carries the same value.  For a
    session's ``AQ_INSTANCE_TOKEN`` that is the harness process in the pane
    (its parent, the tmux server, carries no token) — or, once the harness
    has exited, the reparented leftover itself.  ``None`` when *pid* does
    not carry the value.
    """
    root: ProcEntry | None = None
    current = pid
    seen: set[int] = set()
    while current > 0 and current not in seen:
        seen.add(current)
        env = _read_environ_sync(current)
        if env is None or env.get(key) != value:
            break
        root = read_entry_sync(current)
        if root is None:
            break
        current = root.ppid
    return root


def file_holders_sync(path: str | os.PathLike[str]) -> list[ProcEntry]:
    """Every readable process with a descriptor open on *path*.

    For an ``flock`` file this is exactly who keeps the lock held: the lock
    lives on the open file description, so it survives until the last of
    these processes closes it — including a child that inherited the
    descriptor after the process that took the lock was killed.
    """
    try:
        target = os.stat(path)
    except OSError:
        return []
    holders: list[ProcEntry] = []
    try:
        pids = [int(p.name) for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return holders
    for pid in pids:
        fd_dir = _PROC / str(pid) / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(fd)
            except OSError:
                continue
            if st.st_ino == target.st_ino and st.st_dev == target.st_dev:
                entry = read_entry_sync(pid)
                if entry is not None:
                    holders.append(entry)
                break
    return holders
