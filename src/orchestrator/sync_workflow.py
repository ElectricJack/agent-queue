"""Sync workflow mixin — multi-phase workspace synchronization."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.models import (
    AgentResult,
    AgentState,
    ProjectStatus,
    Task,
    TaskContext,
    TaskStatus,
)
from src.notifications.events import (
    TaskMessageEvent,
    TaskThreadOpenEvent,
)
from src.scheduler import AssignAction

logger = logging.getLogger(__name__)


class SyncWorkflowMixin:
    """Sync workflow methods mixed into Orchestrator."""

    async def _execute_sync_workflow(
        self,
        action: AssignAction,
        task: Task,
        agent,
    ) -> None:
        """Orchestrator-managed sync workflow (bypasses normal agent execution).

        This method handles tasks with ``task_type=SYNC``.  Instead of
        launching a regular agent, it coordinates a multi-phase workflow:

        1. **Pause project** — prevent new tasks from being queued.
        2. **Wait for active tasks** — poll until all IN_PROGRESS tasks
           (other than this sync task) have completed.
        3. **Merge feature branches** — acquire a workspace and launch a
           Claude Code agent to merge all feature branches into the default
           branch across all project workspaces.
        4. **Cleanup & resume** — release workspaces, resume the project.
        """
        project = await self.db.get_project(action.project_id)
        if not project:
            logger.error("Sync workflow: project %s not found", action.project_id)
            await self.db.transition_task(
                action.task_id, TaskStatus.FAILED, context="project_not_found"
            )
            await self._emit_task_failure(
                task, "project_not_found", error=f"Project {action.project_id} not found"
            )
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE, current_task_id=None)
            return

        default_branch = project.repo_default_branch or "main"
        workspaces = await self.db.list_workspaces(project_id=action.project_id)
        merge_succeeded = False  # Track merge outcome for final status

        async def _notify(msg: str) -> None:
            await self._emit_text_notify(msg, project_id=action.project_id)

        await _notify(
            f"🔄 **Sync Workspaces started:** `{task.id}`\n"
            f"Phase 1/4 — Pausing project `{action.project_id}`…"
        )

        # ── Phase 1: Pause the project ──────────────────────────────────
        await self.db.update_project(action.project_id, status=ProjectStatus.PAUSED)
        logger.info("Sync workflow %s: project %s paused", task.id, action.project_id)

        try:
            # ── Phase 2: Wait for active tasks ──────────────────────────
            await _notify(
                f"🔄 **Sync `{task.id}`** — Phase 2/4: Waiting for active tasks to complete…"
            )

            max_wait = 3600  # 1 hour max wait
            poll_interval = 10  # seconds between checks
            waited = 0
            notify_interval = 60  # start notifying after 1 minute
            next_notify_at = notify_interval  # first notification at 60s
            max_notify_interval = 960  # cap at ~16 minutes

            while waited < max_wait:
                active_tasks = await self.db.list_active_tasks(
                    project_id=action.project_id,
                    exclude_statuses={TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED},
                )
                # Filter out this sync task itself and tasks that aren't running
                running = [
                    t
                    for t in active_tasks
                    if t.id != task.id and t.status in (TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED)
                ]

                if not running:
                    break

                if waited >= next_notify_at:  # Exponential backoff for notifications
                    running_ids = ", ".join(f"`{t.id}`" for t in running[:5])
                    await _notify(
                        f"🔄 **Sync `{task.id}`** — Still waiting for "
                        f"{len(running)} task(s): {running_ids}"
                    )
                    # Double the interval for next notification, with a cap
                    notify_interval = min(notify_interval * 2, max_notify_interval)
                    next_notify_at = waited + notify_interval

                await asyncio.sleep(poll_interval)
                waited += poll_interval

            if waited >= max_wait:
                await _notify(
                    f"⚠️ **Sync `{task.id}`** — Timed out waiting for active tasks "
                    f"after {max_wait}s. Aborting sync."
                )
                await self.db.transition_task(
                    action.task_id, TaskStatus.FAILED, context="sync_timeout_waiting_for_tasks"
                )
                await self._emit_task_failure(
                    task,
                    "sync_timeout_waiting_for_tasks",
                    error=f"Timed out waiting for active tasks after {max_wait}s",
                )
                return

            logger.info("Sync workflow %s: all active tasks completed", task.id)

            # ── Early-out: check if workspaces are already synced ───────
            needs_merge = False
            for ws in workspaces:
                ws_path = ws.workspace_path
                if not os.path.isdir(ws_path):
                    continue  # skip missing paths
                try:
                    current = await self.git.aget_current_branch(ws_path)
                    branches = await self.git.alist_branches(ws_path)
                    clean_branches = [b.lstrip("* ").strip() for b in branches if b.strip()]
                    non_default = [b for b in clean_branches if b and b != default_branch]
                    if current != default_branch or non_default:
                        needs_merge = True
                        break
                except Exception:
                    # Can't check — assume merge is needed
                    needs_merge = True
                    break

            if not needs_merge:
                logger.info(
                    "Sync workflow %s: all workspaces already on %s — skipping merge",
                    task.id,
                    default_branch,
                )
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.COMPLETED,
                    context="sync_already_synced",
                )
                await _notify(
                    f"✅ **Sync `{task.id}`** — All workspaces already on "
                    f"`{default_branch}` with no feature branches. Skipping merge."
                )
                return

            # ── Phase 3: Merge feature branches ─────────────────────────
            await _notify(
                f"🔄 **Sync `{task.id}`** — Phase 3/4: "
                f"Merging feature branches into `{default_branch}`…"
            )

            # Gather workspace info for the merge task description.
            workspace_info_lines = []
            for ws in workspaces:
                workspace_info_lines.append(
                    f"  - Path: {ws.workspace_path} (id: {ws.id}, name: {ws.name or '—'})"
                )
            workspace_info = "\n".join(workspace_info_lines)

            merge_description = f"""## Merge All Feature Branches — {action.project_id}

