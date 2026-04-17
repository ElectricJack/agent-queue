"""Monitoring mixin — state checks run each cycle."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.discord.notifications import (
    format_failed_blocked_report,
    format_failed_blocked_report_embed,
)
from src.notifications.builder import build_task_detail
from src.notifications.events import (
    BudgetWarningEvent,
    ChainStuckEvent,
    StuckDefinedTaskEvent,
)
from src.models import Task, TaskStatus
from src.task_summary import write_task_summary

logger = logging.getLogger(__name__)


class MonitoringMixin:
    """Monitoring and housekeeping methods mixed into Orchestrator."""

    async def _resume_paused_tasks(self) -> None:
        """Check PAUSED tasks whose ``resume_after`` has elapsed and promote to READY.

        Tasks enter PAUSED when the agent hits a rate limit or token
        exhaustion, with ``resume_after`` set to a future timestamp.
        This method scans all PAUSED tasks and transitions any whose
        backoff timer has expired back to READY for re-scheduling.
        """
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        for task in paused:
            if task.resume_after and task.resume_after <= now:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.READY,
                    context="resume_paused",
                    assigned_agent_id=None,
                    resume_after=None,
                )

    async def _check_defined_tasks(self) -> None:
        """Promote DEFINED/BLOCKED tasks to READY when all dependencies are satisfied.

        Scans all DEFINED tasks and checks their dependency list:
        - Tasks with no dependencies are immediately promoted to READY.
        - Tasks with dependencies are promoted only when every upstream
          dependency has reached COMPLETED status.

        Also scans BLOCKED tasks that have dependencies — if all deps are now
        COMPLETED, the task is promoted to READY (e.g. a task that was blocked
        on a dependency chain and the upstream has since completed).

        Special handling for plan subtasks:
        - Skipped if the parent plan is still in AWAITING_PLAN_APPROVAL.
        - If the parent plan is IN_PROGRESS (approved, subtasks running),
          the parent dependency is treated as met — only non-parent
          dependencies must be COMPLETED.

        This runs after ``_check_awaiting_approval`` so that freshly-merged
        PRs can unblock their dependents in the same cycle.
        """
        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        # Also check BLOCKED tasks — their dependencies may have been
        # satisfied since they were blocked, allowing them to proceed.
        blocked = await self.db.list_tasks(status=TaskStatus.BLOCKED)
        for task in [*defined, *blocked]:
            # Plan subtask special handling: the parent plan transitions to
            # IN_PROGRESS (not COMPLETED) when approved, so standard
            # are_dependencies_met() would block forever.  We treat the
            # IN_PROGRESS parent dep as satisfied.
            if task.is_plan_subtask and task.parent_task_id:
                parent = await self.db.get_task(task.parent_task_id)
                if parent and parent.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                    continue
                if parent and parent.status == TaskStatus.IN_PROGRESS:
                    # Parent plan is approved and active — treat parent dep as met.
                    # Check only non-parent dependencies.
                    deps = await self.db.get_dependencies(task.id)
                    non_parent_deps = deps - {task.parent_task_id}
                    if not non_parent_deps:
                        await self.db.transition_task(
                            task.id, TaskStatus.READY, context="deps_met_plan_parent_active"
                        )
                    else:
                        # All non-parent deps must be COMPLETED
                        all_met = True
                        for dep_id in non_parent_deps:
                            dep_task = await self.db.get_task(dep_id)
                            if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                                all_met = False
                                break
                        if all_met:
                            await self.db.transition_task(
                                task.id, TaskStatus.READY, context="deps_met_plan_parent_active"
                            )
                    continue

            deps = await self.db.get_dependencies(task.id)
            if not deps:
                if task.status == TaskStatus.DEFINED:
                    # No dependencies — promote DEFINED to READY.
                    # (BLOCKED tasks with no deps stay blocked — they were
                    # blocked for other reasons like verification failure.)
                    await self.db.transition_task(
                        task.id, TaskStatus.READY, context="deps_met_no_deps"
                    )
            else:
                deps_met = await self.db.are_dependencies_met(task.id)
                if deps_met:
                    await self.db.transition_task(task.id, TaskStatus.READY, context="deps_met")

    async def _check_plan_parent_completion(self) -> None:
        """Auto-complete plan parent tasks when all their subtasks are done.

        When a plan is approved, the parent transitions to IN_PROGRESS (not
        COMPLETED) so its status accurately reflects that work is still in
        progress.  This method checks all IN_PROGRESS tasks that have subtasks
        and transitions them to COMPLETED once every subtask has finished.

        Runs every cycle to catch all completion paths (agent completion,
        PR merge, admin skip, etc.) without needing hooks in each path.
        """
        in_progress = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for task in in_progress:
            subtasks = await self.db.get_subtasks(task.id)
            if not subtasks:
                continue  # Not a plan parent — skip
            if all(s.status == TaskStatus.COMPLETED for s in subtasks):
                await self.db.transition_task(
                    task.id, TaskStatus.COMPLETED, context="subtasks_completed"
                )
                await self.db.log_event(
                    "plan_completed",
                    project_id=task.project_id,
                    task_id=task.id,
                    payload=f"All {len(subtasks)} subtask(s) completed",
                )
                # Write task summary to vault
                try:
                    result = await self.db.get_task_result(task.id)
                    write_task_summary(self.config.vault_root, task, result)
                except Exception as e:
                    logger.warning("Failed to write task summary for %s: %s", task.id, e)
                await self._emit_text_notify(
                    f"**Plan Completed:** `{task.id}` — {task.title} "
                    f"(all {len(subtasks)} subtask(s) finished).",
                    project_id=task.project_id,
                )
                logger.info(
                    "Plan parent %s auto-completed: all %d subtasks finished",
                    task.id,
                    len(subtasks),
                )
                # Check if this plan-parent completion finishes a workflow stage
                await self._check_workflow_stage_completion(task)

    async def _check_stuck_defined_tasks(self) -> None:
        """Monitoring: detect DEFINED tasks stuck waiting for dependencies.

        Queries for tasks that have been in DEFINED status longer than
        ``monitoring.stuck_task_threshold_seconds`` and sends a notification
        with details about which upstream dependencies are blocking them.

        Notifications are rate-limited to one per threshold period per task
        (tracked in ``_stuck_notified_at``) to avoid flooding Discord.
        The tracker is garbage-collected each cycle to remove entries for
        tasks that are no longer stuck.
        """
        threshold = self.config.monitoring.stuck_task_threshold_seconds
        if threshold <= 0:
            return  # Disabled

        stuck_tasks = await self.db.get_stuck_defined_tasks(threshold)
        if not stuck_tasks:
            return

        now = time.time()

        # Clean up notification tracker for tasks no longer DEFINED
        stuck_ids = {t.id for t in stuck_tasks}
        for tid in list(self._stuck_notified_at):
            if tid not in stuck_ids:
                del self._stuck_notified_at[tid]

        for task in stuck_tasks:
            # Rate-limit: only notify once per threshold period per task
            last_notified = self._stuck_notified_at.get(task.id, 0)
            if now - last_notified < threshold:
                continue

            # Find which dependencies are blocking this task
            blocking = await self.db.get_blocking_dependencies(task.id)

            # Calculate how long the task has been stuck
            task_created_at = await self.db.get_task_created_at(task.id)
            if not task_created_at:
                task_created_at = now  # fallback (should not happen)
            stuck_hours = (now - task_created_at) / 3600

            await self._emit_notify(
                "notify.stuck_defined_task",
                StuckDefinedTaskEvent(
                    task=build_task_detail(task),
                    blocking_deps=[
                        {"id": dep_id, "title": dep_title, "status": dep_status}
                        for dep_id, dep_title, dep_status in blocking
                    ],
                    stuck_hours=stuck_hours,
                    project_id=task.project_id,
                ),
            )

            # Log the event
            blocking_info = ", ".join(
                f"{dep_id}({dep_status})" for dep_id, _, dep_status in blocking[:10]
            )
            await self.db.log_event(
                "stuck_defined_task",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"stuck_hours={stuck_hours:.1f}, blocking=[{blocking_info}]",
            )
            logger.info(
                "Stuck task detected: %s — %s (DEFINED for %.1fh, blocked by %d deps)",
                task.id,
                task.title,
                stuck_hours,
                len(blocking),
            )

            self._stuck_notified_at[task.id] = now

    async def _check_failed_blocked_tasks(self) -> None:
        """Periodic report: summarize all FAILED and BLOCKED tasks to the channel.

        Queries for tasks currently in FAILED or BLOCKED status and posts a
        consolidated summary to the notification channel so operators have an
        at-a-glance view of everything needing manual intervention.

        Rate-limited by ``monitoring.failed_blocked_report_interval_seconds``
        (default 1 hour).  Set to 0 or negative to disable.  The report is
        only sent when at least one task is in FAILED or BLOCKED status.
        """
        interval = self.config.monitoring.failed_blocked_report_interval_seconds
        if interval <= 0:
            return  # Disabled

        now = time.time()
        if now - self._last_failed_blocked_report < interval:
            return

        self._last_failed_blocked_report = now

        failed_tasks = await self.db.list_tasks(status=TaskStatus.FAILED)
        blocked_tasks = await self.db.list_tasks(status=TaskStatus.BLOCKED)

        if not failed_tasks and not blocked_tasks:
            return

        total = len(failed_tasks) + len(blocked_tasks)
        logger.info(
            "Failed/blocked report: %d failed, %d blocked (%d total)",
            len(failed_tasks),
            len(blocked_tasks),
            total,
        )

        # Group tasks by project so we can notify each project's channel
        projects: dict[str, tuple[list, list]] = {}
        for t in failed_tasks:
            projects.setdefault(t.project_id, ([], []))[0].append(t)
        for t in blocked_tasks:
            projects.setdefault(t.project_id, ([], []))[1].append(t)

        for project_id, (proj_failed, proj_blocked) in projects.items():
            msg = format_failed_blocked_report(proj_failed, proj_blocked)
            format_failed_blocked_report_embed(proj_failed, proj_blocked)
            await self._emit_text_notify(msg, project_id=project_id)

    async def _auto_archive_tasks(self) -> None:
        """Automatically archive terminal tasks older than the configured threshold.

        Runs at most once per hour (rate-limited via ``_last_auto_archive``)
        and only when ``config.archive.enabled`` is True.  Tasks matching the
        configured terminal statuses whose ``updated_at`` is older than
        ``archive.after_hours`` are silently moved to the ``archived_tasks``
        table so they no longer appear in active views.

        This eliminates the need for agents or operators to manually run
        ``/archive-tasks``; the orchestrator handles it automatically.
        """
        archive_cfg = self.config.archive
        if not archive_cfg.enabled:
            return

        now = time.time()
        # Rate-limit to once per hour
        if now - self._last_auto_archive < 3600:
            return
        self._last_auto_archive = now

        older_than_seconds = archive_cfg.after_hours * 3600
        try:
            archived_ids = await self.db.archive_old_terminal_tasks(
                statuses=archive_cfg.statuses,
                older_than_seconds=older_than_seconds,
            )
        except Exception as e:
            logger.error("Auto-archive error: %s", e)
            return

        if archived_ids:
            logger.info(
                "Auto-archived %d terminal task(s) older than %.1fh: %s%s",
                len(archived_ids),
                archive_cfg.after_hours,
                ", ".join(archived_ids[:10]),
                "..." if len(archived_ids) > 10 else "",
            )
            for tid in archived_ids:
                try:
                    await self.db.log_event(
                        "task_auto_archived",
                        task_id=tid,
                    )
                except Exception:
                    pass

    async def _check_paused_playbook_timeouts(self) -> None:
        """Sweep paused playbook runs for expired timeouts (roadmap 5.4.4).

        Delegates to :meth:`CommandHandler.check_paused_playbook_timeouts`
        which resolves per-node and per-playbook timeout configuration and
        handles the transition (either to a timeout node or to timed_out
        status).

        Runs every tick (5s) — the query is lightweight (indexed status
        column) and the actual timeout handling only fires for runs that
        have genuinely expired.
        """
        if not hasattr(self, "command_handler") or self.command_handler is None:
            return
        try:
            results = await self.command_handler.check_paused_playbook_timeouts()
            for r in results:
                logger.info(
                    "Playbook run %s timed out: status=%s, timeout=%ds, on_timeout=%s",
                    r["run_id"],
                    r["status"],
                    r["timeout_seconds"],
                    r.get("on_timeout"),
                )
        except Exception as e:
            logger.warning("Paused playbook timeout sweep failed: %s", e)

    async def _find_stuck_downstream(self, blocked_task_id: str) -> list[Task]:
        """BFS walk of the dependency graph to find orphaned DEFINED tasks.

        Starting from a BLOCKED task, walks forward through ``get_dependents``
        and collects every downstream task still in DEFINED status.  These
        tasks can never proceed because their dependency chain is broken.

        Only DEFINED tasks are collected — tasks that have already been
        promoted past the dependency gate (READY, IN_PROGRESS, etc.) are
        not affected by the upstream blockage.

        Used by ``_notify_stuck_chain`` to give operators visibility into
        the full blast radius when a task fails or is stopped.
        """
        stuck: list[Task] = []
        visited: set[str] = set()
        queue: list[str] = [blocked_task_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            dependents = await self.db.get_dependents(current_id)
            for dep_id in dependents:
                if dep_id in visited:
                    continue
                task = await self.db.get_task(dep_id)
                if not task:
                    continue
                # Only DEFINED tasks are "stuck" — tasks in other states
                # (READY, IN_PROGRESS, etc.) have already moved past the
                # dependency gate.
                if task.status == TaskStatus.DEFINED:
                    stuck.append(task)
                    # Continue walking: this stuck task may itself have
                    # downstream dependents.
                    queue.append(dep_id)

        return stuck

    async def _notify_stuck_chain(self, blocked_task: Task) -> None:
        """Check for downstream stuck tasks and send a notification.

        Uses ``_find_stuck_downstream`` to do a BFS walk of the dependency
        graph.  If any DEFINED tasks are found that are transitively blocked
        by this task, sends a single consolidated notification listing all
        affected downstream tasks so operators can decide whether to skip,
        retry, or manually unblock the chain.
        """
        stuck = await self._find_stuck_downstream(blocked_task.id)
        if not stuck:
            return

        await self._emit_notify(
            "notify.chain_stuck",
            ChainStuckEvent(
                blocked_task=build_task_detail(blocked_task),
                stuck_task_ids=[t.id for t in stuck],
                stuck_task_titles=[t.title for t in stuck],
                project_id=blocked_task.project_id,
            ),
        )
        await self.db.log_event(
            "chain_stuck",
            project_id=blocked_task.project_id,
            task_id=blocked_task.id,
            payload=f"stuck_count={len(stuck)}, stuck_ids={[t.id for t in stuck[:20]]}",
        )

    # Budget warning thresholds — notify once per threshold crossing.
    #
    # IMPORTANT: This class attribute and the ``_check_budget_warning`` method
    # below intentionally SHADOW the earlier definitions (``_BUDGET_WARNING_THRESHOLDS``
    # and the first ``_check_budget_warning`` at line ~469).  Python resolves
    # method lookups top-down within the class body, so the LAST definition
    # wins at runtime.  This version uses cumulative token usage (simpler)
    # instead of rolling-window-scoped usage.
    #
    # TODO: consolidate the two implementations into one.  The shadowed version
    # (earlier in this file) is dead code at runtime.
    _BUDGET_THRESHOLDS: list[int] = [80, 95]

    async def _check_budget_warning(
        self,
        project_id: str,
        tokens_added: int,
    ) -> None:
        """Send a budget warning if a project crosses a spending threshold.

        Called after recording token usage for a completed task.  Queries
        the project's cumulative token usage and ``budget_limit``, then
        checks whether usage has crossed any of the ``_BUDGET_THRESHOLDS``
        percentage levels.  Each threshold (80%, 95%) fires at most once
        per project; the ``_budget_warned_at`` dict tracks the highest
        threshold already notified to avoid duplicate alerts.

        Note: this method shadows an earlier definition that uses rolling-
        window scoped usage.  The shadowed version is unreachable at runtime.
        """
        project = await self.db.get_project(project_id)
        if not project or project.budget_limit is None or project.budget_limit <= 0:
            return

        usage = await self.db.get_project_token_usage(project_id)
        pct = usage / project.budget_limit * 100

        prev_threshold = self._budget_warned_at.get(project_id, 0)

        for threshold in self._BUDGET_THRESHOLDS:
            if pct >= threshold > prev_threshold:
                await self._emit_notify(
                    "notify.budget_warning",
                    BudgetWarningEvent(
                        project_name=project.name,
                        usage=usage,
                        limit=project.budget_limit,
                        percentage=pct,
                        project_id=project_id,
                    ),
                )
                await self.db.log_event(
                    "budget_warning",
                    project_id=project_id,
                    payload=f"threshold={threshold}%, usage={usage:,}/{project.budget_limit:,}",
                )
                self._budget_warned_at[project_id] = threshold
