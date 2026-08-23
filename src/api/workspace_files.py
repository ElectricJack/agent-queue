"""Workspace-scoped file browsing endpoints (pane view: file-browser).

Two endpoints:

* ``GET /api/workspaces/{workspace_id}/browse?path=<relpath>`` — directory
  listing at ``path`` (default: workspace root).
* ``GET /api/workspaces/{workspace_id}/file?path=<relpath>`` — raw file
  content, delegating to the same path-safe helper
  ``/api/tasks/{id}/file`` uses (``src/api/file_serving.py``).

Unlike the task-files pair, these resolve a workspace directly by id
rather than through a task's current lock — this is what the file-browser
pane view (workspace-scoped, task-independent) needs.
"""
from __future__ import annotations

import logging
import stat as stat_module
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from src.api import dependencies as deps
from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.file_serving import serve_workspace_relative_file

logger = logging.getLogger(__name__)

__all__ = ["build_workspace_files_router", "router"]


def _require_workspace_scope(scope: RequestScope, workspace) -> None:
    """404 if the caller's RequestScope can't see workspace.project_id.

    404 (not 403) to avoid leaking workspace existence to a session scoped
    to a different project — matches the task-files endpoint's posture.
    """
    if scope.kind == "local":
        return
    if scope.elevated and scope.project_id is None:
        # Global admin (dashboard-shell-v2 spec §4.2): elevated + no
        # project filter — sees every workspace.
        return
    if scope.project_id == workspace.project_id:
        return
    raise HTTPException(status_code=404, detail=f"No workspace '{workspace.id}'")


def _resolve_relative_dir(root: Path, path: str) -> Path:
    """Path-safety for a directory target — same algorithm as the file
    helper's lexical + strict resolve passes, plus an is_dir() check."""
    if Path(path).is_absolute():
        raise HTTPException(status_code=403, detail="absolute path not allowed")

    lexical = (root / path).resolve()
    try:
        lexical.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    try:
        candidate = (root / path).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="directory not found")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=403, detail="path not accessible")

    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")

    return candidate


def build_workspace_files_router(*, db=None) -> APIRouter:
    """Router factory — mirrors ``build_graph_router``'s pattern.

    ``db`` is accepted for test-seam symmetry with other router factories,
    but the handlers below read the live orchestrator's ``db`` via
    ``deps._orchestrator`` at request time (matching ``task_files.py``'s
    pattern), so a caller passing a fixture ``db`` here still needs
    ``deps._orchestrator.db`` wired to that same instance — see the
    ``wired`` fixture in ``tests/test_workspace_files_api.py``.
    """
    router = APIRouter()

    @router.get("/api/workspaces/{workspace_id}/browse")
    async def browse(workspace_id: str, request: Request, path: str = Query("")):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        workspace = await orch.db.get_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"No workspace '{workspace_id}'")

        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_workspace_scope(scope, workspace)

        if not workspace.workspace_path:
            return {
                "success": True,
                "path": path,
                "entries": [],
                "reason": "no_workspace_path",
            }

        root = Path(workspace.workspace_path).resolve()
        candidate = _resolve_relative_dir(root, path)

        entries: list[dict] = []
        for dirent in candidate.iterdir():
            try:
                is_symlink = dirent.is_symlink()
                # Classify by resolved target (dir vs file); a broken
                # symlink raises here and is omitted from the listing.
                stat_result = dirent.stat()
            except OSError:
                continue

            if stat_module.S_ISDIR(stat_result.st_mode):
                entry: dict = {"name": dirent.name, "type": "dir"}
            elif stat_module.S_ISREG(stat_result.st_mode):
                entry = {
                    "name": dirent.name,
                    "type": "file",
                    "size": stat_result.st_size,
                }
            else:
                # Other types (sockets, fifos, etc.) are omitted.
                continue

            if is_symlink:
                entry["is_symlink"] = True
            entries.append(entry)

        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

        try:
            relative = str(candidate.relative_to(root))
        except ValueError:
            relative = path
        if relative == ".":
            relative = ""

        return {"success": True, "path": relative, "entries": entries}

    @router.get("/api/workspaces/{workspace_id}/file")
    async def get_file(workspace_id: str, request: Request, path: str = Query(...)):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        workspace = await orch.db.get_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"No workspace '{workspace_id}'")

        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_workspace_scope(scope, workspace)

        if not workspace.workspace_path:
            raise HTTPException(status_code=404, detail="workspace has no path")

        return await serve_workspace_relative_file(workspace.workspace_path, path)

    return router


router = build_workspace_files_router()