You are a workspace synchronization agent. Your job is to merge ALL feature
branches into the default branch (`{default_branch}`) across all project workspaces,
then ensure every workspace is on `{default_branch}` with no remaining feature branches.

### Project Workspaces (absolute paths):
{workspace_info}

### Default Branch: `{default_branch}`

### Instructions:

For EACH workspace listed above, perform these steps IN ORDER:

1. **Navigate to the workspace directory** using the absolute path above.
2. **Fetch latest changes**: `git fetch origin`
3. **Identify all local branches**: `git branch` — look for any branches other than `{default_branch}`.
4. **Checkout the default branch**: `git checkout {default_branch} && git pull origin {default_branch}`
5. **For each feature branch** (branches that are NOT `{default_branch}`):
   a. Ensure the feature branch has all its changes committed and pushed.
   b. Merge the feature branch into `{default_branch}`:
      `git merge <feature-branch> --no-ff -m "Merge <feature-branch> into {default_branch}"`
   c. If there are merge conflicts, resolve them intelligently:
      - Prefer to preserve feature functionality
      - Keep both changes when possible
      - If truly duplicated code, keep the more complete version
   d. After successful merge, delete the feature branch:
      `git branch -d <feature-branch>`
      `git push origin --delete <feature-branch>` (if it exists on remote)
6. **Push the updated default branch**: `git push origin {default_branch}`
7. **Pull updates before moving to the next workspace** to ensure each subsequent
   workspace has the latest merged changes.

