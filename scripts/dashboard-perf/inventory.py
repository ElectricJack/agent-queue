#!/usr/bin/env python3
"""Who is using the CPU: the top processes, classified by their AQ markers (spec §4.2).

The experiment takes one inventory before and one after each loaded
repetition, so the evidence can say whether the load that was on the box was
the sanctioned helper, a gated agent session or test run, a queued job, or
something nobody gated (an operator shell, a self-hosted CI runner).  The
markers come from ``/proc/<pid>/environ``, which every AQ launch sets and
every child inherits; ``AQ_INSTANCE_TOKEN`` — the session's kill fence — is
reported only as ``"set"``, and any DSN password in a command line is masked.

``pcpu`` is ``ps``'s figure: CPU time over the process's lifetime, not an
instantaneous rate.  The fleet series carries the per-second view.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.resources.procs import _SLOT_RE, _read_env_keys

MARKERS = ("AQ_TASK_ID", "AQ_SESSION_ID", "AQ_SESSION_NAME", "AQ_INSTANCE_TOKEN", "AQ_JOB_ID",
           "AQ_TEST_RUN_ID")
CLASSES = ("load_helper", "job", "session", "test_run", "ungated")
_SESSION_MARKERS = ("AQ_SESSION_ID", "AQ_SESSION_NAME", "AQ_INSTANCE_TOKEN", "AQ_TASK_ID")
_REDACTED_MARKERS = frozenset({"AQ_INSTANCE_TOKEN"})
_LOAD_HELPER_RE = re.compile(r"dashboard-perf/load\.py(?:\s|$)")
_DSN_PASSWORD_RE = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)[^/\s@]*(@)")


def classify(markers: Mapping[str, str], cmdline: str) -> str:
    """``load_helper`` | ``job`` | ``session`` | ``test_run`` | ``ungated``.

    The helper is named by its command line first: launched from a worker
    session it inherits that session's markers, yet it is the experiment's
    own load, not the session's work.  A job outranks the session that
    submitted it, and a session outranks an ``aq test`` run inside it (the
    run is part of the session's work).  No marker at all is ``ungated``.
    """
    if _LOAD_HELPER_RE.search(cmdline or ""):
        return "load_helper"
    if markers.get("AQ_JOB_ID"):
        return "job"
    if any(markers.get(key) for key in _SESSION_MARKERS):
        return "session"
    if markers.get("AQ_TEST_RUN_ID"):
        return "test_run"
    return "ungated"


def _redact_args(args: str) -> str:
    return _DSN_PASSWORD_RE.sub(r"\1***\2", args or "")


def _read_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(errors="replace").strip() or None
    except OSError:
        return None


def _read_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _read_markers(pid: int) -> dict[str, str]:
    return _read_env_keys(pid, MARKERS)


def _ps_rows(top: int) -> list[dict]:
    """The ``top`` busiest processes from ``ps`` (tooling, not daemon code)."""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,pcpu=,rss=,args=", "--sort=-pcpu"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    rows: list[dict] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pcpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        args = parts[4] if len(parts) > 4 else ""
        rows.append({"pid": pid, "ppid": ppid, "pcpu": pcpu, "rss_kb": rss,
                     "comm": _read_comm(pid) or (args.split()[0] if args else ""), "args": args})
        if len(rows) >= top:
            break
    return rows


def inventory(top: int = 40, *, rows: list[dict] | None = None,
              read_markers: Callable[[int], Mapping[str, str]] | None = None,
              read_cwd: Callable[[int], str | None] | None = None) -> dict:
    """The ``top`` busiest processes with markers, class and worktree slot, plus per-class totals."""
    rows = list(rows if rows is not None else _ps_rows(top))[:top]
    read_markers = read_markers or _read_markers
    read_cwd = read_cwd or _read_cwd
    processes: list[dict] = []
    totals: dict[str, dict] = {}
    for row in rows:
        pid = int(row["pid"])
        found = dict(read_markers(pid) or {})
        markers = {key: ("set" if key in _REDACTED_MARKERS else found[key])
                   for key in MARKERS if found.get(key)}
        args = str(row.get("args") or "")
        cls = classify(found, args)
        match = _SLOT_RE.search(read_cwd(pid) or "")
        pcpu = float(row.get("pcpu") or 0.0)
        processes.append({
            "pid": pid, "ppid": int(row.get("ppid") or 0), "pcpu": pcpu,
            "rss_kb": int(row.get("rss_kb") or 0), "comm": str(row.get("comm") or ""),
            "args": _redact_args(args), "markers": markers, "class": cls,
            "slot": match.group("slot") if match else None,
        })
        total = totals.setdefault(cls, {"count": 0, "pcpu": 0.0})
        total["count"] += 1
        total["pcpu"] = round(total["pcpu"] + pcpu, 1)
    return {"captured_at": time.time(), "processes": processes, "totals": totals}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=40, help="processes to list (default %(default)s)")
    args = parser.parse_args(argv)
    print(json.dumps(inventory(args.top), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
