"""Task attachment endpoints — screenshots and files pinned to a task.

Four endpoints:

* ``POST /api/tasks/{task_id}/attachments`` — multipart upload (image/* +
  PDF allowlist, size-capped) into the daemon-managed attachments dir
  ``<data_dir>/attachments/task-<task_id>/<uuid8>-<filename>``; the
  absolute path is appended to ``Task.attachments`` atomically.
* ``GET /api/tasks/{task_id}/attachments`` — the task's attachments with
  per-file metadata (filename, size, content type, existence).
* ``GET /api/tasks/{task_id}/attachment?path=…`` — raw file bytes with a
  proper content type, for inline thumbnails.  ``path`` must be an exact
  member of the task's attachments list — the DB list is the allowlist,
  so no traversal is possible regardless of what the path contains.
* ``DELETE /api/tasks/{task_id}/attachment?path=…`` — remove the path
  from the task; the file itself is only unlinked when it lives under the
  managed ``<data_dir>/attachments/`` root (never for externally-attached
  paths such as CLI-provided files).

Agents see the same paths through ``aq prime`` section 4 (task context),
which renders ``**attachments:**`` with one absolute path per line.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from src.api import dependencies as deps

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_ATTACHMENT_BYTES",
    "build_task_attachments_router",
    "router",
]

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

# Screenshots paste as image/png; drag-drop covers the other image types.
# SVG is deliberately excluded — served inline it can execute script in the
# dashboard origin.  PDF is allowed for design docs / printouts.
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

_CHUNK = 64 * 1024


def _sanitize_filename(name: str, content_type: str) -> str:
    """Collapse a client-supplied filename to a safe basename.

    Strips any directory components (traversal), then whitelists
    ``[A-Za-z0-9._-]``.  An empty or fully-mangled name (e.g. a clipboard
    paste with no filename) falls back to ``pasted<ext>`` for the type.
    """
    base = Path(name or "").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    if not base:
        base = "pasted" + ALLOWED_CONTENT_TYPES.get(content_type, "")
    return base[:120]


def _attachments_root(config) -> Path:
    return Path(config.data_dir) / "attachments"


def _attachment_meta(path: str) -> dict:
    p = Path(path)
    meta = {
        "path": path,
        "filename": p.name,
        "content_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
        "exists": p.is_file(),
        "size": None,
    }
    if meta["exists"]:
        try:
            meta["size"] = p.stat().st_size
        except OSError:
            meta["exists"] = False
    return meta


def build_task_attachments_router() -> APIRouter:
    """Router factory — mirrors ``build_task_files_router`` for testability."""
    router = APIRouter()

    async def _get_task_or_404(task_id: str):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        return orch, task

    @router.post("/api/tasks/{task_id}/attachments")
    async def upload_attachment(task_id: str, file: UploadFile):
        orch, task = await _get_task_or_404(task_id)

        content_type = (file.content_type or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"content type '{content_type or 'unknown'}' not allowed; "
                f"accepted: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}",
            )

        safe_name = _sanitize_filename(file.filename or "", content_type)
        target_dir = _attachments_root(orch.config) / f"task-{task_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid.uuid4().hex[:8]}-{safe_name}"

        written = 0
        try:
            with target.open("wb") as out:
                while chunk := await file.read(_CHUNK):
                    written += len(chunk)
                    if written > MAX_ATTACHMENT_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file exceeds {MAX_ATTACHMENT_BYTES} byte cap",
                        )
                    out.write(chunk)
            if written == 0:
                raise HTTPException(status_code=400, detail="empty upload")
            attachments = await orch.db.add_task_attachment(task_id, str(target))
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        logger.info("attachment uploaded for task %s: %s (%d bytes)", task_id, target, written)
        return {
            "success": True,
            "attachment": _attachment_meta(str(target)),
            "attachments": attachments,
        }

    @router.get("/api/tasks/{task_id}/attachments")
    async def list_attachments(task_id: str):
        _, task = await _get_task_or_404(task_id)
        paths = task.attachments or []
        return {
            "success": True,
            "attachments": [_attachment_meta(p) for p in paths],
        }

    @router.get("/api/tasks/{task_id}/attachment")
    async def get_attachment(task_id: str, path: str = Query(...)):
        _, task = await _get_task_or_404(task_id)
        # Exact membership in the task's attachments list is the allowlist;
        # anything else is forbidden no matter what the path looks like.
        if path not in (task.attachments or []):
            raise HTTPException(status_code=403, detail="path is not a task attachment")
        p = Path(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing on disk")
        media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=p.name,
                            content_disposition_type="inline")

    @router.delete("/api/tasks/{task_id}/attachment")
    async def delete_attachment(task_id: str, path: str = Query(...)):
        orch, task = await _get_task_or_404(task_id)
        if path not in (task.attachments or []):
            raise HTTPException(status_code=404, detail="path is not a task attachment")
        attachments = await orch.db.remove_task_attachment(task_id, path)

        # Only unlink files the daemon manages; externally-attached paths
        # (CLI, user files) are merely de-linked.
        removed_from_disk = False
        root = _attachments_root(orch.config).resolve()
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            pass
        else:
            try:
                resolved.unlink(missing_ok=True)
                removed_from_disk = True
                parent = resolved.parent
                if parent != root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError as e:
                logger.warning("attachment unlink failed for %s: %s", path, e)

        return {
            "success": True,
            "attachments": attachments,
            "removed_from_disk": removed_from_disk,
        }

    return router


router = build_task_attachments_router()
