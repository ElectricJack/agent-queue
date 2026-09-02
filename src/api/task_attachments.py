"""Task screenshot upload, preview, and removal endpoints."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from src.api import dependencies as deps
from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.models.task import (
    TaskAttachmentDeleteResponse,
    TaskAttachmentResponse,
    TaskAttachmentsResponse,
)

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_ALLOWED_TYPES = {
    "image/png": ("PNG", ".png"),
    "image/jpeg": ("JPEG", ".jpg"),
    "image/gif": ("GIF", ".gif"),
    "image/webp": ("WEBP", ".webp"),
}
_TYPE_BY_SUFFIX = {extension: media_type for media_type, (_fmt, extension) in _ALLOWED_TYPES.items()}


def _require_task_scope(scope: RequestScope, task) -> None:
    if scope.kind == "local" or (scope.elevated and scope.project_id is None):
        return
    if scope.project_id != task.project_id:
        raise HTTPException(status_code=404, detail=f"No task '{task.id}'")
    if not scope.elevated and scope.task_id != task.id:
        raise HTTPException(status_code=404, detail=f"No task '{task.id}'")


def _task_directory(data_dir: str, task_id: str) -> Path:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-")[:48] or "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return Path(data_dir).expanduser().resolve() / "attachments" / f"task-{readable}-{digest}"


def _safe_stem(filename: str | None) -> str:
    raw = Path(filename or "screenshot").name
    stem = Path(raw).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return safe[:80] or "screenshot"


async def _write_upload(file: UploadFile, destination: Path) -> None:
    size = 0
    try:
        with destination.open("xb") as handle:
            while chunk := await file.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"attachment exceeds {MAX_ATTACHMENT_BYTES} byte cap",
                    )
                handle.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _verify_image(path: Path, claimed_type: str) -> None:
    expected_format, _extension = _ALLOWED_TYPES[claimed_type]
    try:
        with Image.open(path) as image:
            actual_format = image.format
            image.verify()
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="file is not a valid image") from exc
    if actual_format != expected_format:
        raise HTTPException(status_code=415, detail="image content does not match content type")


def _attachment_json(task_id: str, path: Path, media_type: str) -> dict:
    return {
        "id": path.name,
        "name": path.name.split("-", 1)[-1],
        "path": str(path),
        "url": f"/api/tasks/{task_id}/attachments/{path.name}",
        "content_type": media_type,
        "size": path.stat().st_size,
    }


def _linked_attachment(task, attachment_id: str) -> Path:
    if not attachment_id or Path(attachment_id).name != attachment_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    for linked in task.attachments:
        path = Path(linked)
        if path.name == attachment_id:
            return path
    raise HTTPException(status_code=404, detail="attachment not found")


def _media_type(path: Path) -> str:
    media_type = _TYPE_BY_SUFFIX.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="attachment is not a previewable image")
    return media_type


def build_task_attachments_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/tasks/{task_id:path}/attachments",
        response_model=TaskAttachmentResponse,
        status_code=201,
    )
    async def upload_attachment(task_id: str, request: Request, file: UploadFile):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_task_scope(scope, task)

        media_type = (file.content_type or "").lower()
        if media_type not in _ALLOWED_TYPES:
            await file.close()
            raise HTTPException(
                status_code=415,
                detail="only PNG, JPEG, GIF, and WebP images are allowed",
            )

        directory = _task_directory(orch.config.data_dir, task_id)
        directory.mkdir(parents=True, exist_ok=True)
        extension = _ALLOWED_TYPES[media_type][1]
        destination = directory / f"{uuid4().hex}-{_safe_stem(file.filename)}{extension}"
        try:
            await _write_upload(file, destination)
            _verify_image(destination, media_type)
            attachments = await orch.db.append_task_attachment(task_id, str(destination))
            if attachments is None:
                raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {"success": True, "attachment": _attachment_json(task_id, destination, media_type)}

    @router.get(
        "/api/tasks/{task_id:path}/attachments",
        response_model=TaskAttachmentsResponse,
    )
    async def list_attachments(task_id: str, request: Request):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_task_scope(scope, task)
        attachments = []
        for linked in task.attachments:
            path = Path(linked)
            if path.is_file() and path.suffix.lower() in _TYPE_BY_SUFFIX:
                attachments.append(_attachment_json(task_id, path, _media_type(path)))
        return {"success": True, "attachments": attachments}

    @router.get("/api/tasks/{task_id:path}/attachments/{attachment_id}")
    async def get_attachment(task_id: str, attachment_id: str, request: Request):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_task_scope(scope, task)
        path = _linked_attachment(task, attachment_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="attachment not found")
        return FileResponse(path, media_type=_media_type(path))

    @router.delete(
        "/api/tasks/{task_id:path}/attachments/{attachment_id}",
        response_model=TaskAttachmentDeleteResponse,
    )
    async def delete_attachment(task_id: str, attachment_id: str, request: Request):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")
        scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
        _require_task_scope(scope, task)
        path = _linked_attachment(task, attachment_id)
        attachments = await orch.db.remove_task_attachment(task_id, str(path))
        if attachments is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        upload_root = (Path(orch.config.data_dir).expanduser().resolve() / "attachments")
        try:
            path.resolve().relative_to(upload_root)
        except (OSError, ValueError):
            pass
        else:
            path.unlink(missing_ok=True)
        return {"success": True, "removed": attachment_id}

    return router


router = build_task_attachments_router()
