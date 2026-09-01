"""Task screenshot upload, preview, and removal API tests."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.api import dependencies as deps
from src.api.app import create_app
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Project, Task
from src.orchestrator import Orchestrator
from src.prime.renderer import PrimeRenderer


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
async def attachment_app(tmp_path):
    db = Database(str(tmp_path / "attachments.db"))
    await db.initialize()
    await db.create_project(Project(id="proj", name="Project"))
    await db.create_task(Task(id="task/unsafe", project_id="proj", title="Screenshots", description=""))

    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "attachments.db"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus()

    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
    app = create_app(orch, config)
    try:
        yield app, db, config
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.close()


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _upload(client: AsyncClient, name: str = "screen shot.png", data: bytes = PNG_BYTES):
    return await client.post(
        "/api/tasks/task%2Funsafe/attachments",
        files={"file": (name, data, "image/png")},
    )


async def test_upload_sanitizes_filename_and_surfaces_path_to_task_and_prime(attachment_app):
    app, db, config = attachment_app
    async with _client(app) as client:
        response = await _upload(client, "../../screen shot.png")
        assert response.status_code == 201
        attachment = response.json()["attachment"]

        stored = Path(attachment["path"])
        assert stored.is_file()
        assert stored.read_bytes() == PNG_BYTES
        assert stored.is_relative_to(Path(config.data_dir) / "attachments")
        assert ".." not in stored.name
        assert " " not in stored.name
        assert attachment["url"].endswith(f"/{stored.name}")

        detail = await client.post("/api/task/get", json={"task_id": "task/unsafe"})
        assert detail.status_code == 200
        assert detail.json()["attachments"] == [str(stored)]

        shown = await client.post("/api/task/show", json={"task_id": "task/unsafe"})
        assert shown.status_code == 200
        assert shown.json()["attachments"] == [str(stored)]

    prime = await PrimeRenderer(db, config).render_for_task("task/unsafe")
    context = next(section.body for section in prime.sections if section.key == "task_context")
    assert f"**attachments:**\n- {stored}" in context
    assert stored.read_bytes() == PNG_BYTES


async def test_list_preview_and_delete_only_linked_attachment(attachment_app):
    app, db, _config = attachment_app
    async with _client(app) as client:
        uploaded = (await _upload(client)).json()["attachment"]

        listed = await client.get("/api/tasks/task%2Funsafe/attachments")
        assert listed.status_code == 200
        assert listed.json()["attachments"] == [uploaded]

        preview = await client.get(uploaded["url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("image/png")
        assert preview.content == PNG_BYTES

        traversal = await client.get("/api/tasks/task%2Funsafe/attachments/..%2Fsecrets.txt")
        assert traversal.status_code in {404, 422}

        removed = await client.delete(uploaded["url"])
        assert removed.status_code == 200
        assert removed.json() == {"success": True, "removed": uploaded["id"]}
        assert not Path(uploaded["path"]).exists()
        assert (await db.get_task("task/unsafe")).attachments == []

        assert (await client.get(uploaded["url"])).status_code == 404
        assert (await client.delete(uploaded["url"])).status_code == 404


async def test_upload_rejects_disallowed_content_type_without_writing(attachment_app):
    app, db, config = attachment_app
    async with _client(app) as client:
        response = await client.post(
            "/api/tasks/task%2Funsafe/attachments",
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )
    assert response.status_code == 415
    assert (await db.get_task("task/unsafe")).attachments == []
    assert not (Path(config.data_dir) / "attachments").exists()


async def test_upload_rejects_file_over_size_cap_without_writing(attachment_app):
    app, db, config = attachment_app
    async with _client(app) as client:
        response = await _upload(client, data=b"x" * (10 * 1024 * 1024 + 1))
    assert response.status_code == 413
    assert (await db.get_task("task/unsafe")).attachments == []
    stored = Path(config.data_dir) / "attachments"
    assert not stored.exists() or not any(stored.rglob("*.*"))


async def test_upload_rejects_spoofed_image_content_type(attachment_app):
    app, db, _config = attachment_app
    async with _client(app) as client:
        response = await _upload(client, "fake.png", b"this is not a PNG")
    assert response.status_code == 415
    assert (await db.get_task("task/unsafe")).attachments == []


async def test_concurrent_uploads_preserve_both_attachment_paths(attachment_app):
    app, db, _config = attachment_app
    async with _client(app) as client:
        first, second = await asyncio.gather(
            _upload(client, "first.png"),
            _upload(client, "second.png"),
        )
    assert first.status_code == 201
    assert second.status_code == 201
    paths = (await db.get_task("task/unsafe")).attachments
    assert len(paths) == 2
    assert len(set(paths)) == 2


async def test_attachment_routes_publish_typed_openapi_contract(attachment_app):
    app, _db, _config = attachment_app
    spec = app.openapi()
    upload = spec["paths"]["/api/tasks/{task_id}/attachments"]["post"]
    upload_schema = upload["responses"]["201"]["content"]["application/json"]["schema"]
    assert upload_schema["$ref"].endswith("/TaskAttachmentResponse")
    assert "multipart/form-data" in upload["requestBody"]["content"]

    listing = spec["paths"]["/api/tasks/{task_id}/attachments"]["get"]
    list_schema = listing["responses"]["200"]["content"]["application/json"]["schema"]
    assert list_schema["$ref"].endswith("/TaskAttachmentsResponse")

    removal = spec["paths"]["/api/tasks/{task_id}/attachments/{attachment_id}"]["delete"]
    delete_schema = removal["responses"]["200"]["content"]["application/json"]["schema"]
    assert delete_schema["$ref"].endswith("/TaskAttachmentDeleteResponse")


async def test_generated_python_client_uploads_binary_image(attachment_app):
    app, _db, _config = attachment_app
    client_root = Path(__file__).resolve().parents[1] / "packages" / "aq-client"
    if str(client_root) not in sys.path:
        sys.path.insert(0, str(client_root))

    from agent_queue_api_client import types
    from agent_queue_api_client.api.default import (
        upload_attachment_api_tasks_task_id_attachments_post as upload_api,
    )
    from agent_queue_api_client.client import Client
    from agent_queue_api_client.models.body_upload_attachment_api_tasks_task_id_attachments_post import (
        BodyUploadAttachmentApiTasksTaskIdAttachmentsPost,
    )
    from agent_queue_api_client.models.task_attachment_response import TaskAttachmentResponse

    generated = Client(base_url="http://test", raise_on_unexpected_status=False)
    async with _client(app) as http:
        generated.set_async_httpx_client(http)
        result = await upload_api.asyncio(
            "task/unsafe",
            client=generated,
            body=BodyUploadAttachmentApiTasksTaskIdAttachmentsPost(
                file=types.File(
                    payload=BytesIO(PNG_BYTES),
                    file_name="generated.png",
                    mime_type="image/png",
                )
            ),
        )

    assert isinstance(result, TaskAttachmentResponse)
    assert Path(result.attachment.path).read_bytes() == PNG_BYTES
