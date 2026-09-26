"""Attribute ``aq test`` slot holders to sessions, and reap the orphans.

The slot semaphore is crash-safe by construction — the kernel drops an
``flock`` when its last descriptor closes — but it cannot tell a *wanted*
run from an *unowned* one.  On 2026-09-24 three stopped tasks' full-suite
runs outlived their sessions and held three of the box's four test slots
for well over an hour: each ran from a detached Bash-tool shell, which was
reparented to init when its harness exited, so nothing that stopped the
session reached it.  Stopping a session now sweeps its leftovers
(:func:`src.sessions.proctable.kill_marked`); this module is the backstop
for runs that escaped anyway, and for runs started before that sweep.

Attribution
    ``aq test`` records, beside the holder pid (:func:`holder_identity`):
    the session id and name; the task id — from the claim file for a pool
    worker, whose environment carries no ``AQ_TASK_ID``; ``session_root``,
    the pid and start time of the session's harness process (the topmost
    ancestor carrying the same ``AQ_INSTANCE_TOKEN``); a digest of that
    token; and ``test_run_id``, the ``AQ_TEST_RUN_ID`` every pytest child
    inherits.

Verdicts (:func:`held_slots`)
    * ``live`` — the harness is still running.  **Never reaped**, whatever
      its task's status: a session whose claim was reclaimed still owns its
      processes, and stopping that session is what sweeps them.
    * ``orphaned`` — the harness has exited.  A record written before
      attribution existed has no ``session_root``; for those the run is
      orphaned only when every session-marked process keeping the lock has
      been reparented to init (pid 1) — a conservative reading that leaves
      a run adopted by any other subreaper alone.
    * ``unattributed`` — no session identity at all (a human's shell, CI).
      Reported, never reaped.

Reaping (:func:`reap_orphans`, dry-run unless ``apply``)
    Every process that keeps the slot file open *and* belongs to the dead
    session, every process carrying the run's ``AQ_TEST_RUN_ID``, and their
    descendants get ``SIGTERM``, then ``SIGKILL`` after a grace period; the
    lock is then re-tested.  The verdict is re-derived immediately before
    any signal.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.resources.semaphore import SlotSemaphore
from src.sessions.proctable import (
    ProcEntry,
    file_holders_sync,
    is_alive_sync,
    kill_marked_sync,
    marked_root_sync,
    read_entry_sync,
    read_environ_sync,
)

__all__ = [
    "LIVE",
    "ORPHANED",
    "RUN_ID_KEY",
    "UNATTRIBUTED",
    "HeldSlot",
    "held_slots",
    "holder_identity",
    "reap_orphans",
]

LIVE = "live"
ORPHANED = "orphaned"
UNATTRIBUTED = "unattributed"

#: Set by ``aq test`` on the pytest child; inherited by every xdist worker.
RUN_ID_KEY = "AQ_TEST_RUN_ID"

_TOKEN_KEY = "AQ_INSTANCE_TOKEN"
_SLOT_FILE = re.compile(r"^slot-(\d+)\.lock$")

#: How long a reaped run gets between ``SIGTERM`` and ``SIGKILL``.
DEFAULT_GRACE = 5.0


def _token_digest(token: str) -> str:
    """Stable short digest: the lock file is world-readable, the token is not."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def holder_identity(
    *,
    test_run_id: str,
    environ: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Who is about to hold a test slot, for the slot record.

    Read from the calling process: its own start time fences the pid
    against recycling, and its ancestry names the session's harness.
    """
    from src.claim_file import read_claim_file

    env = os.environ if environ is None else environ
    cwd = cwd or os.getcwd()
    pid = os.getpid()
    me = read_entry_sync(pid)
    task_id = env.get("AQ_TASK_ID") or None
    if task_id is None:
        claim = read_claim_file(env.get("AQ_WORK_DIR") or cwd)
        task_id = (claim or {}).get("task_id") or None
    identity: dict = {
        "pid": pid,
        "pid_start": me.start_ticks if me is not None else None,
        "task_id": task_id,
        "session": env.get("AQ_SESSION_NAME") or None,
        "session_id": env.get("AQ_SESSION_ID") or None,
        "test_run_id": test_run_id,
        "cwd": cwd,
    }
    if env.get("AQ_JOB_ID") and env.get("AQ_JOB_NONCE"):
        identity.update(job_id=env["AQ_JOB_ID"], job_nonce=env["AQ_JOB_NONCE"])
    token = env.get(_TOKEN_KEY)
    if token:
        identity["session_token_sha"] = _token_digest(token)
        root = marked_root_sync(pid, _TOKEN_KEY, token)
        if root is not None:
            identity["session_root"] = {
                "pid": root.pid,
                "start": root.start_ticks,
                "comm": root.comm,
            }
    return identity


@dataclass(frozen=True)
class HeldSlot:
    """One held slot and the verdict on whoever holds it."""

    slot: int
    path: str
    holder: dict
    state: str
    reason: str

    @property
    def owner(self) -> str:
        """Best human name for the holder."""
        h = self.holder
        return str(h.get("task_id") or h.get("session") or h.get("cwd") or f"pid {h.get('pid')}")

    def to_dict(self) -> dict:
        since = self.holder.get("since")
        return {
            "job_id": self.holder.get("job_id"),
            "slot": self.slot,
            "state": self.state,
            "reason": self.reason,
            "owner": self.owner,
            "task_id": self.holder.get("task_id"),
            "session_id": self.holder.get("session_id"),
            "cwd": self.holder.get("cwd"),
            "pid": self.holder.get("pid"),
            "held_for_s": round(time.time() - since) if isinstance(since, (int, float)) else None,
        }


def _slot_files(lock_dir: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    try:
        entries = list(lock_dir.iterdir())
    except OSError:
        return found
    for path in entries:
        match = _SLOT_FILE.match(path.name)
        if match:
            found[int(match.group(1))] = path
    return found


def _recorded_root(holder: Mapping) -> ProcEntry | None:
    root = holder.get("session_root")
    if not isinstance(root, Mapping):
        return None
    pid, start = root.get("pid"), root.get("start")
    if not isinstance(pid, int) or not isinstance(start, int):
        return None
    return ProcEntry(pid=pid, ppid=0, comm=str(root.get("comm") or ""), start_ticks=start)


def _session_top(entry: ProcEntry) -> ProcEntry | None:
    """The topmost process of *entry*'s session, or ``None`` when unmarked."""
    env = read_environ_sync(entry.pid) or {}
    token = env.get(_TOKEN_KEY)
    if not token:
        return None
    return marked_root_sync(entry.pid, _TOKEN_KEY, token)


