"""The order Tidy seeds a rank with (reorganisation design §3.2).

Rank-0 siblings used to be seeded by ``(created_at, id)`` alone, so the
newest work — usually the work being done — sorted last, and at the root,
where every edge-free child shares rank 0 and a banded epic owns a whole
line, "last" means "furthest down". The seed key here puts declared phases
in phase order, then running work, then everything unfinished, then
finished work, and falls back to ``(created_at, id)`` — so a scope whose
siblings are all one class is ordered exactly as it is today.

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

RUNNING: int = 0
UNFINISHED: int = 1
FINISHED: int = 2


def activity_class(task: SnapTask, agg: Mapping[str, int] | None = None) -> int:
    """0 = running, 1 = unfinished, 2 = finished. Lower sorts first.

    A container is classed by its subtree rollup: ``agg["running"] > 0`` is
    class 0, else ``agg["active"] > 0`` is class 1, else class 2. A leaf is
    classed by its own status against ``RUNNING_STATUSES`` /
    ``FINISHED_STATUSES`` — the driver hands the engine an aggregate for
    every child, and a leaf's is all zeros, which would otherwise read as
    "finished". A container with nothing under it yet (an empty phase, a
    freshly created standing parent) has nothing to roll up and is classed
    by its own status for the same reason.
    """
    if task.is_container and agg is not None and agg.get("descendants", 0) > 0:
        if agg.get("running", 0) > 0:
            return RUNNING
        return UNFINISHED if agg.get("active", 0) > 0 else FINISHED
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
