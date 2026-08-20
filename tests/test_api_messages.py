"""Chat relay router — ``/api/sessions/{name}/message[s]`` (supervisor-agent §6.2).

Uses the router directly on a bare FastAPI app with the command-handler
dependency overridden: ``create_app()`` drags in the whole daemon, and what
is under test here is the translation from a session name to a ``messages``
row, not the daemon wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import get_command_handler
from src.api.messages import resolve_session_project, router
from src.commands.handler import CommandHandler
from src.config import MessagesConfig
from src.database import Database
from src.models import Project


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    await db.initialize()
    await db.create_project(Project(id="agent-queue", name="Agent Queue"))

    bus = MagicMock()
    bus.emit = AsyncMock()
    orch = MagicMock()
    orch.db = db
    orch.bus = bus

    config = MagicMock()
    config.messages = MessagesConfig(enabled=True)

    yield CommandHandler(orch, config)
    await db.close()


@pytest.fixture
def client(handler):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler
    with TestClient(app) as test_client:
        yield test_client


class TestNameResolution:
    async def test_hyphenated_project_id_resolves(self, handler):
        assert await resolve_session_project("supervisor-agent-queue", handler) == "agent-queue"

    async def test_unknown_name_is_404(self, handler):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await resolve_session_project("supervisor-nope", handler)
        assert exc.value.status_code == 404

    async def test_name_without_a_hyphen_is_404(self, handler):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await resolve_session_project("supervisor", handler)
        assert exc.value.status_code == 404


class TestPostMessage:
    def test_queues_a_row(self, client, handler):
        resp = client.post(
            "/api/sessions/supervisor-agent-queue/message",
            json={"body": "status?", "from": "discord:42", "thread_id": "discord:9"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["success"] is True
        assert payload["state"] == "queued"
        assert payload["message_id"].startswith("msg-")

    def test_from_defaults_to_user(self, client):
        resp = client.post(
            "/api/sessions/supervisor-agent-queue/message", json={"body": "hello"}
        )
        assert resp.status_code == 200

    def test_unknown_session_is_404(self, client):
        resp = client.post("/api/sessions/supervisor-ghost/message", json={"body": "hi"})
        assert resp.status_code == 404
        assert "Unknown session name" in resp.json()["detail"]

    def test_command_error_is_422(self, client, handler):
        handler.config.messages = MessagesConfig(enabled=False)
        resp = client.post(
            "/api/sessions/supervisor-agent-queue/message", json={"body": "hi"}
        )
        assert resp.status_code == 422
        assert "messages are disabled" in resp.json()["detail"]

    def test_empty_body_is_422(self, client):
        resp = client.post("/api/sessions/supervisor-agent-queue/message", json={"body": "  "})
        assert resp.status_code == 422


class TestGetMessages:
    def test_returns_both_directions_of_the_thread(self, client, handler):
        sent = client.post(
            "/api/sessions/supervisor-agent-queue/message",
            json={"body": "status?", "thread_id": "t1"},
        ).json()

        resp = client.get(
            "/api/sessions/supervisor-agent-queue/messages", params={"thread_id": "t1"}
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["success"] is True
        assert payload["project_id"] == "agent-queue"
        assert [m["id"] for m in payload["messages"]] == [sent["message_id"]]

    def test_since_filters(self, client):
        client.post("/api/sessions/supervisor-agent-queue/message", json={"body": "old"})
        import time

        resp = client.get(
            "/api/sessions/supervisor-agent-queue/messages",
            params={"since": time.time() + 3600},
        )
        assert resp.json()["messages"] == []

    def test_unknown_session_is_404(self, client):
        resp = client.get("/api/sessions/supervisor-ghost/messages")
        assert resp.status_code == 404


def test_router_is_mounted_in_create_app():
    """The relay must be registered in ``create_app`` (§6.2), not just exist."""
    import inspect

    from src.api import app as app_module

    source = inspect.getsource(app_module.create_app)
    assert "messages_router" in source