def _classify(path: Path, holder: Mapping) -> tuple[str, str]:
    if holder.get("job_id") and holder.get("job_nonce"):
        # The queue owns cleanup. Session orphan reaping must never cancel a
        # task-owned job when its submitter sleeps or its supervisor dies.
        for entry in file_holders_sync(path):
            env = read_environ_sync(entry.pid) or {}
            if (
                env.get("AQ_JOB_NONCE") == holder["job_nonce"]
                and env.get("AQ_JOB_ID") == holder["job_id"]
            ):
                return LIVE, f"managed job {holder['job_id']} retains execution capacity"
        return (
            UNATTRIBUTED,
            f"managed job {holder['job_id']}: jobs.cleanup_blocked; queue must reconcile",
        )
    root = _recorded_root(holder)
    session = holder.get("session_id") or "?"
    if root is not None:
        current = read_entry_sync(root.pid) if is_alive_sync(root) else None
        if current is None:
            return ORPHANED, f"session {session} harness (pid {root.pid}) has exited"
        if current.ppid == 1:
            # A run that queued from an already-orphaned shell recorded that
            # shell as its root; a harness is parented to tmux or the daemon.
            return ORPHANED, f"session {session} root (pid {root.pid}) was reparented to init"
        return LIVE, f"session {session} harness (pid {root.pid}) is running"
    # A record from before attribution: judge by the processes keeping the
    # lock.  Any of them still under a live session wins.
    tops = [top for top in map(_session_top, file_holders_sync(path)) if top is not None]
    if not tops:
        return UNATTRIBUTED, "no session marker on the holder; not reaped"
    live = [top for top in tops if top.ppid != 1]
    if live:
        return LIVE, f"session process {live[0].pid} ({live[0].comm}) is still parented"
    pids = ", ".join(str(top.pid) for top in tops)
    return ORPHANED, f"its session's processes ({pids}) were reparented to init"


