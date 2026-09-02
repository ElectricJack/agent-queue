"""Attribute load back to the sessions causing it (``/proc``, POSIX only).

A doctor check that says "load is 61" is not actionable; one that says
"load is 61 — 96 pytest processes under slot-3 (task prime-ember) and
slot-7 (task noble-pinnacle)" is.  Sessions are identified two ways, in
order of trust:

1. ``AQ_TASK_ID`` / ``AQ_SESSION_NAME`` from ``/proc/<pid>/environ`` — set
   by :func:`src.sessions.env.build_session_env` on every launch, so it is
   present on the harness and inherited by everything it spawns.
2. The worktree slot in the process's ``cwd`` — a fallback for anything
   started outside a session (a human's shell in the same worktree).

**Counting test processes means walking the tree, not matching a name.**
A ``pytest -n 24`` run is one controller plus 24 ``execnet`` workers, and a
worker's command line is::

    python -u -c import sys;exec(eval(sys.stdin.readline()))

— no "pytest" anywhere in it.  Matching on the name alone reports 1 where
the truth is 25, which would have made the pressure check useless for
exactly the fan-out it exists to catch.  :func:`pytest_processes` therefore
matches controllers by name and then adds their descendants.

``/proc`` reads of other users' processes fail with ``EACCES``; every read
here treats that as "unknown" rather than an error, because a partial
attribution is still useful.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProcInfo",
    "scan_processes",
    "pytest_processes",
    "load_average",
    "summarize_by_session",
]

_PROC = Path("/proc")
_SLOT_RE = re.compile(r"/\.aq/worktrees/(?P<slot>[A-Za-z0-9_-]+)")

#: Env keys read for attribution.  Kept short: ``/proc/<pid>/environ`` is
#: read only for processes that already matched, never for the whole table.
_ATTRIBUTION_KEYS = ("AQ_TASK_ID", "AQ_SESSION_NAME")


@dataclass(frozen=True)
class ProcInfo:
    """One process, enough of it to name a culprit."""

    pid: int
    comm: str
    cmdline: str
    ppid: int = 0
    cwd: str | None = None
    task_id: str | None = None
    session: str | None = None

    @property
    def slot(self) -> str | None:
        match = _SLOT_RE.search(self.cwd or "")
        return match.group("slot") if match else None

    @property
    def label(self) -> str:
        """Best available human name for whoever owns this process."""
        parts = [p for p in (self.slot, self.task_id, self.session) if p]
        return " / ".join(dict.fromkeys(parts)) if parts else f"pid {self.pid}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_ppid(pid: int) -> int:
    """Field 4 of ``/proc/<pid>/stat``, bounded by the last ``)``.

    ``comm`` may itself contain spaces and parentheses, which is why the
    split is anchored on ``rfind(")")`` rather than on whitespace.
    """
    raw = _read_text(_PROC / str(pid) / "stat")
    if not raw:
        return 0
    rparen = raw.rfind(")")
    if rparen < 0:
        return 0
    fields = raw[rparen + 2 :].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def _read_env_keys(pid: int, keys: tuple[str, ...] = _ATTRIBUTION_KEYS) -> dict[str, str]:
    raw = _read_text(_PROC / str(pid) / "environ")
    if raw is None:
        return {}
    found: dict[str, str] = {}
    for chunk in raw.split("\0"):
        key, sep, value = chunk.partition("=")
        if sep and key in keys:
            found[key] = value
    return found


def _cmdlines() -> dict[int, tuple[int, str]]:
    """``{pid: (ppid, cmdline)}`` for every readable process.

    The cheap pass: two small reads per process and no ``environ``, so
    scanning a 2,000-process box stays in the low tens of milliseconds.
    """
    table: dict[int, tuple[int, str]] = {}
    if not _PROC.is_dir():  # pragma: no cover - non-Linux
        return table
    try:
        pids = [int(p.name) for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError:  # pragma: no cover - /proc vanished
        return table
    for pid in pids:
        raw = _read_text(_PROC / str(pid) / "cmdline")
        if raw is None:
            continue
        cmdline = " ".join(part for part in raw.split("\0") if part)
        table[pid] = (_read_ppid(pid), cmdline)
    return table


def _enrich(pid: int, ppid: int, cmdline: str) -> ProcInfo:
    """The expensive pass, run only for processes we are going to report."""
    try:
        cwd = os.readlink(_PROC / str(pid) / "cwd")
    except OSError:
        cwd = None
    env = _read_env_keys(pid)
    return ProcInfo(
        pid=pid,
        comm=(_read_text(_PROC / str(pid) / "comm") or "").strip(),
        cmdline=cmdline,
        ppid=ppid,
        cwd=cwd,
        task_id=env.get("AQ_TASK_ID") or None,
        session=env.get("AQ_SESSION_NAME") or None,
    )


def _descendants(roots: set[int], table: dict[int, tuple[int, str]]) -> set[int]:
    """Every pid whose ancestry reaches *roots*."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, ()):
            if child not in seen and child not in roots:
                seen.add(child)
                stack.append(child)
    return seen


def scan_processes(match: str | None = None, *, include_children: bool = False) -> list[ProcInfo]:
    """Readable processes, optionally filtered to cmdlines containing *match*.

    *match* is a plain substring, not a regex: callers want "pytest", and a
    regex here would be a way to accidentally match nothing.
    ``include_children`` adds every descendant of a matched process, which
    is how worker pools that do not carry the tool's name in their command
    line get counted.
    """
    table = _cmdlines()
    if match is None:
        selected = set(table)
    else:
        selected = {pid for pid, (_, cmd) in table.items() if match in cmd}
        if include_children:
            selected |= _descendants(selected, table)
    return [_enrich(pid, *table[pid]) for pid in sorted(selected) if pid in table]


def pytest_processes() -> list[ProcInfo]:
    """Every pytest process on the box, xdist workers included."""
    return scan_processes("pytest", include_children=True)


def summarize_by_session(procs: list[ProcInfo]) -> list[dict]:
    """Group *procs* by owner label, busiest first."""
    buckets: dict[str, list[ProcInfo]] = {}
    for proc in procs:
        buckets.setdefault(proc.label, []).append(proc)
    rows = [
        {
            "owner": label,
            "count": len(items),
            "slot": items[0].slot,
            "task_id": items[0].task_id,
            "session": items[0].session,
            "pids": sorted(p.pid for p in items)[:10],
        }
        for label, items in buckets.items()
    ]
    rows.sort(key=lambda r: (-r["count"], r["owner"]))
    return rows


def load_average() -> tuple[float, float, float] | None:
    """``(1m, 5m, 15m)`` load, or ``None`` where the kernel has no notion."""
    try:
        return os.getloadavg()
    except (OSError, AttributeError):  # pragma: no cover - non-POSIX
        return None
