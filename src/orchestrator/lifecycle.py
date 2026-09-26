"""Lifecycle sweep: re-check stale BLOCKED/PAUSED tasks, retry obsolete cleanup.

Restarts, updates and holds leave tasks sitting in BLOCKED or PAUSED long after
the reason they stopped has gone away, and nothing looks at them again: the
promotion cascade reads only the persisted ``is_blocked`` projection, and a
terminal or operator decision is never revisited.  On 2026-09-26 finished and
superseded work lingered like that for 10-13 hours.

Every ``work_graph.lifecycle_sweep_interval_seconds`` this sweep takes each
BLOCKED or PAUSED task unchanged for ``work_graph.stale_open_after_seconds``
and recomputes why it is stopped:

* **BLOCKED** -- the projection is recomputed.  A task whose graph blockers
  (dependencies, gates) are all satisfied now -- the dependency completed or
  was delivered -- goes back to READY.  Anything else keeps its status.
* **PAUSED** -- a pause with no timer and no operator-hold snapshot (the hold is
  gone) is resumed through ``recover_orphaned_pause``.  An expired timer is
  left to the backoff sweep.  An operator hold or a running timer stays put.

A task that stays stopped is flagged ``needs_attention=stale_open`` and its
project supervisor gets one inbox message per stale episode
(:meth:`~src.database.queries.lifecycle_queries.LifecycleQueryMixin.flag_stale_open`).
The flag is advisory. It never holds a task out of promotion, and any
transition out of BLOCKED/PAUSED removes it.  A task that already carries
another ``needs_attention`` code is left to whoever raised it.

The sweep makes no LLM call and takes no judgment: deciding what a stale task
should become is the supervisor's.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.database.queries.task_queries import STALE_OPEN_ATTENTION, TERMINAL_BLOCKED_META_KEY
from src.models import Task, TaskStatus

logger = logging.getLogger(__name__)

#: How many unmet blockers a stale-open detail names.
_BLOCKER_LIMIT = 20


class LifecycleMixin:
    """The lifecycle sweep, mixed into the Orchestrator."""

    _last_lifecycle_sweep: float = 0.0

    async def _sweep_lifecycle(self) -> None:
        """Run the stale-open re-evaluation and obsolete-cleanup retries on cadence."""
        interval = self.config.work_graph.lifecycle_sweep_interval_seconds
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_lifecycle_sweep < interval:
            return
        self._last_lifecycle_sweep = now
        try:
            await self.reevaluate_stale_open(now=now)
        except Exception:
            logger.exception("Stale-open re-evaluation failed")
        try:
            await self.retry_obsolete_cleanup()
        except Exception:
            logger.exception("Obsolete-close cleanup retry failed")

    async def reevaluate_stale_open(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Unblock or flag every BLOCKED/PAUSED task older than the threshold.

        Returns ``{"settled", "unblocked", "flagged"}`` task ids.  Settled are
        stale containers completed first (:meth:`reconcile_stale_containers`),
        so a finished epic is completed rather than flagged.
        """
        result: dict[str, list[str]] = {"settled": [], "unblocked": [], "flagged": []}
        threshold = self.config.work_graph.stale_open_after_seconds
        if threshold <= 0:
            return result
        now = time.time() if now is None else now
        result["settled"] = await self.reconcile_stale_containers()
        cutoff = now - threshold
        stale = [
            task
            for status in (TaskStatus.BLOCKED, TaskStatus.PAUSED)
            for task in await self.db.list_tasks(status=status)
            if task.updated_at and task.updated_at <= cutoff
        ]
        if not stale:
            return result
        codes = await self.db.get_task_meta_bulk([task.id for task in stale], "needs_attention")
        for task in stale:
            code = codes.get(task.id)
            if code is not None and code != STALE_OPEN_ATTENTION:
                continue
            try:
                if task.status == TaskStatus.BLOCKED:
                    verdict = await self._recheck_stale_blocked(task, now)
                else:
                    verdict = await self._recheck_stale_paused(task, now)
            except Exception:
                logger.exception("Stale-open re-check of %s failed", task.id)
                continue
            if verdict is None:
                continue
            outcome, current, detail = verdict
            if outcome == "unblocked":
                result["unblocked"].append(task.id)
                logger.info("Stale %s task %s unblocked: %s", task.status.value, task.id, detail)
                continue
            if code is not None:
                continue  # already flagged for this episode
            detail["stale_hours"] = round((now - current.updated_at) / 3600, 1)
            detail["checked_at"] = now
            if await self.db.flag_stale_open(
                task.id,
                status=current.status.value,
                updated_at=current.updated_at,
                detail=detail,
            ):
                result["flagged"].append(task.id)
                logger.warning(
                    "Task %s has been %s for %sh with its blocker in place: flagged stale_open",
                    task.id,
                    current.status.value,
                    detail["stale_hours"],
                )
                try:
                    await self.bus.emit(
                        "task.needs_attention",
                        {
                            "task_id": task.id,
                            "project_id": task.project_id,
                            "title": task.title,
                            "reason": STALE_OPEN_ATTENTION,
                        },
                    )
                except Exception:
                    logger.debug("task.needs_attention emit failed for %s", task.id, exc_info=True)
        return result

    async def _recheck_stale_blocked(
        self, task: Task, now: float
    ) -> tuple[str, Task, Any] | None:
        """Recompute why *task* is BLOCKED; unblock it when the blocker is gone.

        ``None`` when the task left BLOCKED meanwhile.  A terminal close keeps
        its status: that decision is the supervisor's, not the graph's.
        """
        async with self.db._engine.begin() as conn:
            flipped = await self.db.recompute_blocked({task.id}, conn=conn)
        if flipped:
            await self.db.log_blocked_flips(flipped)
            await self._emit_blocked_flips(flipped, reason="stale_open_recheck")
        current = await self.db.get_task(task.id)
        if current is None or current.status != TaskStatus.BLOCKED:
            return None
        terminal = await self.db.get_task_meta(task.id, TERMINAL_BLOCKED_META_KEY)
        graph = task.id in await self.db.tasks_with_graph_blockers([task.id])
        if terminal is None and graph and not current.is_blocked:
            await self.db.transition_task(
                task.id, TaskStatus.READY, context="stale_open_recheck"
            )
            await self._release_ready_containers([task.id])
            return "unblocked", current, "every blocking dependency and gate is satisfied"
        blockers = await self.db.get_blocking_dependencies(task.id)
        if terminal is not None:
            reason = f"terminal close ({terminal}); only a recovery decision reopens it"
        elif blockers:
            reason = f"waiting on {len(blockers)} unmet dependenc{'y' if len(blockers) == 1 else 'ies'}"
        elif current.is_blocked:
            reason = "an open gate"
        else:
            reason = "BLOCKED with no graph blocker and no recorded terminal close"
        return "flag", current, {
            "status": TaskStatus.BLOCKED.value,
            "since": current.updated_at,
            "reason": reason,
            "terminal": terminal,
            "blockers": [
                {"task_id": dep_id, "status": dep_status, "dep_type": dep_type}
                for dep_id, _title, dep_status, dep_type, _project in blockers[:_BLOCKER_LIMIT]
            ],
        }

    async def _recheck_stale_paused(
        self, task: Task, now: float
    ) -> tuple[str, Task, Any] | None:
        """Resume *task* when its hold is gone; otherwise describe the hold."""
        if task.resume_after is not None and task.resume_after <= now:
            return None  # the backoff sweep resumes it this cycle
        if task.resume_after is None:
            snapshot = await self.db.get_task_meta(task.id, "manual_pause")
            if snapshot is None:
                resumed = await self.db.recover_orphaned_pause(task.id)
                if resumed is not None:
                    return "unblocked", resumed, "PAUSED with no timer and no operator hold"
                return None
            prior = snapshot.get("status") if isinstance(snapshot, dict) else None
            reason = f"operator hold (paused from {prior or 'unknown'}); only a resume ends it"
            hold = {"kind": "manual_pause", "paused_from": prior}
        else:
            reason = "backoff timer still running"
            hold = {"kind": "backoff", "resume_after": task.resume_after}
        current = await self.db.get_task(task.id)
        if current is None or current.status != TaskStatus.PAUSED:
            return None
        return "flag", current, {
            "status": TaskStatus.PAUSED.value,
            "since": current.updated_at,
            "reason": reason,
            "hold": hold,
        }