def held_slots(lock_dir: str | os.PathLike[str]) -> list[HeldSlot]:
    """Every held slot in *lock_dir*, with a verdict on its holder.

    Globs the slot files rather than trusting a configured count, so a
    slot beyond a since-lowered ``test_slots`` is still seen.
    """
    lock_dir = Path(lock_dir)
    files = _slot_files(lock_dir)
    if not files:
        return []
    sem = SlotSemaphore(lock_dir, max(files) + 1)
    held: list[HeldSlot] = []
    for slot, path in sorted(files.items()):
        state = sem.state(slot)
        if not state.held:
            continue
        verdict, reason = _classify(path, state.holder)
        held.append(
            HeldSlot(slot=slot, path=str(path), holder=state.holder, state=verdict, reason=reason)
        )
    return held


def _belongs_to_run(entry: ProcEntry, holder: Mapping) -> bool:
    """Whether *entry*, which keeps the slot open, is part of the dead run.

    Anything else with the file open — a live session's waiter probing the
    slot, an ``--aq-status`` observer — is left alone.
    """
    env = read_environ_sync(entry.pid) or {}
    run_id = holder.get("test_run_id")
    if run_id and env.get(RUN_ID_KEY) == run_id:
        return True
    if entry.pid == holder.get("pid") and entry.start_ticks == holder.get("pid_start"):
        return True
    token = env.get(_TOKEN_KEY)
    if not token:
        return False
    digest = holder.get("session_token_sha")
    if digest:
        return _token_digest(token) == digest
    top = marked_root_sync(entry.pid, _TOKEN_KEY, token)
    return top is not None and top.ppid == 1


def _reap_one(sem: SlotSemaphore, slot: HeldSlot, *, grace: float) -> dict:
    row = slot.to_dict()
    # Re-derive right before signalling: the harness may have been adopted
    # or the slot released since the listing.
    current = sem.state(slot.slot)
    if not current.held:
        return {**row, "action": "already_free", "freed": True, "pids": []}
    verdict, reason = _classify(Path(slot.path), current.holder)
    if verdict != ORPHANED:
        return {
            **row,
            "state": verdict,
            "reason": reason,
            "action": "skipped",
            "freed": False,
            "pids": [],
        }
    holder = current.holder
    roots = [e for e in file_holders_sync(slot.path) if _belongs_to_run(e, holder)]
    run_id = holder.get("test_run_id") or ""
    if not roots and not run_id:
        return {**row, "action": "no_target", "freed": False, "pids": []}
    killed = kill_marked_sync(RUN_ID_KEY, run_id, roots=roots, grace=grace)
    freed = not sem.state(slot.slot).held
    return {
        **row,
        "action": "reaped" if freed else "reap_failed",
        "freed": freed,
        "pids": sorted(e.pid for e in killed),
    }


def reap_orphans(
    lock_dir: str | os.PathLike[str],
    *,
    apply: bool = False,
    grace: float = DEFAULT_GRACE,
) -> dict:
    """Report — and with *apply*, terminate — every orphaned test run.

    Returns ``{"lock_dir", "applied", "held", "orphaned", "results"}``:
    ``held`` is every held slot's verdict, ``orphaned`` the subset a reap
    targets, and ``results`` (empty on a dry run) one row per reap attempt
    with its ``action`` (``reaped``/``reap_failed``/``skipped``/
    ``already_free``/``no_target``) and the pids signalled.
    """
    slots = held_slots(lock_dir)
    orphaned = [s for s in slots if s.state == ORPHANED]
    results: list[dict] = []
    if apply and orphaned:
        sem = SlotSemaphore(lock_dir, max(s.slot for s in slots) + 1)
        results = [_reap_one(sem, s, grace=grace) for s in orphaned]
    return {
        "lock_dir": str(lock_dir),
        "applied": apply,
        "held": [s.to_dict() for s in slots],
        "orphaned": [s.to_dict() for s in orphaned],
        "results": results,
    }
