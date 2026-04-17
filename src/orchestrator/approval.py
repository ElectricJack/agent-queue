"""Approval mixin — plan/approval workflows and PR status polling."""

from __future__ import annotations

import logging
from typing import Any

from src.git.manager import GitError
from src.task_summary import write_task_summary
from src.models import (
    PhaseResult,
    PipelineContext,
    Task,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class ApprovalMixin:
    """Plan/approval workflow methods mixed into Orchestrator."""

    # ── Approval polling constants ─────────────────────────────────────────
    #
    # These control the behavior of _check_awaiting_approval and its helpers
    # (_handle_awaiting_no_pr, _check_pr_status).  The approval check itself
    # is throttled to once per 60s (see _last_approval_check in __init__).
    #
    # How often (seconds) to re-send reminders for tasks awaiting manual
    # approval (no PR URL).  Prevents notification spam for tasks that
    # legitimately need manual review.
    _NO_PR_REMINDER_INTERVAL: int = 3600  # 1 hour
    # After this many seconds without approval, escalate the notification
    # tone from "awaiting review" to "stuck task" with stronger language.
    _NO_PR_ESCALATION_THRESHOLD: int = 86400  # 24 hours
    # Tasks that don't require approval and have no PR URL are auto-completed
    # after this grace period (seconds).  The grace period avoids a race
    # condition: _complete_workspace transitions the task to AWAITING_APPROVAL
    # before the PR URL is set, and _create_pr_for_task sets the URL shortly
    # after.  Without the grace period, we might auto-complete a task that
    # was about to get a PR URL.
    _NO_PR_AUTO_COMPLETE_GRACE: int = 120  # 2 minutes

    async def _check_awaiting_approval(self) -> None:
        """Poll PR merge status for tasks in AWAITING_APPROVAL. Throttled to once per 60s.

        Two paths:

        * **Tasks with a PR URL** — check whether the PR has been merged
          (complete the task) or closed without merge (block the task and
          alert about orphaned downstream dependents).
        * **Tasks without a PR URL** — either auto-complete them after a
          grace period (if they don't actually require approval, which can
          happen for intermediate plan subtasks), or send periodic reminders
          so they don't rot silently in the queue.
        """
        import time

        now = time.time()
        if now - self._last_approval_check < 60:
            return
        self._last_approval_check = now

        tasks = await self.db.list_tasks(status=TaskStatus.AWAITING_APPROVAL)

        # Clean up reminder tracking for tasks that are no longer AWAITING_APPROVAL.
        active_ids = {t.id for t in tasks}
        for tid in list(self._no_pr_reminded_at):
            if tid not in active_ids:
                del self._no_pr_reminded_at[tid]

        for task in tasks:
            if not task.pr_url:
                await self._handle_awaiting_no_pr(task, now)
                continue

            await self._check_pr_status(task)

    async def _handle_awaiting_no_pr(self, task: Task, now: float) -> None:
        """Handle an AWAITING_APPROVAL task that has no PR URL.

        * If the task doesn't actually require approval, auto-complete it after
          a short grace period (avoids a race with slow PR creation).
        * If the task *does* require approval, send periodic reminders so it
          doesn't rot silently.
        """
        updated_at = await self.db.get_task_updated_at(task.id)
        age = (now - updated_at) if updated_at else 0

        # --- Auto-complete path ---------------------------------------------------
        if not task.requires_approval:
            if age >= self._NO_PR_AUTO_COMPLETE_GRACE:
                await self.db.transition_task(
                    task.id, TaskStatus.COMPLETED, context="auto_complete_no_pr"
                )
                await self.db.log_event(
                    "task_completed",
                    project_id=task.project_id,
                    task_id=task.id,
                    payload="auto-completed: no PR and approval not required",
                )
                # Write task summary to vault
                try:
                    result = await self.db.get_task_result(task.id)
                    write_task_summary(self.config.vault_root, task, result)
                except Exception as e:
                    logger.warning("Failed to write task summary for %s: %s", task.id, e)
                await self._emit_text_notify(
                    f"**Auto-completed:** Task `{task.id}` — {task.title} "
                    f"(no PR created, approval not required).",
                    project_id=task.project_id,
                )
                self._no_pr_reminded_at.pop(task.id, None)
            return

        # --- Manual-approval path -------------------------------------------------
        last_reminded = self._no_pr_reminded_at.get(task.id, 0.0)
        if now - last_reminded < self._NO_PR_REMINDER_INTERVAL:
            return  # throttle reminders

        self._no_pr_reminded_at[task.id] = now

        if age >= self._NO_PR_ESCALATION_THRESHOLD:
            hours = int(age // 3600)
            await self._emit_text_notify(
                f"⚠️ **Stuck Task:** `{task.id}` — {task.title} has been "
                f"AWAITING_APPROVAL for **{hours}h** with no PR URL.\n"
                f"Use `approve_task {task.id}` to complete it or investigate "
                f"why no PR was created.",
                project_id=task.project_id,
            )
            await self.db.log_event(
                "approval_stuck",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"no_pr_url, age={hours}h",
            )
        else:
            await self._emit_text_notify(
                f"🔍 **Awaiting manual approval:** Task `{task.id}` — "
                f"{task.title}\nNo PR URL — use `approve_task {task.id}` "
                f"to complete.",
                project_id=task.project_id,
            )

    async def _check_pr_status(self, task: Task) -> None:
        """Check whether a PR-backed AWAITING_APPROVAL task has been merged.

        Uses ``GitManager.check_pr_merged()`` (which shells out to ``gh``)
        to determine the PR's current state.  Three outcomes:

        - **True** — PR was merged → task transitions to COMPLETED, and the
          remote task branch is cleaned up.
        - **None** — PR was closed *without* merge → task transitions to
          BLOCKED, and downstream dependents are checked for orphaning.
        - **False** — PR is still open → no action (check again next cycle).

        Requires a valid git checkout path to run ``gh pr view``.  Falls back
        to any workspace associated with the project if the task's own
        workspace has already been released.
        """
        from src.notifications.builder import build_task_detail

        # Need a checkout path to run gh commands
        checkout_path = None
        # Try workspace locked by this task first
        ws = await self.db.get_workspace_for_task(task.id)
        if ws:
            checkout_path = ws.workspace_path
        # Fall back to any workspace for this project
        if not checkout_path:
            workspaces = await self.db.list_workspaces(project_id=task.project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            return

        try:
            merged = await self.git.acheck_pr_merged(checkout_path, task.pr_url)
        except Exception as e:
            logger.warning("Error checking PR for task %s: %s", task.id, e)
            return

        if merged is True:
            await self.db.transition_task(task.id, TaskStatus.COMPLETED, context="pr_merged")
            await self.db.log_event("task_completed", project_id=task.project_id, task_id=task.id)
            await self._emit_text_notify(
                f"**PR Merged:** Task `{task.id}` — {task.title} is now COMPLETED.",
                project_id=task.project_id,
            )
            # Write task summary to vault
            try:
                result = await self.db.get_task_result(task.id)
                write_task_summary(self.config.vault_root, task, result)
            except Exception as e:
                logger.warning("Failed to write task summary for %s: %s", task.id, e)
            # Check if this completion finishes a workflow stage
            await self._check_workflow_stage_completion(task)
            # Clean up the task branch (remote may already be deleted by GitHub)
            if task.branch_name:
                try:
                    await self.git.adelete_branch(
                        checkout_path,
                        task.branch_name,
                        delete_remote=True,
                    )
                except Exception:
                    pass  # branch cleanup is best-effort
        elif merged is None:
            # Closed without merge
            await self.db.transition_task(task.id, TaskStatus.BLOCKED, context="pr_closed")
            profile = await self._resolve_profile(task)
            await self._emit_task_failure(
                task,
                "pr_closed",
                error="PR was closed without merging",
                agent_type=profile.id if profile else None,
            )
            await self._emit_text_notify(
                f"**PR Closed:** Task `{task.id}` — {task.title} "
                f"was closed without merging. Marked as BLOCKED.",
                project_id=task.project_id,
            )
            await self._notify_stuck_chain(task)

    async def _phase_plan_discover(self, ctx: PipelineContext) -> PhaseResult:
        """Delegate plan discovery to the Supervisor."""
        if not hasattr(self, "_supervisor") or not self._supervisor:
            logger.info(
                "Task %s: no supervisor available, using legacy plan discovery",
                ctx.task.id,
            )
            return await self._phase_plan_generate(ctx)  # Legacy fallback

        logger.info(
            "Task %s: starting plan discovery via supervisor (workspace=%s)",
            ctx.task.id,
            ctx.workspace_path,
        )
        result = await self._supervisor.on_task_completed(
            task_id=ctx.task.id,
            project_id=ctx.task.project_id or "",
            workspace_path=ctx.workspace_path,
        )
        if result and result.get("plan_found"):
            logger.info(
                "Task %s: plan found — will present for approval",
                ctx.task.id,
            )
            ctx.plan_needs_approval = True
            # The supervisor archived the plan file (renamed plan.md →
            # .claude/plans/), which dirties the working tree.  Commit the
            # archival so _phase_verify doesn't see it as uncommitted agent
            # changes and incorrectly reopen the task.
            #
            # Must use exclude_plans=False because the archived files live
            # under .claude/plans/ which is in _PLAN_FILE_EXCLUDES.
            #
            # Must use no_verify=True to bypass pre-commit hooks (e.g. ruff)
            # which can reject the commit and crash the pipeline before
            # _phase_verify even runs — causing the task to be blocked with
            # a misleading "verification failed" error.
            if ctx.workspace_path and await self.git.avalidate_checkout(ctx.workspace_path):
                await self.git.acommit_all(
                    ctx.workspace_path,
                    f"chore: archive plan file\n\nTask-Id: {ctx.task.id}",
                    exclude_plans=False,
                    no_verify=True,
                    event_bus=self.bus,
                    project_id=ctx.task.project_id,
                    agent_id=ctx.agent.id,
                )
        else:
            reason = (
                result.get("reason", "unknown") if isinstance(result, dict) else "non-dict result"
            )
            logger.info(
                "Task %s: no plan found (reason: %s)",
                ctx.task.id,
                reason,
            )
        return PhaseResult.CONTINUE

    async def _phase_plan_generate(self, ctx: PipelineContext) -> PhaseResult:
        """Pipeline phase: discover plan files and store for approval.

        Runs BEFORE merge so the plan file archival is committed to the
        branch before it reaches the default branch.  After archiving the
        plan file, a cleanup commit is made to ensure the deletion of the
        original plan file is in git history.

        Instead of auto-creating subtasks, this phase stores the parsed plan
        data in ``task_context`` and sets ``ctx.plan_needs_approval = True``
        so the caller can transition the task to AWAITING_PLAN_APPROVAL
        and present the plan to the user for approval.
        """
        if not ctx.workspace_path:
            return PhaseResult.CONTINUE
        plan_stored = await self._discover_and_store_plan(ctx.task, ctx.workspace_path)
        # If a plan was stored, the plan file was archived (renamed).
        # Commit the archival so the merge won't carry the plan file to main.
        # Use no_verify=True to bypass pre-commit hooks that could crash the
        # pipeline before _phase_verify runs.
        if plan_stored and ctx.task.branch_name:
            if await self.git.avalidate_checkout(ctx.workspace_path):
                await self.git.acommit_all(
                    ctx.workspace_path,
                    f"chore: archive plan file\n\nTask-Id: {ctx.task.id}",
                    exclude_plans=False,
                    no_verify=True,
                    event_bus=self.bus,
                    project_id=ctx.task.project_id,
                    agent_id=ctx.agent.id,
                )
            ctx.plan_needs_approval = True
        return PhaseResult.CONTINUE
