"""Task-scoped worktree file preview endpoints.

* ``GET /api/tasks/{task_id}/files`` — list of files changed on the task's
  branch vs its merge base with the project's default branch, with per-file
  additions/deletions/status.

The workspace-for-task mapping is the DB row
:func:`Database.get_workspace_for_task` returns — the workspace currently
locked by the task.  When a task is between assignments (no lock held),
the files endpoint returns an empty list with ``reason: "no_workspace"``
rather than 404; the sidebar renders "no worktree attached" in that case.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.api import dependencies as deps

logger = logging.getLogger(__name__)

__all__ = ["build_task_files_router", "router"]

MAX_FILE_BYTES = 512 * 1024  # 512 KB


def _parse_numstat_and_status(numstat_out: str, name_status_out: str) -> list[dict]:
    """Merge ``git diff --numstat`` and ``git diff --name-status`` outputs."""
    status_by_path: dict[str, str] = {}
    for line in name_status_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # R100 old\tnew, C075 old\tnew — collapse to the destination path
        code = parts[0][0]
        path = parts[-1]
        status_by_path[path] = code

    files: list[dict] = []
    for line in numstat_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        adds_s, dels_s, path = parts[0], parts[1], parts[-1]
        # numstat renders binary as "-\t-"
        adds = int(adds_s) if adds_s.isdigit() else 0
        dels = int(dels_s) if dels_s.isdigit() else 0
        files.append({
            "path": path,
            "additions": adds,
            "deletions": dels,
            "status": status_by_path.get(path, "M"),
        })
    return files


async def _resolve_base_ref(git, workspace: str, default_branch: str) -> str:
    """Pick the diff base: ``origin/<default>`` if present, else ``<default>``."""
    if await git.ahas_remote(workspace):
        return f"origin/{default_branch}"
    return default_branch


def build_task_files_router() -> APIRouter:
    """Router factory — mirrors ``build_sessions_router`` for testability."""
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/files")
    async def list_files(task_id: str):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        ws = await orch.db.get_workspace_for_task(task_id)
        if ws is None or not ws.workspace_path:
            return {
                "success": True,
                "files": [],
                "base": None,
                "workspace_path": None,
                "reason": "no_workspace",
            }

        workspace = ws.workspace_path
        git = orch.git
        if not await git.avalidate_checkout(workspace):
            return {
                "success": True,
                "files": [],
                "base": None,
                "workspace_path": workspace,
                "reason": "not_a_git_checkout",
            }

        project = await orch.db.get_project(task.project_id)
        default_branch = (
            getattr(project, "repo_default_branch", None) or "main"
        )
        base_ref = await _resolve_base_ref(git, workspace, default_branch)
        branch = task.branch_name or "HEAD"

        # Merge-base is the correct diff origin: it isolates the task's
        # changes from unrelated commits landed on default since the branch
        # was cut.  Fall through to the raw ref when merge-base fails.
        try:
            mb = (await git._arun(
                ["merge-base", base_ref, branch], cwd=workspace
            )).strip()
            diff_from = mb or base_ref
        except Exception:
            diff_from = base_ref

        try:
            numstat = await git._arun(
                ["diff", "--numstat", f"{diff_from}..{branch}"], cwd=workspace
            )
            name_status = await git._arun(
                ["diff", "--name-status", f"{diff_from}..{branch}"], cwd=workspace
            )
        except Exception as e:
            logger.warning("task-files diff failed for %s: %s", task_id, e)
            return {
                "success": True,
                "files": [],
                "base": base_ref,
                "workspace_path": workspace,
                "reason": "diff_failed",
            }

        return {
            "success": True,
            "files": _parse_numstat_and_status(numstat, name_status),
            "base": base_ref,
            "workspace_path": workspace,
        }

    # /file endpoint added in Task 2.
    return router


router = build_task_files_router()
