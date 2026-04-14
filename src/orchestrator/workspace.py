"""Workspace mixin — workspace preparation, branch/worktree creation, cleanup."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.git.manager import GitError, GitManager
from src.models import (
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceMode,
)

logger = logging.getLogger(__name__)


class WorkspaceMixin:
    """Workspace management methods mixed into Orchestrator."""

    @staticmethod
    def _remove_sentinel(workspace: str) -> None:
        """Remove the .agent-queue-lock sentinel file from a workspace."""
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        try:
            os.remove(sentinel)
        except OSError:
            pass

    async def _prepare_workspace(self, task: Task, agent) -> str | None:
        """Acquire a workspace lock and prepare it for the task.

        Steps:
        1. Acquire an unlocked workspace for the project via
           ``db.acquire_workspace()``.  This is an atomic DB operation that
           sets ``locked_by_agent_id`` — only one agent can hold a workspace.
        2. If no workspace is available and ``lock_mode`` is
           ``BRANCH_ISOLATED``, try to share a workspace that is already
           locked with ``BRANCH_ISOLATED`` by creating a git worktree.
        3. If still no workspace, return ``None`` (caller returns the task
           to READY and frees the agent).
        4. Determine the branch name:
           - Root tasks: generate a fresh branch from task ID + title.
           - Plan subtasks: reuse the parent task's branch name so all
             steps accumulate commits on a single shared branch.
        5. Perform git operations based on ``workspace.source_type``:
           - CLONE: orchestrator manages the full clone lifecycle (clone on
             first use, fetch + branch on subsequent uses).
           - LINK: workspace points to a pre-existing local checkout;
             orchestrator only manages branch operations, never clones.
           - WORKTREE: workspace is a git worktree of another workspace;
             git fetch is serialized via a per-repo mutex.
        6. Return the workspace path.

        Error resilience: git failures (network issues, auth errors) are
        caught and reported via Discord but do NOT prevent the workspace
        from being returned.  The agent can still work in the directory —
        it just won't have proper branch management.
        """
        project = await self.db.get_project(task.project_id)
        lock_mode = task.workspace_mode or WorkspaceMode.EXCLUSIVE

        # Directory-isolated mode is stubbed but not yet implemented.
        # Reject early with a clear message rather than silently falling
        # through to exclusive-like behavior.
        # See docs/specs/design/agent-coordination.md §7 (Workspace Strategy).
        if lock_mode == WorkspaceMode.DIRECTORY_ISOLATED:
            raise RuntimeError(
                "workspace_mode='directory-isolated' is not yet implemented. "
                "This mode is reserved for future monorepo support where multiple "
                "agents work on the same branch in different directories. "
                "Use 'exclusive' (default) or 'branch-isolated' instead."
            )

        ws = await self.db.acquire_workspace(
            task.project_id,
            agent.id,
            task.id,
            preferred_workspace_id=task.preferred_workspace_id,
            lock_mode=lock_mode,
        )

        # Branch-isolated fallback: when no unlocked workspace is available,
        # share an existing BRANCH_ISOLATED workspace via a git worktree.
        if not ws and lock_mode == WorkspaceMode.BRANCH_ISOLATED:
            ws = await self._create_branch_isolated_worktree(task, agent, project)

        if not ws:
            return None

        workspace = ws.workspace_path
        is_worktree = ws.source_type == RepoSourceType.WORKTREE

        # Register a git mutex for branch-isolated workspaces.  This ensures
        # that all shared git operations (fetch, gc, pull) routed through
        # GitManager._arun are serialized for this workspace and any
        # worktrees derived from it.  The mutex is keyed by the base
        # workspace path (for worktrees, the parent repo; otherwise the
        # workspace itself).
        if lock_mode == WorkspaceMode.BRANCH_ISOLATED:
            base = self._get_worktree_base_path(workspace) if is_worktree else None
            mutex_key = base if base else workspace
            self._git_mutex(mutex_key)  # ensure the lock exists in the dict

        # Layer 2: Filesystem sentinel — detect concurrent access that slipped
        # past the DB-level path lock (e.g. race condition, stale lock).
        # If the sentinel's owner task is no longer IN_PROGRESS, the sentinel
        # is stale (left behind by a crash) and safe to remove.
        #
        # For WORKTREE workspaces, each worktree has its own directory and
        # its own sentinel — no conflict with the base workspace's sentinel.
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        if os.path.exists(sentinel):
            try:
                with open(sentinel) as f:
                    owner_info = f.read().strip()
            except OSError:
                owner_info = "(unreadable)"

            # Check if the sentinel owner is still actively running.
            # Sentinel format: "task_id\nagent_id\n"
            owner_task_id = owner_info.split("\n")[0] if owner_info else ""
            owner_active = False
            if owner_task_id:
                owner_task = await self.db.get_task(owner_task_id)
                owner_active = (
                    owner_task is not None and owner_task.status == TaskStatus.IN_PROGRESS
                )

            if owner_active:
                logger.warning(
                    "Workspace %s has active sentinel (owner: %s) — releasing lock",
                    workspace,
                    owner_info,
                )
                await self._release_workspace_and_cleanup(ws)
                return None
            else:
                # Stale sentinel from a crashed/completed task — clean it up
                logger.info(
                    "Workspace %s has stale sentinel (owner: %s) — removing",
                    workspace,
                    owner_info,
                )
                self._remove_sentinel(workspace)
        # Write our sentinel
        try:
            os.makedirs(workspace, exist_ok=True)
            with open(sentinel, "w") as f:
                f.write(f"{task.id}\n{agent.id}\n")
        except OSError as e:
            logger.warning("Failed to write sentinel to %s: %s", workspace, e)

        repo_url = project.repo_url if project else ""
        default_branch = await self._get_default_branch(project, workspace)

        # Branch naming strategy:
        # - Root tasks get a fresh branch derived from their ID + title.
        # - Plan subtasks REUSE their parent task's branch name so all
        #   steps in the chain accumulate commits on a single branch.
        #   This is what enables the "one PR for the whole plan" workflow
        #   in _complete_workspace.
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            branch_name = (
                parent.branch_name
                if parent and parent.branch_name
                else GitManager.make_branch_name(task.id, task.title)
            )
        else:
            branch_name = GitManager.make_branch_name(task.id, task.title)

        # Git operations may fail (network issues, auth errors, merge
        # conflicts) but should never prevent returning the workspace path.
        # The agent can still work in the directory; it just won't have
        # proper branch management.  Errors are reported via Discord
        # notification so operators are aware.
        try:
            if is_worktree:
                # WORKTREE: Created by _create_branch_isolated_worktree().
                # The worktree directory and branch already exist.
                # Fetch is automatically serialized by the GitManager lock
                # provider — no need for explicit mutex acquisition here.
                base_path = self._get_worktree_base_path(workspace)
                if base_path and await self.git.ahas_remote(base_path):
                    await self.git._arun(["fetch", "origin"], cwd=base_path)
            else:
                # Workspace source types determine the git setup strategy:
                #
                # CLONE: The orchestrator manages the full clone lifecycle.
                #   - First use: clone from repo_url into the workspace path.
                #   - Subsequent uses: fetch + ensure clean default branch.
                #
                # LINK: The workspace points to a pre-existing local checkout
                #   (e.g. the developer's own repo).  The orchestrator only
                #   validates the directory exists.
                #
                # In both cases the orchestrator no longer creates task branches.
                # The agent receives branch-name + default-branch in its prompt
                # and handles branch creation, merge, and push itself.
                if ws.source_type == RepoSourceType.CLONE:
                    if not await self.git.avalidate_checkout(workspace):
                        os.makedirs(os.path.dirname(workspace), exist_ok=True)
                        if repo_url:
                            await self.git.acreate_checkout(repo_url, workspace)
                        # Re-detect default branch now that the repo is cloned.
                        # The initial detection (above) may have fallen back to
                        # "main" because the workspace didn't exist yet.
                        default_branch = await self._get_default_branch(project, workspace)

                elif ws.source_type == RepoSourceType.LINK:
                    if not os.path.isdir(workspace):
                        await self._emit_text_notify(
                            f"**Warning:** Linked workspace path `{workspace}` does not exist.",
                            project_id=task.project_id,
                        )

                # Ensure workspace is on a clean, up-to-date default branch.
                # The agent will create/switch to the task branch per its prompt.
                if await self.git.avalidate_checkout(workspace):
                    # Discard any uncommitted changes left by a previous task
                    # so the checkout to default_branch doesn't fail due to
                    # conflicts.  This is especially important for LINK
                    # workspaces without remotes, where no hard-reset follows.
                    #
                    # We first abort any in-progress operations (merge/rebase)
                    # and remove stale lock files, then force-clean the workspace.
                    # The old approach (checkout -- . + clean -fd) failed when
                    # the workspace was in a mid-merge/rebase state or had
                    # staged-but-uncommitted changes.
                    if await self.git.ahas_uncommitted_changes(workspace):
                        try:
                            await self.git.aforce_clean_workspace(workspace)
                        except GitError:
                            pass  # Best-effort cleanup
                    # Fetch is automatically serialized by the GitManager
                    # lock provider for branch-isolated workspaces.
                    if await self.git.ahas_remote(workspace):
                        await self.git._arun(["fetch", "origin"], cwd=workspace)
                    try:
                        await self.git._arun(["checkout", default_branch], cwd=workspace)
                    except GitError:
                        pass  # May already be on default branch
                    if await self.git.ahas_remote(workspace):
                        await self.git._arun(
                            ["reset", "--hard", f"origin/{default_branch}"],
                            cwd=workspace,
                        )

            # Update task branch in DB
            await self.db.update_task(task.id, branch_name=branch_name)
        except Exception as e:
            # Layer 3: Git failure means no launch — release workspace and
            # clean up the sentinel so another task can use this workspace.
            logger.error("Git setup failed for task %s in %s: %s", task.id, workspace, e)
            await self._emit_text_notify(
                f"**Git Error:** Task `{task.id}` — branch setup failed: {e}\n"
                f"Workspace released. Task will retry when a workspace is available.",
                project_id=task.project_id,
            )
            self._remove_sentinel(workspace)
            await self._release_workspace_and_cleanup(ws)
            return None

        # Clean up ALL plan files from previous tasks to prevent:
        # 1. A new task from discovering a stale plan that belongs to another task
        # 2. A new task failing to write its own plan because the file already exists
        # This covers both archived plans (.claude/plans/) and primary plan files
        # (.claude/plan.md, plan.md, etc.).
        await self._cleanup_plan_files_before_task(
            workspace, task.id, branch_name=branch_name, default_branch=default_branch
        )

        return workspace

    async def _create_branch_isolated_worktree(
        self,
        task: Task,
        agent,
        project,
    ) -> Workspace | None:
        """Create a git worktree for branch-isolated workspace sharing.

        Called when ``lock_mode=BRANCH_ISOLATED`` and no unlocked workspace
        is available.  Finds an existing workspace locked with
        ``BRANCH_ISOLATED``, creates a git worktree from it, registers a
        new workspace record (``source_type=WORKTREE``), and locks it for
        the requesting agent.

        The worktree path convention is::

            <parent_dir>/.worktrees-<base_name>/<branch-slug>/

        where ``base_name`` is the basename of the base workspace and
        ``branch-slug`` is derived from the task ID and title.

        Returns the locked worktree workspace, or ``None`` if no shareable
        base workspace was found.
        """
        from src.workspace_names import generate_workspace_id

        base_ws = await self.db.find_branch_isolated_base(task.project_id)
        if not base_ws:
            return None

        branch_name = GitManager.make_branch_name(task.id, task.title)
        # Derive a filesystem-safe slug for the worktree directory
        slug = GitManager.slugify(f"{task.id}-{task.title}")
        base_dir = os.path.dirname(base_ws.workspace_path)
        base_name = os.path.basename(base_ws.workspace_path)
        worktree_path = os.path.join(base_dir, f".worktrees-{base_name}", slug)

        try:
            # Serialize worktree creation via the git mutex to prevent
            # concurrent modifications to the shared .git/worktrees/ dir.
            async with self._git_mutex(base_ws.workspace_path):
                await self.git.acreate_worktree(base_ws.workspace_path, worktree_path, branch_name)
        except GitError as e:
            logger.error(
                "Failed to create worktree for task %s from %s: %s",
                task.id,
                base_ws.workspace_path,
                e,
            )
            return None

        # Register a workspace record for the worktree and lock it.
        ws_id = await generate_workspace_id(self.db)
        worktree_ws = Workspace(
            id=ws_id,
            project_id=task.project_id,
            workspace_path=worktree_path,
            source_type=RepoSourceType.WORKTREE,
            name=f"worktree:{base_ws.id}",
        )
        await self.db.create_workspace(worktree_ws)
        ws = await self.db.acquire_workspace(
            task.project_id,
            agent.id,
            task.id,
            preferred_workspace_id=ws_id,
            lock_mode=WorkspaceMode.BRANCH_ISOLATED,
        )

        if ws:
            logger.info(
                "Created branch-isolated worktree %s for task %s (base: %s)",
                worktree_path,
                task.id,
                base_ws.id,
            )
        return ws

    @staticmethod
    def _get_worktree_base_path(worktree_path: str) -> str | None:
        """Derive the base workspace path from a worktree path.

        Worktree paths follow the convention::

            <parent_dir>/.worktrees-<base_name>/<slug>/

        Returns the base workspace path, or ``None`` if the path doesn't
        match the convention.
        """
        parent = os.path.dirname(worktree_path.rstrip("/"))
        worktrees_dir = os.path.basename(parent)
        if worktrees_dir.startswith(".worktrees-"):
            base_name = worktrees_dir[len(".worktrees-") :]
            base_dir = os.path.dirname(parent)
            return os.path.join(base_dir, base_name)
        return None

    async def _release_workspace_and_cleanup(self, ws: Workspace) -> None:
        """Release a workspace lock and clean up worktrees if applicable.

        For regular workspaces, this just releases the DB lock.
        For WORKTREE workspaces, it also:
        - Removes the git worktree from the base repo
        - Deletes the dynamically created workspace record
        """
        if ws.source_type == RepoSourceType.WORKTREE:
            await self._cleanup_worktree_workspace(ws)
        else:
            await self.db.release_workspace(ws.id)

    async def _cleanup_worktree_workspace(self, ws: Workspace) -> None:
        """Remove a git worktree and delete its workspace record."""
        base_path = self._get_worktree_base_path(ws.workspace_path)
        if base_path:
            try:
                # Serialize worktree removal via the git mutex to prevent
                # concurrent modifications to the shared .git/worktrees/ dir.
                async with self._git_mutex(base_path):
                    await self.git.aremove_worktree(base_path, ws.workspace_path)
            except GitError as e:
                logger.warning("Failed to remove worktree %s: %s", ws.workspace_path, e)
                # Best-effort: try to remove the directory directly
                import shutil

                try:
                    shutil.rmtree(ws.workspace_path, ignore_errors=True)
                except Exception:
                    pass
        await self.db.release_workspace(ws.id)
        await self.db.delete_workspace(ws.id)
        logger.info("Cleaned up worktree workspace %s at %s", ws.id, ws.workspace_path)

    async def _release_workspaces_for_task(self, task_id: str) -> None:
        """Release all workspace locks for a task, cleaning up worktrees.

        Wraps ``db.release_workspaces_for_task()`` with worktree awareness.
        For tasks that used branch-isolated worktrees, this removes the
        git worktree and deletes the dynamically created workspace record
        before releasing locks.  Regular workspaces are released normally.
        """
        # Find all workspaces locked by this task
        all_ws = await self.db.list_workspaces()
        worktree_ws = [
            ws
            for ws in all_ws
            if ws.locked_by_task_id == task_id and ws.source_type == RepoSourceType.WORKTREE
        ]

        # Clean up worktree workspaces first (remove git worktree + delete record)
        for ws in worktree_ws:
            self._remove_sentinel(ws.workspace_path)
            await self._cleanup_worktree_workspace(ws)

        # Release any remaining (non-worktree) workspaces via bulk operation
        await self.db.release_workspaces_for_task(task_id)

    async def _cleanup_plan_files_before_task(
        self,
        workspace: str,
        task_id: str,
        *,
        branch_name: str | None = None,
        default_branch: str = "main",
    ) -> None:
        """Remove all plan files from previous tasks before starting a new one.

        Cleans up:

        1. **Primary plan files** (``.claude/plan.md``, ``plan.md``, etc.) on
           the current (default) branch — leftover files can cause agents to
           fail to write their own plan ("file already exists") or lead to
           stale plan re-discovery.
        2. **Archived plan files** (``.claude/plans/<task_id>-plan.md``) —
           clearly attributable to specific tasks via their filename prefix.
           Any that don't belong to the current task are removed.
        3. **Plan files on the task branch** (if ``branch_name`` is provided
           and the branch already exists locally or as a remote tracking
           branch).  When a task is retried, or a plan subtask follows a
           sibling, the agent may have committed plan files directly to the
           task branch via ``git add && git commit``.  Those files would
           reappear when the new agent checks out the branch.

        .. note::

           Deletions are committed using direct ``git add -A`` + ``git commit``
           instead of :meth:`GitManager.acommit_all`, because ``acommit_all``
           excludes plan files from commits (via ``_PLAN_FILE_EXCLUDES``).
        """
        import glob as _glob

        plan_patterns = self.config.auto_task.plan_file_patterns

        def _remove_plan_files() -> bool:
            """Delete primary plan files + archived plans.  Returns True if any deleted."""
            deleted = False
            for pattern in plan_patterns:
                full_pattern = os.path.join(workspace, pattern)
                for fpath in _glob.glob(full_pattern):
                    if os.path.isfile(fpath):
                        try:
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed plan file %s (task %s)",
                                fpath,
                                task_id,
                            )
                        except OSError as e:
                            logger.warning(
                                "Pre-task cleanup: failed to remove plan file %s: %s",
                                fpath,
                                e,
                            )

            plans_dir = os.path.join(workspace, ".claude", "plans")
            if os.path.isdir(plans_dir):
                try:
                    for entry in os.listdir(plans_dir):
                        if entry.startswith(task_id):
                            continue
                        fpath = os.path.join(plans_dir, entry)
                        if os.path.isfile(fpath) and entry.endswith(".md"):
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed archived plan %s (task %s)",
                                fpath,
                                task_id,
                            )
                except OSError as e:
                    logger.warning(
                        "Pre-task cleanup: failed to clean plans dir %s: %s",
                        plans_dir,
                        e,
                    )
            return deleted

        async def _commit_plan_deletions(msg: str) -> None:
            """Commit all pending changes including plan file deletions.

            Uses direct ``git add -A`` + ``git commit`` instead of
            ``acommit_all`` which excludes plan files via
            ``_PLAN_FILE_EXCLUDES``.
            """
            try:
                if not await self.git.avalidate_checkout(workspace):
                    return
                await self.git._arun(["add", "-A"], cwd=workspace)
                result = await self.git._arun_subprocess(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=workspace,
                    timeout=self.git._GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    await self.git._arun(["commit", "-m", msg], cwd=workspace)
            except Exception as e:
                logger.warning("Pre-task cleanup: failed to commit: %s", e)

        # ── 1. Clean current (default) branch ─────────────────────────────
        if _remove_plan_files():
            await _commit_plan_deletions(
                f"chore: clean up stale plan files before task {task_id}",
            )

        # ── 2. Clean the task branch if it already exists ─────────────────
        # When a task is retried (or a plan subtask follows a sibling),
        # the task branch may carry plan files committed by a previous
        # agent run.  Checkout that branch, purge plan files, commit the
        # deletion, and return to the default branch so the agent starts
        # from a clean state.
        if branch_name and await self.git.avalidate_checkout(workspace):
            switched = False
            try:
                await self.git._arun(["checkout", branch_name], cwd=workspace)
                switched = True
            except GitError:
                pass  # Branch does not exist — nothing to clean

            if switched:
                try:
                    if _remove_plan_files():
                        await _commit_plan_deletions(
                            f"chore: clean up stale plan files before task {task_id}",
                        )
                finally:
                    # Always return to the default branch even if cleanup failed.
                    try:
                        await self.git._arun(["checkout", default_branch], cwd=workspace)
                    except Exception as e:
                        logger.error(
                            "Pre-task cleanup: CRITICAL — failed to return to %s "
                            "after cleaning task branch %s: %s",
                            default_branch,
                            branch_name,
                            e,
                        )

    async def _cleanup_workspace_for_next_task(
        self,
        workspace: str | None,
        default_branch: str,
        task_id: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Ensure the workspace is clean and on the default branch.

        Called when a task reaches a terminal non-success state (BLOCKED,
        FAILED with retries exhausted) or when a task is reopened for
        verification retry, to prevent dirty workspace state from blocking
        or confusing the next task assigned to this workspace.

        Cleanup strategy (best-effort, most-conservative-first):
        0. Abort any in-progress git operations and remove stale lock files.
        1. Commit any uncommitted changes (preserves work).
        2. Stash if commit fails (preserves work, less accessible).
        3. Force-clean workspace as last resort (reset --hard + clean -fdx).
        4. Switch to the default branch.
        """
        if not workspace:
            return
        try:
            if not await self.git.avalidate_checkout(workspace):
                return
        except Exception:
            return

        # Step 0: Abort any in-progress operations and remove lock files
        # so that subsequent git commands don't fail due to stale state.
        try:
            await self.git.aabort_in_progress_operations(workspace)
        except Exception as e:
            logger.warning(
                "Task %s: abort in-progress operations failed during cleanup: %s",
                task_id,
                e,
            )

        # Step 1: Handle uncommitted changes
        try:
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if has_uncommitted:
                # Try to commit first (safest — preserves work).
                # Use --no-verify to bypass pre-commit hooks which can
                # reject the commit (e.g. ruff formatting) and prevent
                # workspace cleanup.
                try:
                    await self.git.acommit_all(
                        workspace,
                        f"auto-commit: workspace cleanup after task {task_id}",
                        exclude_plans=False,
                        no_verify=True,
                        event_bus=self.bus,
                        project_id=project_id,
                        agent_id=agent_id,
                    )
                    logger.info(
                        "Task %s: auto-committed changes during workspace cleanup",
                        task_id,
                    )
                except Exception:
                    # Commit failed — try stash (still preserves work)
                    try:
                        await self.git._arun(
                            [
                                "stash",
                                "--include-untracked",
                                "-m",
                                f"auto-stash: workspace cleanup for task {task_id}",
                            ],
                            cwd=workspace,
                        )
                        logger.info(
                            "Task %s: stashed changes during workspace cleanup",
                            task_id,
                        )
                    except Exception:
                        # Last resort — force-clean (reset --hard + clean -fdx)
                        try:
                            clean = await self.git.aforce_clean_workspace(workspace)
                            if clean:
                                logger.info(
                                    "Task %s: force-cleaned workspace during cleanup",
                                    task_id,
                                )
                            else:
                                logger.warning(
                                    "Task %s: force-clean did not fully clean workspace",
                                    task_id,
                                )
                        except Exception as force_err:
                            logger.warning(
                                "Task %s: all cleanup attempts failed: %s",
                                task_id,
                                force_err,
                            )
                            return
        except Exception as e:
            logger.warning(
                "Task %s: failed to check uncommitted changes during cleanup: %s",
                task_id,
                e,
            )

        # Step 2: Switch to default branch
        try:
            current = await self.git.aget_current_branch(workspace)
            if current != default_branch:
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                logger.info(
                    "Task %s: switched to '%s' during workspace cleanup",
                    task_id,
                    default_branch,
                )
        except Exception as e:
            logger.warning(
                "Task %s: failed to switch to '%s' during cleanup: %s",
                task_id,
                default_branch,
                e,
            )
