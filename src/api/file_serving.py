"""Shared, path-safe, size-capped, binary-aware file-read helper.

Used by both ``/api/tasks/{id}/file`` (src/api/task_files.py) and
``/api/workspaces/{id}/file`` (src/api/workspace_files.py) — both resolve
to "a workspace root + a relative path" by the time they call this.

Behavior-preserving extraction of ``task_files.py``'s original
``get_file`` body — see that module's history for the pre-extraction
version.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

__all__ = ["serve_workspace_relative_file", "MAX_FILE_BYTES"]

MAX_FILE_BYTES = 512 * 1024  # 512 KB


async def serve_workspace_relative_file(
    workspace_path: str, path: str
) -> PlainTextResponse | JSONResponse:
    """Path-safe, size-capped, binary-aware file read.

    Raises ``HTTPException`` for every rejection case:
    - 403: absolute path, ``..`` traversal, or symlink escape.
    - 404: target missing, not a regular file, or unreadable.
    - 413: target exceeds ``MAX_FILE_BYTES``.
    """
    # ── Path safety ────────────────────────────────────────────────
    # Reject absolute paths outright — an absolute ``path`` would cause
    # ``root / path`` to discard ``root`` and jump anywhere.
    if Path(path).is_absolute():
        raise HTTPException(status_code=403, detail="absolute path not allowed")

    # Resolve BOTH sides, then verify containment. We must resolve before
    # comparing so that symlink escapes and ``..`` segments both collapse
    # to their real target. ``strict=True`` on the file path turns a
    # missing file into a FileNotFoundError we can map to 404.
    root = Path(workspace_path).resolve()

    # First pass: non-strict resolve of the *lexical* path so ``..``
    # segments collapse without touching the filesystem. This catches
    # traversal even when the target doesn't exist, so a missing
    # ``../secret`` is a 403 (escape attempt) not a 404.
    lexical = (root / path).resolve()
    try:
        lexical.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    # Second pass: strict resolve to follow symlinks and error on missing
    # files. A symlink whose real target lies outside the workspace is a
    # 403.
    try:
        candidate = (root / path).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except (OSError, RuntimeError):
        # RuntimeError: symlink loop. OSError: permission etc.
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

    # Binary heuristic: any NUL byte in the first 8 KiB → treat as binary.
    if b"\0" in data[:8192]:
        try:
            relative = str(candidate.relative_to(root))
        except ValueError:
            relative = path
        return JSONResponse(
            content={
                "success": True,
                "reason": "binary",
                "size": len(data),
                "path": relative,
            }
        )

    text = data.decode("utf-8", errors="replace")
    return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")
