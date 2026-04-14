"""Agent commands mixin — agent listing, workspace management."""

from __future__ import annotations

import json
import logging
import os

from src.models import RepoSourceType, Task, TaskStatus, TaskType, Workspace
from src.task_names import generate_task_id
from src.workspace_names import generate_workspace_id
from src.commands.helpers import _run_subprocess

logger = logging.getLogger(__name__)


class AgentCommandsMixin:
    """Agent command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Agent commands -- workspace-as-agent model.
    # Agents are derived from project workspaces: each workspace is an
    # agent slot.  CRUD commands (create/delete/pause/resume) are deprecated
    # and return helpful error messages pointing to workspace commands.
    # -----------------------------------------------------------------------

    async def _cmd_list_agents(self, args: dict) -> dict:
        """List agent slots derived from project workspaces.

        Requires ``project_id`` (or active project).  Each workspace is an
        agent slot: locked workspaces are "busy", unlocked are "idle".
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (or set an active project)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        agent_list = []
        for ws in workspaces:
            if ws.locked_by_task_id:
                state = "busy"
                task = await self.db.get_task(ws.locked_by_task_id)
                info: dict = {
                    "workspace_id": ws.id,
                    "project_id": project_id,
                    "name": ws.name or ws.id,
                    "state": state,
                    "current_task_id": ws.locked_by_task_id,
                    "current_task_title": task.title if task else None,
                }
            else:
                info = {
                    "workspace_id": ws.id,
                    "project_id": project_id,
                    "name": ws.name or ws.id,
                    "state": "idle",
                    "current_task_id": None,
                    "current_task_title": None,
                }
            agent_list.append(info)
        return {"agents": agent_list, "project_id": project_id}

    async def _cmd_create_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces.

        Use ``add_workspace`` to add agent capacity to a project.
        """
        return {
            "error": (
                "create_agent is no longer supported. Agents are now derived "
                "from project workspaces. Use 'add_workspace' to add agent "
                "capacity to a project."
            )
        }

    async def _cmd_edit_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "edit_agent is no longer supported. Agents are derived from "
                "project workspaces. Use workspace management commands instead."
            )
        }

    async def _cmd_add_workspace(self, args: dict) -> dict:
        """Create a workspace for a project."""
        project_id = args["project_id"]
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        source = args.get("source", "clone")
        source_type = RepoSourceType(source)
        path = args.get("path")
        name = args.get("name")

        # Generate a human-readable workspace ID up front so it can double
        # as the checkout directory name when no explicit path is given.
        ws_id = await generate_workspace_id(self.db)

        if source_type == RepoSourceType.LINK:
            if not path:
                return {"error": "Link workspaces require a 'path' parameter"}
            # Always store as absolute path to prevent CWD-relative resolution issues
            path = os.path.realpath(path)
            if not os.path.isdir(path):
                return {"error": f"Path '{path}' does not exist or is not a directory"}
            # Reject if path is already a workspace for a different project
            all_ws = await self.db.list_workspaces()
            for ws in all_ws:
                if os.path.realpath(ws.workspace_path) == path and ws.project_id != project_id:
                    return {
                        "error": f"Path '{path}' is already a workspace for "
                        f"project '{ws.project_id}'"
                    }
        elif source_type == RepoSourceType.CLONE:
            if not path:
                # Auto-generate path under workspace_dir/{project_id}/
                # Use explicit name, or the human-readable workspace ID
                ws_dir_name = name or ws_id
                path = os.path.join(
                    self.config.workspace_dir,
                    project_id,
                    ws_dir_name,
                )
            # Always store as absolute path
            path = os.path.realpath(path)
            if project.repo_url:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    await self.orchestrator.git.acreate_checkout(project.repo_url, path)
                except Exception as e:
                    return {"error": f"Clone failed: {e}"}
        workspace = Workspace(
            id=ws_id,
            project_id=project_id,
            workspace_path=path,
            source_type=source_type,
            name=name,
        )
        await self.db.create_workspace(workspace)

        # Auto-populate project.repo_url from git remote if not already set
        result: dict = {
            "created": ws_id,
            "project_id": project_id,
            "workspace_path": path,
            "source_type": source,
        }
        if not project.repo_url:
            try:
                remote_url = await self.orchestrator.git.aget_remote_url(path)
                if remote_url:
                    await self.db.update_project(project_id, repo_url=remote_url)
                    result["auto_detected_repo_url"] = remote_url
            except Exception:
                pass  # Non-fatal — workspace was still created successfully
        return result

    async def _cmd_list_workspaces(self, args: dict) -> dict:
        """List workspaces with lock status."""
        project_id = args.get("project_id")
        if not project_id and self._active_project_id:
            project_id = self._active_project_id
        workspaces = await self.db.list_workspaces(project_id=project_id)
        return {
            "workspaces": [
                {
                    "id": ws.id,
                    "project_id": ws.project_id,
                    "workspace_path": ws.workspace_path,
                    "source_type": ws.source_type.value,
                    "name": ws.name,
                    "locked_by_agent_id": ws.locked_by_agent_id,
                    "locked_by_task_id": ws.locked_by_task_id,
                    "lock_mode": ws.lock_mode.value if ws.lock_mode else None,
                }
                for ws in workspaces
            ]
        }

    async def _cmd_remove_workspace(self, args: dict) -> dict:
        """Delete a workspace by ID or name."""
        workspace_ref = args.get("workspace_id") or args.get("workspace")
        if not workspace_ref:
            return {"error": "workspace_id or workspace is required"}

        # Try by ID first
        ws = await self.db.get_workspace(workspace_ref)

        # If not found by ID, try by name within a project
        if not ws:
            project_id = args.get("project_id") or self._active_project_id
            if project_id:
                ws = await self.db.get_workspace_by_name(project_id, workspace_ref)

        if not ws:
            return {"error": f"Workspace '{workspace_ref}' not found"}
        if ws.locked_by_agent_id:
            return {
                "error": f"Workspace '{ws.id}' is locked by agent "
                f"'{ws.locked_by_agent_id}'. Release it first."
            }
        await self.db.delete_workspace(ws.id)
        return {
            "deleted": ws.id,
            "name": ws.name,
            "project_id": ws.project_id,
            "workspace_path": ws.workspace_path,
        }

    async def _cmd_release_workspace(self, args: dict) -> dict:
        """Admin force-release a stuck workspace lock."""
        workspace_id = args["workspace_id"]
        ws = await self.db.get_workspace(workspace_id)
        if not ws:
            return {"error": f"Workspace '{workspace_id}' not found"}
        if not ws.locked_by_agent_id:
            return {"workspace_id": workspace_id, "status": "already_unlocked"}
        await self.db.release_workspace(workspace_id)
        return {
            "workspace_id": workspace_id,
            "released_from_agent": ws.locked_by_agent_id,
            "released_from_task": ws.locked_by_task_id,
        }

    async def _cmd_find_merge_conflict_workspaces(self, args: dict) -> dict:
        """Scan project workspaces for branches with merge conflicts against main.

        For each workspace, runs ``git merge-tree`` checks on all remote
        branches to detect conflicts without touching the worktree.  Returns a
        list of workspaces that contain conflicting branches, along with
        conflict details (branch name, conflicting files, commits behind).

        This enables the chat agent to create a task with
        ``preferred_workspace_id`` so the orchestrator assigns the exact
        workspace that needs conflict resolution instead of a random one.
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        default_branch = project.repo_default_branch or "main"
        results: list[dict] = []

        for ws in workspaces:
            ws_path = ws.workspace_path
            if not os.path.isdir(ws_path):
                continue

            # Check if this is a valid git repository
            git_dir = os.path.join(ws_path, ".git")
            if not os.path.exists(git_dir):
                continue

            try:
                # Fetch latest remote state
                await _run_subprocess(
                    "git",
                    "fetch",
                    "origin",
                    "--prune",
                    "--quiet",
                    cwd=ws_path,
                    timeout=30,
                )

                main_ref = f"origin/{default_branch}"

                # Verify main exists
                check_rc, _, _ = await _run_subprocess(
                    "git",
                    "rev-parse",
                    main_ref,
                    cwd=ws_path,
                    timeout=10,
                )
                if check_rc != 0:
                    continue

                # Get current branch
                cb_rc, cb_stdout, _ = await _run_subprocess(
                    "git",
                    "rev-parse",
                    "--abbrev-ref",
                    "HEAD",
                    cwd=ws_path,
                    timeout=10,
                )
                current_branch = cb_stdout.strip() if cb_rc == 0 else "unknown"

                # Check for uncommitted merge conflict markers in working tree
                has_working_tree_conflict = False
                status_rc, status_stdout, _ = await _run_subprocess(
                    "git",
                    "status",
                    "--porcelain",
                    cwd=ws_path,
                    timeout=10,
                )
                if status_rc == 0:
                    for line in status_stdout.splitlines():
                        if (
                            line.startswith("UU ")
                            or line.startswith("AA ")
                            or line.startswith("DD ")
                        ):
                            has_working_tree_conflict = True
                            break

                # List remote branches and check each for merge conflicts
                br_rc, br_stdout, _ = await _run_subprocess(
                    "git",
                    "branch",
                    "-r",
                    "--list",
                    "origin/*",
                    cwd=ws_path,
                    timeout=10,
                )
                if br_rc != 0:
                    continue

                branch_conflicts: list[dict] = []

                for line in br_stdout.splitlines():
                    branch_ref = line.strip()
                    if not branch_ref:
                        continue

                    branch_name = branch_ref.removeprefix("origin/")

                    # Skip main, HEAD, and dependabot branches
                    if branch_name in (default_branch, "HEAD") or branch_name.startswith(
                        "dependabot/"
                    ):
                        continue
                    if " -> " in branch_ref:
                        continue

                    # Find merge base
                    mb_rc, mb_stdout, _ = await _run_subprocess(
                        "git",
                        "merge-base",
                        main_ref,
                        branch_ref,
                        cwd=ws_path,
                        timeout=10,
                    )
                    if mb_rc != 0:
                        continue
                    merge_base = mb_stdout.strip()

                    # Use merge-tree to check for conflicts
                    _, mt_stdout, _ = await _run_subprocess(
                        "git",
                        "merge-tree",
                        merge_base,
                        main_ref,
                        branch_ref,
                        cwd=ws_path,
                        timeout=10,
                    )
                    merge_output = mt_stdout

                    if "+<<<<<<< " in merge_output:
                        # Extract conflicting files
                        conflicting_files = []
                        for mline in merge_output.splitlines():
                            if mline.startswith("changed in both"):
                                conflicting_files.append(
                                    mline.replace("changed in both", "").strip()
                                )

                        # Extract task ID from branch name
                        if "/" in branch_name:
                            task_id_part = branch_name.split("/")[0]
                        else:
                            task_id_part = branch_name

                        # Commits behind main
                        behind_rc, behind_stdout, _ = await _run_subprocess(
                            "git",
                            "rev-list",
                            "--count",
                            f"{branch_ref}..{main_ref}",
                            cwd=ws_path,
                            timeout=10,
                        )
                        behind_count = behind_stdout.strip() if behind_rc == 0 else "?"

                        branch_conflicts.append(
                            {
                                "branch": branch_name,
                                "task_id": task_id_part,
                                "conflicting_files": conflicting_files or ["unknown"],
                                "commits_behind_main": behind_count,
                            }
                        )

                if branch_conflicts or has_working_tree_conflict:
                    results.append(
                        {
                            "workspace_id": ws.id,
                            "workspace_name": ws.name,
                            "workspace_path": ws_path,
                            "current_branch": current_branch,
                            "locked_by_task_id": ws.locked_by_task_id,
                            "locked_by_agent_id": ws.locked_by_agent_id,
                            "has_working_tree_conflict": has_working_tree_conflict,
                            "branch_conflicts": branch_conflicts,
                        }
                    )

            except (asyncio.TimeoutError, OSError) as e:
                logging.getLogger(__name__).warning(
                    "Error scanning workspace %s for conflicts: %s",
                    ws_path,
                    e,
                )
                continue

        return {
            "project_id": project_id,
            "workspaces_scanned": len(workspaces),
            "workspaces_with_conflicts": len(results),
            "conflicts": results,
        }

    async def _cmd_queue_sync_workspaces(self, args: dict) -> dict:
        """Queue a high-priority Sync Workspaces task that orchestrates a full sync workflow.

        When the orchestrator picks up this task, it will:
        1. Pause the project (prevent new tasks from being queued).
        2. Wait for ALL currently running tasks to complete.
        3. Launch a Claude Code agent task to merge all feature branches
           into the default branch across all project workspaces.
        4. Resume the project after synchronization is complete.

        This is used for periodic workspace consolidation when feature branches
        have drifted from the default branch.

        Early-out conditions (no task queued):
        - A SYNC task already exists for this project (READY/ASSIGNED/IN_PROGRESS).
        - All workspaces are already on the default branch with no feature branches.
        """
        from src.task_names import generate_task_id

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        default_branch = project.repo_default_branch or "main"

        # ── Early-out: duplicate sync task ─────────────────────────────
        # If there's already a SYNC task queued or running for this project,
        # don't create another one.
        active_tasks = await self.db.list_active_tasks(
            project_id=project_id,
            exclude_statuses={TaskStatus.COMPLETED, TaskStatus.FAILED},
        )
        existing_sync = [
            t
            for t in active_tasks
            if t.task_type == TaskType.SYNC
            and t.status in (TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        ]
        if existing_sync:
            existing_ids = ", ".join(t.id for t in existing_sync)
            return {
                "already_queued": True,
                "existing_task_ids": [t.id for t in existing_sync],
                "project_id": project_id,
                "message": (
                    f"Sync Workspaces task already exists for '{project_id}': {existing_ids}. "
                    f"Skipping duplicate."
                ),
            }

        # ── Early-out: workspaces already synced ───────────────────────
        # Check if all workspaces are already on the default branch with no
        # feature branches.  If so, there's nothing to merge — skip the sync.
        git = self.orchestrator.git
        needs_sync = False
        workspace_details = []
        for ws in workspaces:
            ws_path = ws.workspace_path
            if not os.path.isdir(ws_path):
                # Workspace path doesn't exist — can't check, assume needs sync
                needs_sync = True
                workspace_details.append(f"  - {ws_path}: directory not found")
                break
            try:
                current = await git.aget_current_branch(ws_path)
                branches = await git.alist_branches(ws_path)
                # alist_branches returns lines like "* main", "  feature-x"
                # Strip the leading "* " or "  " to get clean branch names.
                clean_branches = [b.lstrip("* ").strip() for b in branches if b.strip()]
                non_default = [b for b in clean_branches if b and b != default_branch]
                if current != default_branch or non_default:
                    needs_sync = True
                    workspace_details.append(
                        f"  - {ws_path}: branch={current}, other_branches={non_default or '(none)'}"
                    )
                    break
                workspace_details.append(f"  - {ws_path}: on {default_branch}, no feature branches")
            except Exception:
                # Git error — can't determine state, assume needs sync
                needs_sync = True
                workspace_details.append(f"  - {ws_path}: git check failed")
                break

        if not needs_sync:
            return {
                "already_synced": True,
                "project_id": project_id,
                "workspace_count": len(workspaces),
                "default_branch": default_branch,
                "message": (
                    f"All {len(workspaces)} workspace(s) for '{project_id}' are already on "
                    f"'{default_branch}' with no feature branches. No sync needed."
                ),
            }

        workspace_root = "/home/jkern/agent-queue-workspaces"

        # Build a self-contained description with all context for the sync workflow.
        workspace_paths = "\n".join(
            f"  - {ws.workspace_path} (id: {ws.id}, name: {ws.name or '—'})" for ws in workspaces
        )

        description = f"""## Sync Workspaces — {project_id}

    **Task Type:** Orchestrator-managed sync workflow (handled automatically by the orchestrator)

    ### Workflow Steps (executed by the orchestrator, NOT the agent):
    1. **Pause Project** — Prevent new tasks from being queued for `{project_id}`
    2. **Wait for Active Tasks** — Monitor all running tasks and wait for completion
    3. **Merge Feature Branches** — Launch a Claude Code agent to merge all feature work
    4. **Cleanup & Resume** — Unlock workspaces and resume the project

    ### Project Details:
    - **Project ID:** {project_id}
    - **Default Branch:** {default_branch}
    - **Workspace Root:** {workspace_root}

    ### Workspaces:
    {workspace_paths}

    ### Merge Strategy:
    - Merge and push one workspace at a time
    - Pull updates before working on subsequent workspaces
    - Preserve ALL feature work unless it's duplicated or made irrelevant by subsequent changes
    - Resolve conflicts intelligently, preferring to preserve feature functionality
    - Ensure each workspace ends up on `{default_branch}` with no remaining feature branches from agent-queue

    ### Why This Exists:
    This synchronizes workspaces that have drifted from the default branch, consolidating
    feature work stuck on feature branches across multiple workspaces.
    """

        task_id = await generate_task_id(self.db)
        task = Task(
            id=task_id,
            project_id=project_id,
            title=f"Sync Workspaces — {project_id}",
            description=description,
            priority=1,  # Highest priority
            status=TaskStatus.READY,
            task_type=TaskType.SYNC,
        )
        await self.db.create_task(task)

        return {
            "queued": task_id,
            "project_id": project_id,
            "title": task.title,
            "priority": 1,
            "workspace_count": len(workspaces),
            "default_branch": default_branch,
            "message": (
                f"Sync Workspaces task queued with highest priority (id: {task_id}). "
                f"When it starts, it will pause the project, wait for active tasks to "
                f"complete, merge all feature branches into '{default_branch}', then resume."
            ),
        }

    async def _cmd_pause_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "pause_agent is no longer supported. Agents are derived from "
                "project workspaces. To pause work, pause the project instead."
            )
        }

    async def _cmd_resume_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "resume_agent is no longer supported. Agents are derived from "
                "project workspaces. To resume work, resume the project instead."
            )
        }

    async def _cmd_delete_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "delete_agent is no longer supported. Agents are derived from "
                "project workspaces. Use 'remove_workspace' to remove agent "
                "capacity from a project."
            )
        }
