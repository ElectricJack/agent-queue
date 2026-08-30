"""Approval mixin — plan/approval workflows and PR status polling."""

from __future__ import annotations

import logging

from src.database.queries.hierarchy_queries import HierarchyError
from src.task_summary import write_task_summary
from src.profiles.sync import underlying_agent_type
from src.models import (
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
                try:
                    await self.db.transition_task(
                        task.id, TaskStatus.COMPLETED, context="auto_complete_no_pr"
                    )
                except HierarchyError as exc:
                    # Invariant 6 (spec §7): a container cannot complete while
                    # a child is open.  Leave it as it was — the container
                    # settles on its own once the children finish.
                    logger.warning("auto-complete refused for %s: %s", task.id, exc)
                    return
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

    # Accepted deviation: the base call ``_poll_pr_merged(pr_url)`` remains
    # valid — ``project_id`` is kw-only so positional callers keep working.
    async def _poll_pr_merged(
        self, pr_url: str, *, project_id: str | None = None
    ) -> bool | None:
        """Poll ``gh`` for a PR's merge state.

        Returns:
            * ``True``  — PR is merged.
            * ``False`` — PR is still open (``gh`` said so).
            * ``None``  — PR is closed without merge, OR the poll could
              not run (``gh`` raised, no checkout available).

        The tri-state matters at the caller: ``_check_pr_status`` uses
        ``None`` to transition tasks to BLOCKED (closed-unmerged is a
        distinct failure from still-open).  Swallowing ``None`` into
        ``False`` — which the previous ``except`` did — silently kept
        closed-unmerged PRs stuck in AWAITING_APPROVAL forever.

        The sweep caller (``_sweep_resolve_pr_ci_gates``) only acts on
        ``True`` and treats both ``False`` and ``None`` as "leave the gate
        open", so the distinction is safe there.  Extracted from
        :meth:`_check_pr_status` so gate sweeps and task polls share one
        polling body.
        """
        checkout_path: str | None = None
        # Prefer a workspace for the given project; fall back to any.
        if project_id:
            workspaces = await self.db.list_workspaces(project_id=project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            workspaces = await self.db.list_workspaces()
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            # Nothing to poll from — behave as "still open" (retry next
            # cycle when a workspace shows up).  Do not return None here:
            # the task-poll caller maps None → BLOCKED, and blocking on
            # "no checkout yet" would be a false positive.
            return False

        try:
            # ``acheck_pr_merged`` returns ``None`` for closed-unmerged —
            # let that propagate so ``_check_pr_status`` can transition
            # the task to BLOCKED.  The previous ``except`` mapped every
            # unexpected condition to False and swallowed that signal.
            return await self.git.acheck_pr_merged(checkout_path, pr_url)
        except Exception as e:
            logger.warning("Error polling PR %s: %s", pr_url, e)
            # Transient gh failure — retry next cycle rather than block.
            return False

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

        # Prefer the workspace locked by this task; else fall back to any
        # workspace on the project.  The extracted ``_poll_pr_merged`` covers
        # the plain any-workspace-on-project path — we still probe the
        # per-task workspace first here because ``AWAITING_APPROVAL`` tasks
        # usually still hold theirs.
        checkout_path = None
        ws = await self.db.get_workspace_for_task(task.id)
        if ws:
            checkout_path = ws.workspace_path
        if checkout_path:
            try:
                merged = await self.git.acheck_pr_merged(checkout_path, task.pr_url)
            except Exception as e:
                logger.warning("Error checking PR for task %s: %s", task.id, e)
                return
        else:
            merged = await self._poll_pr_merged(task.pr_url, project_id=task.project_id)

        if merged is True:
            try:
                await self.db.transition_task(task.id, TaskStatus.COMPLETED, context="pr_merged")
            except HierarchyError as exc:
                # Invariant 6 (spec §7).  The PR is merged but the container
                # still has open children; it settles once they finish.
                logger.warning("pr_merged completion refused for %s: %s", task.id, exc)
                return
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
                agent_type=underlying_agent_type(profile.id) if profile else None,
            )
            await self._emit_text_notify(
                f"**PR Closed:** Task `{task.id}` — {task.title} "
                f"was closed without merging. Marked as BLOCKED.",
                project_id=task.project_id,
            )
            await self._notify_stuck_chain(task)
