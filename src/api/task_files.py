"""Task-scoped worktree file preview endpoints.

Two endpoints:

* ``GET /api/tasks/{task_id}/files`` — list of files changed on the task's
  branch vs its merge base with the project's default branch, with per-file
  additions/deletions/status.
* ``GET /api/tasks/{task_id}/file?path=<rel>`` — raw file bytes as
  ``text/plain``, path-restricted to the task's acquired workspace,
  size-capped at 512 KB.

The workspace-for-task mapping is the DB row
:func:`Database.get_workspace_for_task` returns — the workspace currently
locked by the task.  When a task is between assignments (no lock held),
the files endpoint returns an empty list with ``reason: "no_workspace"``
rather than 404; the sidebar renders "no worktree attached" in that case.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

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
            mb = await git.amerge_base(workspace, base_ref, branch)
            diff_from = mb or base_ref
        except Exception:
            diff_from = base_ref

        try:
            numstat = await git.aget_diff(
                workspace, diff_from, to_ref=branch, numstat=True
            )
            name_status = await git.aget_diff(
                workspace, diff_from, to_ref=branch, name_status=True
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

    @router.get("/api/tasks/{task_id}/file")
    async def get_file(task_id: str, path: str = Query(...)):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        ws = await orch.db.get_workspace_for_task(task_id)
        if ws is None or not ws.workspace_path:
            raise HTTPException(status_code=404, detail="task has no workspace")

        # ── Path safety ────────────────────────────────────────────────
        # Reject absolute paths outright — an absolute ``path`` would
        # cause ``root / path`` to discard ``root`` and jump anywhere.
        if Path(path).is_absolute():
            raise HTTPException(status_code=403, detail="absolute path not allowed")

        # Resolve BOTH sides, then verify containment.  We must resolve
        # before comparing so that symlink escapes and ``..`` segments
        # both collapse to their real target.  ``strict=True`` on the
        # file path turns a missing file into a FileNotFoundError we can
        # map to 404.
        root = Path(ws.workspace_path).resolve()

        # First pass: non-strict resolve of the *lexical* path so ``..``
        # segments collapse without touching the filesystem.  This
        # catches traversal even when the target doesn't exist, so a
        # missing ``../secret`` is a 403 (escape attempt) not a 404.
        lexical = (root / path).resolve()
        try:
            lexical.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes workspace")

        # Second pass: strict resolve to follow symlinks and error on
        # missing files.  A symlink whose real target lies outside the
        # workspace is a 403.
        try:
            candidate = (root / path).resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found")
        except (OSError, RuntimeError):
            # RuntimeError: symlink loop.  OSError: permission etc.
            raise HTTPException(status_code=403, detail="path not accessible")

        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes workspace")

        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not a regular file")

        try:
            size = candidate.stat().st_size
        except OSError:
            raise HTTPException(status_code=404, detail="file not stat-able")
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {MAX_FILE_BYTES} byte cap",
            )

        try:
            data = candidate.read_bytes()
        except OSError as e:
            raise HTTPException(status_code=404, detail=f"read failed: {e}")

        text = data.decode("utf-8", errors="replace")
        return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")

    return router


router = build_task_files_router()
