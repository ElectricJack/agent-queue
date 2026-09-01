"""Tests for the task attachment endpoints (screenshot upload/list/serve/delete)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import dependencies as deps
from src.api.task_attachments import (
    MAX_ATTACHMENT_BYTES,
    build_task_attachments_router,
)
from src.database import Database
from src.models import Project, Task, TaskStatus

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
async def wired(tmp_path):
    """Yields (client_factory, db, data_dir) over a real SQLite Database."""
    db = Database(str(tmp_path / "aq.db"))
    await db.initialize()
    await db.create_project(Project(id="proj", name="P"))
    await db.create_task(Task(
        id="task1", project_id="proj", title="t", description="",
        status=TaskStatus.IN_PROGRESS,
    ))

    data_dir = tmp_path / "data"
    orch = MagicMock()
    orch.db = db
    orch.config = MagicMock()
    orch.config.data_dir = str(data_dir)

    app = FastAPI()
    app.include_router(build_task_attachments_router())
    prev_orch = deps._orchestrator
    deps._orchestrator = orch

    def _client() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    try:
        yield _client, db, data_dir
    finally:
        deps._orchestrator = prev_orch
        await db.close()


def _png_upload(filename: str = "shot.png", content: bytes = PNG_BYTES):
    return {"file": (filename, content, "image/png")}


# ── upload ────────────────────────────────────────────────────────────


async def test_upload_writes_file_and_links_task(wired):
    client_factory, db, data_dir = wired
    async with client_factory() as ac:
        r = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    stored = body["attachment"]["path"]
    assert stored.startswith(str(data_dir / "attachments" / "task-task1"))
    assert stored.endswith("-shot.png")
    assert Path(stored).read_bytes() == PNG_BYTES
    assert body["attachment"]["content_type"] == "image/png"
    assert body["attachment"]["size"] == len(PNG_BYTES)
    task = await db.get_task("task1")
    assert task.attachments == [stored]


async def test_upload_rejects_disallowed_content_type(wired):
    client_factory, db, _ = wired
    async with client_factory() as ac:
        r = await ac.post(
            "/api/tasks/task1/attachments",
            files={"file": ("evil.sh", b"#!/bin/sh\n", "text/x-shellscript")},
        )
    assert r.status_code == 415
    task = await db.get_task("task1")
    assert task.attachments == []


async def test_upload_rejects_svg(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.post(
            "/api/tasks/task1/attachments",
            files={"file": ("x.svg", b"<svg/>", "image/svg+xml")},
        )
    assert r.status_code == 415


async def test_upload_size_cap(wired):
    client_factory, db, data_dir = wired
    big = b"x" * (MAX_ATTACHMENT_BYTES + 1)
    async with client_factory() as ac:
        r = await ac.post("/api/tasks/task1/attachments", files=_png_upload(content=big))
    assert r.status_code == 413
    task = await db.get_task("task1")
    assert task.attachments == []
    # No partial file left behind.
    task_dir = data_dir / "attachments" / "task-task1"
    assert not task_dir.exists() or not any(task_dir.iterdir())


async def test_upload_sanitizes_traversal_filename(wired):
    client_factory, db, data_dir = wired
    async with client_factory() as ac:
        r = await ac.post(
            "/api/tasks/task1/attachments",
            files=_png_upload(filename="../../../etc/evil name!.png"),
        )
    assert r.status_code == 200
    stored = Path(r.json()["attachment"]["path"])
    # File landed inside the managed task dir, with a sanitized basename.
    stored.relative_to(data_dir / "attachments" / "task-task1")
    assert "/etc/" not in str(stored)
    assert stored.name.endswith("-evil_name_.png")
    task = await db.get_task("task1")
    assert task.attachments == [str(stored)]


async def test_upload_empty_filename_gets_default(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.post(
            "/api/tasks/task1/attachments",
            files={"file": ("...", PNG_BYTES, "image/png")},
        )
    assert r.status_code == 200
    assert r.json()["attachment"]["path"].endswith("-pasted.png")


async def test_upload_rejects_empty_file(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.post("/api/tasks/task1/attachments", files=_png_upload(content=b""))
    assert r.status_code == 400


async def test_upload_unknown_task_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.post("/api/tasks/nope/attachments", files=_png_upload())
    assert r.status_code == 404


async def test_two_uploads_same_name_do_not_collide(wired):
    client_factory, db, _ = wired
    async with client_factory() as ac:
        r1 = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
        r2 = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
    p1 = r1.json()["attachment"]["path"]
    p2 = r2.json()["attachment"]["path"]
    assert p1 != p2
    task = await db.get_task("task1")
    assert task.attachments == [p1, p2]


# ── list ──────────────────────────────────────────────────────────────


async def test_list_returns_metadata(wired):
    client_factory, db, _ = wired
    async with client_factory() as ac:
        up = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
        r = await ac.get("/api/tasks/task1/attachments")
    stored = up.json()["attachment"]["path"]
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    [meta] = body["attachments"]
    assert meta["path"] == stored
    assert meta["exists"] is True
    assert meta["size"] == len(PNG_BYTES)
    assert meta["content_type"] == "image/png"


async def test_list_marks_missing_files(wired):
    client_factory, db, _ = wired
    await db.add_task_attachment("task1", "/nonexistent/gone.png")
    async with client_factory() as ac:
        r = await ac.get("/api/tasks/task1/attachments")
    [meta] = r.json()["attachments"]
    assert meta["exists"] is False
    assert meta["size"] is None


# ── serve ─────────────────────────────────────────────────────────────


async def test_serve_returns_bytes_with_content_type(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        up = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
        stored = up.json()["attachment"]["path"]
        r = await ac.get("/api/tasks/task1/attachment", params={"path": stored})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_BYTES
    assert r.headers["content-disposition"].startswith("inline")


async def test_serve_rejects_path_not_on_task(wired, tmp_path):
    """The DB list is the allowlist — arbitrary paths are 403 even if real."""
    client_factory, _, _ = wired
    secret = tmp_path / "secret.txt"
    secret.write_text("shh")
    async with client_factory() as ac:
        r = await ac.get("/api/tasks/task1/attachment", params={"path": str(secret)})
    assert r.status_code == 403


async def test_serve_missing_file_is_404(wired):
    client_factory, db, _ = wired
    await db.add_task_attachment("task1", "/nonexistent/gone.png")
    async with client_factory() as ac:
        r = await ac.get("/api/tasks/task1/attachment", params={"path": "/nonexistent/gone.png"})
    assert r.status_code == 404


# ── delete ────────────────────────────────────────────────────────────


async def test_delete_unlinks_managed_file_and_delinks(wired):
    client_factory, db, _ = wired
    async with client_factory() as ac:
        up = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
        stored = up.json()["attachment"]["path"]
        r = await ac.delete("/api/tasks/task1/attachment", params={"path": stored})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["removed_from_disk"] is True
    assert not Path(stored).exists()
    task = await db.get_task("task1")
    assert task.attachments == []


async def test_delete_external_path_delinks_but_keeps_file(wired, tmp_path):
    """A CLI-attached file outside the managed root must never be unlinked."""
    client_factory, db, _ = wired
    external = tmp_path / "design.png"
    external.write_bytes(PNG_BYTES)
    await db.add_task_attachment("task1", str(external))
    async with client_factory() as ac:
        r = await ac.delete("/api/tasks/task1/attachment", params={"path": str(external)})
    assert r.status_code == 200
    assert r.json()["removed_from_disk"] is False
    assert external.exists()
    task = await db.get_task("task1")
    assert task.attachments == []


async def test_delete_unknown_attachment_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.delete("/api/tasks/task1/attachment", params={"path": "/nope.png"})
    assert r.status_code == 404


# ── agent visibility (prime section 4) ────────────────────────────────


async def test_uploaded_attachment_renders_in_prime_task_context(wired, tmp_path):
    """End-to-end: dashboard upload → `aq prime` lists the path in section 4."""
    from src.config import AppConfig
    from src.prime.renderer import PrimeRenderer

    client_factory, db, _ = wired
    async with client_factory() as ac:
        up = await ac.post("/api/tasks/task1/attachments", files=_png_upload())
    stored = up.json()["attachment"]["path"]

    config = AppConfig(data_dir=str(tmp_path / "primedata"))
    doc = await PrimeRenderer(db, config).render_for_task("task1")
    body = {s.key: s.body for s in doc.sections}["task_context"]
    assert "**attachments:**" in body
    assert stored in body


# ── DB atomic helpers ─────────────────────────────────────────────────


async def test_add_attachment_is_idempotent(wired):
    _, db, _ = wired
    await db.add_task_attachment("task1", "/a.png")
    result = await db.add_task_attachment("task1", "/a.png")
    assert result == ["/a.png"]


async def test_add_attachment_unknown_task_raises(wired):
    _, db, _ = wired
    with pytest.raises(ValueError, match="not found"):
        await db.add_task_attachment("nope", "/a.png")


async def test_remove_attachment_unknown_path_raises(wired):
    _, db, _ = wired
    with pytest.raises(ValueError, match="not on task"):
        await db.remove_task_attachment("task1", "/nope.png")
