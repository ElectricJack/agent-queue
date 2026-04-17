"""Orphan Workflow Recovery — detect and recover workflows whose playbook died.

Implements Roadmap 7.5.6: if a coordination playbook crashes mid-workflow,
in-flight tasks continue executing (they are independent entities), and the
playbook can be re-triggered to resume from the current state.

An "orphan workflow" is a workflow with ``status="running"`` whose associated
playbook run has terminated (failed, timed_out) or was lost entirely.  This
can happen when:

- The daemon crashes while a coordination playbook is executing or paused.
- A playbook run fails due to an LLM error, budget exhaustion, etc.
- The playbook run record is deleted while the workflow is still active.

Recovery operates at two levels:

1. **Startup recovery** — called once during ``Orchestrator.initialize()``
   after ``WorkflowStageResumeHandler`` is subscribed.  Scans for orphaned
   workflows and re-emits missed events or marks workflows for manual recovery.

2. **Periodic monitoring** — called from ``run_one_cycle()`` at a configurable
   interval (default 60s).  Detects orphans that arise during normal operation
   and emits ``workflow.orphaned`` events for alerting/automation.

See docs/specs/design/agent-coordination.md §11 Q2 and roadmap.md §7.5.6.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.database.base import DatabaseBackend
    from src.event_bus import EventBus
    from src.models import PlaybookRun, Workflow

logger = logging.getLogger(__name__)

# Default interval between periodic orphan checks (seconds).
_DEFAULT_CHECK_INTERVAL_S = 60

# Maximum age of a paused run before it's considered stale for recovery
# purposes.  Matches the WorkflowStageResumeHandler default (48h).
_MAX_PAUSE_AGE_S = 172800


class OrphanWorkflowRecovery:
    """Detect and recover orphaned coordination workflows.

    An orphan workflow is one whose coordination playbook run has died
    (failed, timed_out, deleted) while the workflow itself is still
    ``status="running"``.  Tasks within the workflow continue executing
    independently — this class handles re-establishing playbook control.

    Parameters
    ----------
    db:
        Database backend for querying workflows and playbook runs.
    event_bus:
        EventBus for emitting ``workflow.orphaned`` and re-emitting
        ``workflow.stage.completed`` events.
    check_interval_seconds:
        Minimum time between periodic orphan checks in ``run_one_cycle()``.
    """

    def __init__(
        self,
        *,
        db: DatabaseBackend,
        event_bus: EventBus,
        check_interval_seconds: int = _DEFAULT_CHECK_INTERVAL_S,
    ) -> None:
        self._db = db
        self._bus = event_bus
        self._check_interval = check_interval_seconds
        self._last_check_time: float = 0.0
        # Track workflows we've already reported as orphaned to avoid
        # spamming events every cycle.
        self._reported_orphans: set[str] = set()

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def recover_on_startup(self) -> dict[str, Any]:
        """Scan for orphaned workflows and attempt recovery.

        Called once during ``Orchestrator.initialize()`` after the
        ``WorkflowStageResumeHandler`` is subscribed (so re-emitted events
        will be caught).

        Returns a summary dict with counts of recovered/orphaned workflows.
        """
        summary: dict[str, Any] = {
            "checked": 0,
            "events_reemitted": 0,
            "marked_failed": 0,
            "orphaned": 0,
            "synced_completed": 0,
            "details": [],
        }

        try:
            running_workflows = await self._db.list_workflows(status="running")
        except Exception:
            logger.error(
                "Orphan recovery: failed to list running workflows",
                exc_info=True,
            )
            return summary

        summary["checked"] = len(running_workflows)
        if not running_workflows:
            logger.debug("Orphan recovery: no running workflows found")
            return summary

        for workflow in running_workflows:
            detail = await self._recover_workflow(workflow)
            summary["details"].append(detail)

            action = detail.get("action", "none")
            if action == "reemit_event":
                summary["events_reemitted"] += 1
            elif action == "mark_run_failed":
                summary["marked_failed"] += 1
            elif action == "orphaned":
                summary["orphaned"] += 1
            elif action == "sync_completed":
                summary["synced_completed"] += 1

        if summary["events_reemitted"] or summary["marked_failed"] or summary["orphaned"]:
            logger.info(
                "Orphan recovery complete: checked=%d, events_reemitted=%d, "
                "marked_failed=%d, orphaned=%d, synced_completed=%d",
                summary["checked"],
                summary["events_reemitted"],
                summary["marked_failed"],
                summary["orphaned"],
                summary["synced_completed"],
            )

        return summary

    async def _recover_workflow(self, workflow: Workflow) -> dict[str, Any]:
        """Attempt to recover a single running workflow.

        Returns a detail dict describing what action was taken.
        """
        wf_id = workflow.workflow_id
        run_id = workflow.playbook_run_id

        if not run_id:
            # Workflow has no playbook run — definitely orphaned
            logger.warning(
                "Orphan recovery: workflow '%s' has no playbook_run_id",
                wf_id,
            )
            await self._emit_orphaned(workflow, reason="no_playbook_run_id")
            return {"workflow_id": wf_id, "action": "orphaned", "reason": "no_playbook_run_id"}

        try:
            db_run = await self._db.get_playbook_run(run_id)
        except Exception:
            logger.error(
                "Orphan recovery: failed to fetch playbook run '%s' for workflow '%s'",
                run_id,
                wf_id,
                exc_info=True,
            )
            return {"workflow_id": wf_id, "action": "error", "reason": "db_fetch_failed"}

        if not db_run:
            # PlaybookRun was deleted — orphaned
            logger.warning(
                "Orphan recovery: playbook run '%s' not found for workflow '%s'",
                run_id,
                wf_id,
            )
            await self._emit_orphaned(workflow, reason="run_not_found")
            return {"workflow_id": wf_id, "action": "orphaned", "reason": "run_not_found"}

        # Dispatch based on playbook run status
        if db_run.status == "paused":
            return await self._recover_paused(workflow, db_run)
        elif db_run.status == "running":
            return await self._recover_stale_running(workflow, db_run)
        elif db_run.status == "completed":
            return await self._sync_completed(workflow, db_run)
        elif db_run.status in ("failed", "timed_out"):
            return await self._handle_terminal_run(workflow, db_run)
        else:
            logger.debug(
                "Orphan recovery: workflow '%s' run '%s' has unexpected status '%s'",
                wf_id,
                run_id,
                db_run.status,
            )
            return {
                "workflow_id": wf_id,
                "action": "none",
                "reason": f"unknown_status:{db_run.status}",
            }

    async def _recover_paused(self, workflow: Workflow, db_run: PlaybookRun) -> dict[str, Any]:
        """Recover a workflow whose playbook is paused.

        If the run is paused waiting for ``workflow.stage.completed`` and
        all workflow tasks are actually completed, re-emit the event so the
        ``WorkflowStageResumeHandler`` picks it up.
        """
        wf_id = workflow.workflow_id
        run_id = db_run.run_id

        if db_run.waiting_for_event != "workflow.stage.completed":
            logger.debug(
                "Orphan recovery: workflow '%s' run '%s' is paused waiting for '%s' "
                "(not stage completion) — skipping",
                wf_id,
                run_id,
                db_run.waiting_for_event,
            )
            return {
                "workflow_id": wf_id,
                "action": "none",
                "reason": f"paused_for_other_event:{db_run.waiting_for_event}",
            }

        # Check if pause has exceeded the max age
        paused_at = db_run.paused_at or db_run.started_at
        if paused_at and (time.time() - paused_at) > _MAX_PAUSE_AGE_S:
            logger.warning(
                "Orphan recovery: workflow '%s' run '%s' paused for >%ds — "
                "event handler will handle timeout",
                wf_id,
                run_id,
                _MAX_PAUSE_AGE_S,
            )
            return {"workflow_id": wf_id, "action": "none", "reason": "pause_expired"}

        # Check if all workflow tasks are completed (the event was missed)
        all_completed = await self._all_tasks_completed(workflow)
        if not all_completed:
            logger.debug(
                "Orphan recovery: workflow '%s' has outstanding tasks — "
                "waiting for natural stage completion",
                wf_id,
            )
            return {"workflow_id": wf_id, "action": "none", "reason": "tasks_pending"}

        # All tasks completed but the event was never processed (daemon was
        # down when the last task finished).  Re-emit the event.
        stage = workflow.current_stage or ""
        logger.info(
            "Orphan recovery: re-emitting workflow.stage.completed for "
            "workflow '%s' stage '%s' (event missed during downtime)",
            wf_id,
            stage,
        )
        await self._bus.emit(
            "workflow.stage.completed",
            {
                "workflow_id": wf_id,
                "stage": stage,
                "task_ids": list(workflow.task_ids),
                "_recovery": True,  # Mark as recovery-originated
            },
        )
        return {"workflow_id": wf_id, "action": "reemit_event", "stage": stage}

    async def _recover_stale_running(
        self, workflow: Workflow, db_run: PlaybookRun
    ) -> dict[str, Any]:
        """Recover a workflow whose playbook run is stuck in 'running'.

        After a daemon restart, a run in 'running' state is a stale artifact
        — no process is actually executing it.  Mark it as failed so the
        workflow can be recovered manually or automatically.
        """
        wf_id = workflow.workflow_id
        run_id = db_run.run_id

        logger.warning(
            "Orphan recovery: playbook run '%s' for workflow '%s' is stuck "
            "in 'running' (stale after restart) — marking as failed",
            run_id,
            wf_id,
        )

        try:
            await self._db.update_playbook_run(
                run_id,
                status="failed",
                completed_at=time.time(),
                error="Marked failed by orphan recovery: daemon restarted while run was executing",
            )
        except Exception:
            logger.error(
                "Orphan recovery: failed to update run '%s' status",
                run_id,
                exc_info=True,
            )
            return {"workflow_id": wf_id, "action": "error", "reason": "update_failed"}

        await self._emit_orphaned(workflow, reason="run_stale_running", run_id=run_id)
        return {"workflow_id": wf_id, "action": "mark_run_failed", "run_id": run_id}

    async def _sync_completed(self, workflow: Workflow, db_run: PlaybookRun) -> dict[str, Any]:
        """Sync a workflow whose playbook run completed but workflow status
        was never updated.
        """
        wf_id = workflow.workflow_id

        logger.info(
            "Orphan recovery: workflow '%s' playbook run completed but "
            "workflow status is still 'running' — syncing to 'completed'",
            wf_id,
        )

        try:
            await self._db.update_workflow_status(
                wf_id, "completed", completed_at=db_run.completed_at or time.time()
            )
        except Exception:
            logger.error(
                "Orphan recovery: failed to update workflow '%s' status",
                wf_id,
                exc_info=True,
            )
            return {"workflow_id": wf_id, "action": "error", "reason": "workflow_update_failed"}

        return {"workflow_id": wf_id, "action": "sync_completed"}

    async def _handle_terminal_run(self, workflow: Workflow, db_run: PlaybookRun) -> dict[str, Any]:
        """Handle a workflow whose playbook run is in a terminal failed/timed_out state.

        The workflow is truly orphaned — emit an event so operators are notified
        and can trigger manual recovery.
        """
        wf_id = workflow.workflow_id
        run_id = db_run.run_id

        logger.warning(
            "Orphan recovery: workflow '%s' is orphaned — playbook run '%s' "
            "has status '%s' (error: %s)",
            wf_id,
            run_id,
            db_run.status,
            db_run.error or "none",
        )

        await self._emit_orphaned(
            workflow,
            reason=f"run_{db_run.status}",
            run_id=run_id,
            error=db_run.error,
        )
        return {
            "workflow_id": wf_id,
            "action": "orphaned",
            "reason": f"run_{db_run.status}",
            "run_id": run_id,
        }

    # ------------------------------------------------------------------
    # Periodic monitoring
    # ------------------------------------------------------------------

    async def check_periodic(self) -> None:
        """Periodic orphan check — called from ``run_one_cycle()``.

        Rate-limited by ``check_interval_seconds``.  Emits
        ``workflow.orphaned`` events for newly discovered orphans.
        """
        now = time.time()
        if (now - self._last_check_time) < self._check_interval:
            return

        self._last_check_time = now

        try:
            running_workflows = await self._db.list_workflows(status="running")
        except Exception:
            logger.debug("Periodic orphan check: failed to list workflows", exc_info=True)
            return

        if not running_workflows:
            # Clear reported orphans if no running workflows remain
            self._reported_orphans.clear()
            return

        # Clean up reported orphans that are no longer running
        running_ids = {w.workflow_id for w in running_workflows}
        self._reported_orphans -= self._reported_orphans - running_ids

        for workflow in running_workflows:
            if workflow.workflow_id in self._reported_orphans:
                continue  # Already reported this orphan

            run_id = workflow.playbook_run_id
            if not run_id:
                await self._emit_orphaned(workflow, reason="no_playbook_run_id")
                self._reported_orphans.add(workflow.workflow_id)
                continue

            try:
                db_run = await self._db.get_playbook_run(run_id)
            except Exception:
                continue  # Transient DB error, retry next cycle

            if not db_run:
                await self._emit_orphaned(workflow, reason="run_not_found")
                self._reported_orphans.add(workflow.workflow_id)
                continue

            if db_run.status in ("failed", "timed_out"):
                await self._emit_orphaned(
                    workflow,
                    reason=f"run_{db_run.status}",
                    run_id=run_id,
                    error=db_run.error,
                )
                self._reported_orphans.add(workflow.workflow_id)
            elif db_run.status == "completed":
                # Playbook finished but workflow wasn't updated — sync it
                try:
                    await self._db.update_workflow_status(
                        workflow.workflow_id,
                        "completed",
                        completed_at=db_run.completed_at or time.time(),
                    )
                    logger.info(
                        "Periodic orphan check: synced workflow '%s' to completed",
                        workflow.workflow_id,
                    )
                except Exception:
                    logger.debug(
                        "Periodic orphan check: failed to sync workflow '%s'",
                        workflow.workflow_id,
                    )
            elif db_run.status == "paused":
                # Paused runs might have missed events — check if all tasks done
                if db_run.waiting_for_event == "workflow.stage.completed":
                    all_done = await self._all_tasks_completed(workflow)
                    if all_done:
                        stage = workflow.current_stage or ""
                        logger.info(
                            "Periodic orphan check: re-emitting missed "
                            "workflow.stage.completed for '%s' stage '%s'",
                            workflow.workflow_id,
                            stage,
                        )
                        await self._bus.emit(
                            "workflow.stage.completed",
                            {
                                "workflow_id": workflow.workflow_id,
                                "stage": stage,
                                "task_ids": list(workflow.task_ids),
                                "_recovery": True,
                            },
                        )

    # ------------------------------------------------------------------
    # Manual recovery
    # ------------------------------------------------------------------

    async def recover_workflow(
        self,
        workflow_id: str,
    ) -> dict[str, Any]:
        """Manually recover an orphaned workflow.

        This method is called by ``CommandHandler._cmd_recover_workflow``
        to allow operators to manually trigger recovery of a specific
        workflow.

        Recovery strategy:
        - If the run is paused and all tasks are done → re-emit stage event
        - If the run is failed/timed_out → re-emit orphaned event with
          recovery_requested flag so hooks/automation can re-trigger the
          playbook
        - If the run is running → no action (still active)
        - If no run → emit orphaned event

        Returns a result dict describing the recovery action taken.
        """
        try:
            workflow = await self._db.get_workflow(workflow_id)
        except Exception:
            return {"success": False, "error": f"Failed to fetch workflow '{workflow_id}'"}

        if not workflow:
            return {"success": False, "error": f"Workflow '{workflow_id}' not found"}

        if workflow.status not in ("running", "paused"):
            return {
                "success": False,
                "error": (
                    f"Workflow '{workflow_id}' has status '{workflow.status}' — "
                    f"only 'running' or 'paused' workflows can be recovered"
                ),
            }

        run_id = workflow.playbook_run_id
        if not run_id:
            await self._emit_orphaned(
                workflow, reason="no_playbook_run_id", recovery_requested=True
            )
            return {
                "success": True,
                "action": "orphaned_event_emitted",
                "reason": "no_playbook_run_id",
            }

        try:
            db_run = await self._db.get_playbook_run(run_id)
        except Exception:
            return {"success": False, "error": f"Failed to fetch playbook run '{run_id}'"}

        if not db_run:
            await self._emit_orphaned(workflow, reason="run_not_found", recovery_requested=True)
            return {
                "success": True,
                "action": "orphaned_event_emitted",
                "reason": "run_not_found",
            }

        if db_run.status == "paused":
            if db_run.waiting_for_event == "workflow.stage.completed":
                all_done = await self._all_tasks_completed(workflow)
                if all_done:
                    stage = workflow.current_stage or ""
                    await self._bus.emit(
                        "workflow.stage.completed",
                        {
                            "workflow_id": workflow_id,
                            "stage": stage,
                            "task_ids": list(workflow.task_ids),
                            "_recovery": True,
                        },
                    )
                    return {
                        "success": True,
                        "action": "stage_event_reemitted",
                        "stage": stage,
                    }
                else:
                    return {
                        "success": True,
                        "action": "no_action",
                        "reason": "tasks_still_pending",
                        "message": (
                            f"Workflow '{workflow_id}' playbook is paused waiting for "
                            f"stage completion, but not all tasks are done yet. "
                            f"The playbook will resume automatically when tasks complete."
                        ),
                    }
            else:
                return {
                    "success": True,
                    "action": "no_action",
                    "reason": f"paused_for_{db_run.waiting_for_event or 'human'}",
                    "message": (
                        f"Workflow '{workflow_id}' playbook is paused waiting for "
                        f"'{db_run.waiting_for_event or 'human input'}'. "
                        f"Use resume_playbook to provide input."
                    ),
                }

        elif db_run.status == "running":
            return {
                "success": True,
                "action": "no_action",
                "reason": "still_running",
                "message": f"Workflow '{workflow_id}' playbook is still running.",
            }

        elif db_run.status in ("failed", "timed_out"):
            await self._emit_orphaned(
                workflow,
                reason=f"run_{db_run.status}",
                run_id=run_id,
                error=db_run.error,
                recovery_requested=True,
            )
            # Remove from reported set so the event is fresh
            self._reported_orphans.discard(workflow_id)
            return {
                "success": True,
                "action": "orphaned_event_emitted",
                "reason": f"run_{db_run.status}",
                "run_status": db_run.status,
                "run_error": db_run.error,
                "message": (
                    f"Workflow '{workflow_id}' is orphaned — playbook run "
                    f"'{run_id}' {db_run.status}. A workflow.orphaned event "
                    f"has been emitted. To re-trigger the playbook, use "
                    f"resume_playbook or create a new playbook run for "
                    f"playbook '{workflow.playbook_id}'."
                ),
            }

        elif db_run.status == "completed":
            try:
                await self._db.update_workflow_status(
                    workflow_id, "completed", completed_at=db_run.completed_at or time.time()
                )
            except Exception:
                return {
                    "success": False,
                    "error": "Failed to sync workflow status to completed",
                }
            return {
                "success": True,
                "action": "synced_completed",
                "message": (
                    f"Workflow '{workflow_id}' synced to 'completed' — "
                    f"playbook run had already finished."
                ),
            }

        return {"success": False, "error": f"Unexpected run status: {db_run.status}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _all_tasks_completed(self, workflow: Workflow) -> bool:
        """Check if all tasks in a workflow have reached COMPLETED status."""
        from src.models import TaskStatus

        if not workflow.task_ids:
            return False  # No tasks = stage not completable

        for tid in workflow.task_ids:
            try:
                task = await self._db.get_task(tid)
            except Exception:
                return False  # Can't verify, assume incomplete
            if not task or task.status != TaskStatus.COMPLETED:
                return False

        return True

    async def _emit_orphaned(
        self,
        workflow: Workflow,
        *,
        reason: str,
        run_id: str | None = None,
        error: str | None = None,
        recovery_requested: bool = False,
    ) -> None:
        """Emit a ``workflow.orphaned`` event for operator alerting."""
        data: dict[str, Any] = {
            "workflow_id": workflow.workflow_id,
            "playbook_id": workflow.playbook_id,
            "project_id": workflow.project_id,
            "reason": reason,
        }
        if run_id:
            data["run_id"] = run_id
        if error:
            data["error"] = error
        if workflow.current_stage:
            data["current_stage"] = workflow.current_stage
        if workflow.task_ids:  # Only include non-empty lists
            data["task_ids"] = list(workflow.task_ids)
        if recovery_requested:
            data["recovery_requested"] = True

        try:
            await self._bus.emit("workflow.orphaned", data)
        except Exception:
            logger.error(
                "Failed to emit workflow.orphaned event for '%s'",
                workflow.workflow_id,
                exc_info=True,
            )

    def clear_reported(self, workflow_id: str | None = None) -> None:
        """Clear reported orphan tracking.

        If *workflow_id* is given, only clear that workflow.  Otherwise
        clear all tracked orphans (e.g. on full re-check).
        """
        if workflow_id:
            self._reported_orphans.discard(workflow_id)
        else:
            self._reported_orphans.clear()
