"""The order Tidy seeds a rank with (reorganisation design §3.2).

Rank-0 siblings were seeded by ``(created_at, id)`` alone, so the newest
work sorted last; at the root, where every edge-free child shares rank 0,
"last" means "furthest down". The seed key here puts declared phases in
phase order, then finished work at the top, then the running band, then
everything still to do, and falls back to ``(created_at, id)`` — the
operator's model, so completed work collects at the top and the active band
moves down as work completes. A scope whose siblings are all one class is
ordered exactly as before.

This is the *seed* only. Nothing here runs on the incremental path, which
still appends new work at the end of its rank (§3.2, §6.1).
"""

from __future__ import annotations

from collections.abc import Mapping

from src.task_graph.layout.constants import FINISHED_STATUSES, RUNNING_STATUSES
from src.task_graph.layout.model import SnapTask

#: Sorts after every real phase order, so a non-phase sibling never jumps
#: ahead of a phase.
NO_PHASE: int = 1 << 30

FINISHED: int = 0
RUNNING: int = 1
UNFINISHED: int = 2


def activity_class(task: SnapTask, agg: Mapping[str, int] | None = None) -> int:
    """0 = finished, 1 = running, 2 = unfinished. Lower sorts first.

    The operator's model (roadmap decisions 2026-09-22, OD5 Q1): finished
    work rises to the top of its rank, the running band moves down as work
    completes, and the to-do sits at the bottom.

    A container is classed by its subtree rollup: finished (an all-done
    subtree — including a finished-but-context stub) is class 0,
    ``agg["running"] > 0`` is class 1, else ``agg["active"] > 0`` is class
    2. A leaf is classed by its own status against ``FINISHED_STATUSES`` /
    ``RUNNING_STATUSES`` — the driver hands the engine an aggregate for
    every child, and a leaf's is all zeros, so a leaf's class comes from
    its status alone.

    **Only a leaf's own ``ASSIGNED``/``IN_PROGRESS``, or a container's
    ``agg["running"] > 0``, can earn class 1.** A container with nothing to
    roll up — an empty phase, a freshly created standing parent, an epic
    emptied by reparenting, or any container when no aggregate was supplied
    — is class 2 (to-do), or class 0 if its own status is finished. A
    container's own ``IN_PROGRESS`` is not evidence of running work:
    containers are forced straight to ``IN_PROGRESS`` on release
    (``_release_ready_containers``), so classing one 1 would pull an empty
    phase into the running band.

    Note the deliberate disagreement with ``_visible`` (``driver.py``,
    "An UNFINISHED container whose descendants have all finished is still
    live work and keeps its stub"): that rule decides *presence* in the
    ``active`` variant and errs toward keeping context on the canvas, while
    this one decides *order* and sinks a container with no unfinished work
    left in it. Both are intended; changing one to match the other is not a
    fix.
    """
    if task.is_container:
        if agg is not None and agg.get("descendants", 0) > 0:
            if agg.get("running", 0) > 0:
                return RUNNING
            return UNFINISHED if agg.get("active", 0) > 0 else FINISHED
        return FINISHED if task.status in FINISHED_STATUSES else UNFINISHED
    if task.status in RUNNING_STATUSES:
        return RUNNING
    if task.status in FINISHED_STATUSES:
        return FINISHED
    return UNFINISHED


def tidy_seed_key(
    task: SnapTask, agg: Mapping[str, int] | None = None
) -> tuple[int, int, float, str]:
    """``(phase_order or NO_PHASE, activity_class, created_at, id)``."""
    phase_order = NO_PHASE if task.phase_order is None else task.phase_order
    return (phase_order, activity_class(task, agg), task.created_at, task.id)