### CRITICAL Rules:
- Process workspaces ONE AT A TIME, in the order listed above
- ALWAYS pull the latest `{default_branch}` before starting work on each workspace
- Preserve ALL feature work unless it's genuinely duplicated or made irrelevant
- After completion, every workspace should be on `{default_branch}` with no feature branches
- If a workspace has no feature branches, just ensure it's on `{default_branch}` and up to date
- Commit and push after merging each feature branch (don't batch)

### Error Handling:
- If a merge has unresolvable conflicts, document what you tried and move on
- If a workspace path doesn't exist or isn't a git repo, skip it and report the issue
- Always try to complete as many workspaces as possible even if some fail
"""

            # Acquire a workspace for the merge agent to execute in.
            workspace = None
            try:
                workspace = await self._prepare_workspace(task, agent)
            except Exception as e:
                logger.error("Sync workflow %s: workspace prep failed: %s", task.id, e)

            if not workspace:
                # Try to find any workspace path to use as working dir
                if workspaces:
                    workspace = workspaces[0].workspace_path
                    logger.warning(
                        "Sync workflow %s: using fallback workspace %s", task.id, workspace
                    )
                else:
                    await _notify(
                        f"❌ **Sync `{task.id}`** — No workspace available for merge agent."
                    )
                    await self.db.transition_task(
                        action.task_id, TaskStatus.FAILED, context="no_workspace_for_merge"
                    )
                    await self._emit_task_failure(
                        task,
                        "no_workspace_for_merge",
                        error="No workspace available for merge agent",
                    )
                    return

            # Launch the Claude Code agent for the merge work.
            if not self._adapter_factory:
                await _notify(f"❌ **Sync `{task.id}`** — No adapter factory configured.")
                await self.db.transition_task(
                    action.task_id, TaskStatus.FAILED, context="no_adapter_factory"
                )
                await self._emit_task_failure(
                    task, "no_adapter_factory", error="No agent adapter factory configured"
                )
                return

            profile = await self._resolve_profile(task)
            adapter = self._adapter_factory.create("claude", profile=profile)
            self._adapters[action.agent_id] = adapter

            ctx = TaskContext(
                task_id=task.id,
                description=merge_description,
                checkout_path=workspace,
                branch_name=default_branch,
            )

            # Create a thread for streaming output via event.
            start_msg = (
                f"🔄 **Sync Workspaces** — Merge agent starting\n"
                f"Task: `{task.id}` | Agent: {agent.name}"
            )
            thread_name = f"{task.id} | Sync Workspaces"[:100]
            await self._emit_notify(
                "notify.task_thread_open",
                TaskThreadOpenEvent(
                    task_id=task.id,
                    thread_name=thread_name,
                    initial_message=start_msg,
                    project_id=action.project_id,
                ),
            )

            await adapter.start(ctx)

            # Stream agent output via event.
            async def forward_msg(text: str) -> None:
                await self._emit_notify(
                    "notify.task_message",
                    TaskMessageEvent(
                        task_id=task.id,
                        message=text,
                        message_type="agent_output",
                        project_id=action.project_id,
                    ),
                )

            output = await adapter.wait(on_message=forward_msg)

            # Record token usage.
            if output.tokens_used > 0:
                await self.db.record_token_usage(
                    project_id=action.project_id,
                    task_id=task.id,
                    agent_id=action.agent_id,
                    tokens=output.tokens_used,
                )

            merge_succeeded = output.result == AgentResult.COMPLETED
            if merge_succeeded:
                logger.info("Sync workflow %s: merge completed successfully", task.id)
            else:
                logger.warning("Sync workflow %s: merge agent result=%s", task.id, output.result)
                await _notify(
                    f"⚠️ **Sync `{task.id}`** — Merge agent finished with "
                    f"result: {output.result.value}. Proceeding to cleanup."
                )

        finally:
            # ── Phase 4: Cleanup & Resume ───────────────────────────────
            await _notify(f"🔄 **Sync `{task.id}`** — Phase 4/4: Cleanup & resume…")

            # Release all workspace locks for this project (belt and suspenders).
            for ws in workspaces:
                if ws.locked_by_task_id == task.id:
                    try:
                        await self.db.release_workspace(ws.id)
                    except Exception:
                        pass

            # Also release via standard task cleanup (worktree-aware).
            await self._release_workspaces_for_task(action.task_id)

            # Resume the project.
            await self.db.update_project(action.project_id, status=ProjectStatus.ACTIVE)
            logger.info("Sync workflow %s: project %s resumed", task.id, action.project_id)

            # Free the agent.
            post_agent = await self.db.get_agent(action.agent_id)
            next_state = (
                AgentState.PAUSED
                if post_agent and post_agent.state == AgentState.PAUSED
                else AgentState.IDLE
            )
            await self.db.update_agent(action.agent_id, state=next_state, current_task_id=None)

            # Remove adapter reference.
            self._adapters.pop(action.agent_id, None)

            # Complete or fail the sync task.
            try:
                current_task = await self.db.get_task(task.id)
                if current_task and current_task.status == TaskStatus.IN_PROGRESS:
                    if merge_succeeded:
                        await self.db.transition_task(
                            action.task_id, TaskStatus.COMPLETED, context="sync_completed"
                        )
                        await _notify(
                            f"✅ **Sync Workspaces completed:** `{task.id}` — "
                            f"All workspaces synchronized to `{default_branch}`."
                        )
                    else:
                        await self.db.transition_task(
                            action.task_id,
                            TaskStatus.COMPLETED,
                            context="sync_completed_with_warnings",
                        )
                        await _notify(
                            f"⚠️ **Sync Workspaces finished:** `{task.id}` — "
                            f"Completed with warnings. Check thread for details."
                        )
            except Exception as e:
                logger.error("Sync workflow %s: final status update failed: %s", task.id, e)
