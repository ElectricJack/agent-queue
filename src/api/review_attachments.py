"""Read immutable review evidence bytes after command-layer authorization."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.api import dependencies as deps
from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import check_request_scope

router = APIRouter()


@router.get("/api/reviews/{review_id}/revisions/{revision}/attachments/{attachment_id}")
async def download_review_attachment(
    review_id: str, revision: int, attachment_id: str, request: Request,
):
    handler = deps._command_handler
    orch = deps._orchestrator
    if handler is None or orch is None:
        raise HTTPException(status_code=503, detail="orchestrator not ready")
    scope: RequestScope = getattr(request.state, "scope", LOCAL_SCOPE)
    args = {"review_id": review_id, "revision": revision}
    denied = await check_request_scope("review_attachment_list", args, scope, db=orch.db)
    if denied:
        raise HTTPException(status_code=404, detail="attachment not found")
    args["_scope"] = {
        "kind": scope.kind, "session_id": scope.session_id,
        "session_instance_token": scope.session_instance_token,
        "task_id": scope.task_id, "project_id": scope.project_id,
        "elevated": scope.elevated,
    }
    listed = await handler.execute("review_attachment_list", args)
    if not listed.get("success") or not any(
        row["id"] == attachment_id for row in listed["attachments"]
    ):
        raise HTTPException(status_code=404, detail="attachment not found")
    row = await orch.db.get_review_attachment(attachment_id)
    if row is None or row["review_id"] != review_id or row["revision"] != revision:
        raise HTTPException(status_code=404, detail="attachment not found")
    root = (Path(orch.config.data_dir).expanduser().resolve() / "review-attachments")
    path = Path(row["path"])
    try:
        path.resolve().relative_to(root)
        data = path.read_bytes()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="attachment not found") from None
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise HTTPException(status_code=409, detail="attachment integrity check failed")
    return FileResponse(path, media_type=row["content_type"], headers={"ETag": row["sha256"]})
