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

    @pytest.mark.parametrize("name", ["junkrole-agent-queue", "-agent-queue", "agent-queue"])
    async def test_unvalidated_role_is_404(self, name, handler):
        """The old left-to-right hyphen walk never checked the role, so any
        prefix — including none — resolved and queued a row addressed to a
        session that will never exist."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await resolve_session_project(name, handler)
        assert exc.value.status_code == 404

    async def test_global_supervisor_resolves_without_a_preexisting_project(self, handler):
        """Global chat resolves to system scope without any project record."""
        assert await resolve_session_project("supervisor-global", handler) is None

    async def test_global_supervisor_resolution_creates_nothing(self, handler):
        """Reading global chat must never create a project."""
        await resolve_session_project("supervisor-global", handler)
        assert await handler.db.get_project("global") is None

    async def test_role_boundary_beats_hyphen_position(self, handler):
        """`code-reviewer-myproj` with projects {myproj, reviewer-myproj}
        resolved to `reviewer-myproj` — the wrong project — because the walk
        took the first suffix that happened to be real."""
        from fastapi import HTTPException

        await handler.db.create_project(Project(id="myproj", name="My Proj"))
        await handler.db.create_project(Project(id="reviewer-myproj", name="Reviewer Proj"))

        with pytest.raises(HTTPException) as exc:
            await resolve_session_project("code-reviewer-myproj", handler)
        assert exc.value.status_code == 404

        assert await resolve_session_project("supervisor-myproj", handler) == "myproj"
        assert (
            await resolve_session_project("supervisor-reviewer-myproj", handler)
            == "reviewer-myproj"
        )


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
        resp = client.post("/api/sessions/supervisor-agent-queue/message", json={"body": "hello"})
        assert resp.status_code == 200

    def test_unknown_session_is_404(self, client):
        resp = client.post("/api/sessions/supervisor-ghost/message", json={"body": "hi"})
        assert resp.status_code == 404
        assert "Unknown session name" in resp.json()["detail"]

    def test_command_error_is_422(self, client, handler):
        handler.config.messages = MessagesConfig(enabled=False)
        resp = client.post("/api/sessions/supervisor-agent-queue/message", json={"body": "hi"})
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


# ---------------------------------------------------------------------------
# Scope enforcement on dedicated message routes
# ---------------------------------------------------------------------------


from src.api import dependencies as _deps  # noqa: E402
from src.api.auth import SessionTokenStore  # noqa: E402
from src.api.middleware import RequestContextMiddleware, TokenAuthMiddleware  # noqa: E402


async def _seed_messages_app(tmp_path):
    """Build a FastAPI app with the messages router + auth middleware wired up."""
    db = Database(str(tmp_path / "msg_scope.db"))
    await db.initialize()
    await db.create_project(Project(id="proj-a", name="Project A"))
    await db.create_project(Project(id="proj-b", name="Project B"))

    bus = MagicMock()
    bus.emit = AsyncMock()
    orch = MagicMock()
    orch.db = db
    orch.bus = bus
    orch._command_handler = None
    orch.plugin_registry = None

    config = MagicMock()
    config.messages = MessagesConfig(enabled=True)
    config.playbooks = MagicMock(enabled=True)
    config.memory = MagicMock(enabled=True)

    ch = CommandHandler(orch, config)

    store = SessionTokenStore(db, ttl_hours=72)
    _deps._orchestrator = orch
    _deps._command_handler = ch
    _deps._token_store = store
    _deps._require_session_token = False

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TokenAuthMiddleware)
    return db, store, ch, app


class TestMessageRouteScopeEnforcement:
    """Session-scoped tokens must not be able to send/list messages for out-of-scope projects."""

    async def test_session_token_scoped_to_project_a_blocked_on_project_b_send(self, tmp_path):
        """Token scoped to proj-a gets 403 when posting a message to a proj-b session."""
        db, store, ch, app = await _seed_messages_app(tmp_path)
        try:
            tok = await store.mint(session_id="s-a", task_id=None, project_id="proj-a")
            with TestClient(app) as c:
                r = c.post(
                    "/api/sessions/supervisor-proj-b/message",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"body": "infiltrate"},
                )
            assert r.status_code == 403, r.text
            body = r.json()
            assert "error" in body
            assert "out of scope" in body["error"]
        finally:
            _deps._orchestrator = None
            _deps._command_handler = None
            _deps._token_store = None
            _deps._require_session_token = False
            await db.close()

    async def test_session_token_scoped_to_project_a_blocked_on_project_b_list(self, tmp_path):
        """Token scoped to proj-a gets 403 when listing messages for a proj-b session."""
        db, store, ch, app = await _seed_messages_app(tmp_path)
        try:
            tok = await store.mint(session_id="s-a", task_id=None, project_id="proj-a")
            with TestClient(app) as c:
                r = c.get(
                    "/api/sessions/supervisor-proj-b/messages",
                    headers={"Authorization": f"Bearer {tok}"},
                )
            assert r.status_code == 403, r.text
            body = r.json()
            assert "error" in body
            assert "out of scope" in body["error"]
        finally:
            _deps._orchestrator = None
            _deps._command_handler = None
            _deps._token_store = None
            _deps._require_session_token = False
            await db.close()

    async def test_no_token_local_request_send_still_succeeds(self, tmp_path):
        """Unauthenticated (LOCAL_SCOPE) requests are unaffected by scope enforcement."""
        db, store, ch, app = await _seed_messages_app(tmp_path)
        try:
            with TestClient(app) as c:
                r = c.post(
                    "/api/sessions/supervisor-proj-a/message",
                    json={"body": "hello from cli"},
                )
            assert r.status_code == 200, r.text
            assert r.json()["success"] is True
        finally:
            _deps._orchestrator = None
            _deps._command_handler = None
            _deps._token_store = None
            _deps._require_session_token = False
            await db.close()

    async def test_no_token_local_request_list_still_succeeds(self, tmp_path):
        """Unauthenticated (LOCAL_SCOPE) list requests are unaffected by scope enforcement."""
        db, store, ch, app = await _seed_messages_app(tmp_path)
        try:
            with TestClient(app) as c:
                r = c.get("/api/sessions/supervisor-proj-a/messages")
            assert r.status_code == 200, r.text
            assert r.json()["success"] is True
        finally:
            _deps._orchestrator = None
            _deps._command_handler = None
            _deps._token_store = None
            _deps._require_session_token = False
            await db.close()


async def test_global_chat_is_projectless_and_does_not_include_project_messages(client, handler):
    handler.set_active_project("agent-queue")
    try:
        scoped = client.post("/api/sessions/supervisor-agent-queue/message", json={"body": "project only", "thread_id": "shared"})
        sent = client.post("/api/sessions/supervisor-global/message", json={"body": "system only", "thread_id": "shared"})
        assert scoped.status_code == sent.status_code == 200
        message = await handler.db.get_message(sent.json()["message_id"])
        assert message.project_id is None
        assert await handler.db.get_project("global") is None
        reply = await handler.execute("message_reply", {"message_id": message.id, "body": "system reply"})
        assert reply["reply"]["project_id"] is None
        response = client.get("/api/sessions/supervisor-global/messages")
        assert response.status_code == 200
        assert response.json()["project_id"] is None
        assert {m["body"] for m in response.json()["messages"]} == {"system only", "system reply"}
        project_response = client.get("/api/sessions/supervisor-agent-queue/messages")
        assert [m["body"] for m in project_response.json()["messages"]] == ["project only"]
    finally:
        handler.set_active_project(None)


@pytest.mark.parametrize("elevated,project_id", [(False, "proj-a"), (True, "proj-a"), (False, None)])
async def test_scoped_or_unprivileged_tokens_cannot_access_global_chat(tmp_path, elevated, project_id):
    db, store, ch, app = await _seed_messages_app(tmp_path)
    try:
        token = await store.mint(session_id="scoped", task_id=None, project_id=project_id, elevated=elevated)
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            assert client.get("/api/sessions/supervisor-global/messages", headers=headers).status_code == 403
            assert client.post("/api/sessions/supervisor-global/message", headers=headers, json={"body": "denied"}).status_code == 403
        assert await db.list_messages() == []
        assert await db.get_project("global") is None
    finally:
        _deps._orchestrator = None
        _deps._command_handler = None
        _deps._token_store = None
        _deps._require_session_token = False
        await db.close()


@pytest.mark.parametrize("elevated,project_id", [(False, "agent-queue"), (True, "agent-queue"), (False, None)])
async def test_system_message_commands_require_global_authority(handler, elevated, project_id):
    message = await handler.db.create_message(project_id=None, from_kind="user", from_id="user", to_kind="session", to_id="supervisor-global", body="private")
    scope = {"kind": "session", "session_id": "scoped", "elevated": elevated, "project_id": project_id}
    calls = [
        ("message_list", {"system_only": True}),
        ("message_inbox", {"to_kind": "session", "to_id": "supervisor-global", "inject": True}),
        ("message_reply", {"message_id": message.id, "body": "denied"}),
        ("message_send", {"to_kind": "session", "to_id": "supervisor-global", "from_id": "user", "body": "denied"}),
    ]
    for command, args in calls:
        result = await handler.execute(command, {**args, "_scope": scope})
        assert "out of scope" in result["error"]
    persisted = await handler.db.get_message(message.id)
    assert persisted.read_at is None
    assert persisted.delivered_at is None
    assert len(await handler.db.list_messages()) == 1


async def test_global_admin_can_send_without_project_and_read_system_chat(tmp_path):
    from httpx import ASGITransport, AsyncClient
    db, store, ch, app = await _seed_messages_app(tmp_path)
    try:
        token = await store.mint(session_id="global-admin", task_id=None, project_id=None, elevated=True)
        transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
            sent = await client.post("/api/sessions/supervisor-global/message", json={"body": "hello"})
            assert sent.status_code == 200
            response = await client.get("/api/sessions/supervisor-global/messages")
            assert response.status_code == 200
            assert response.json()["project_id"] is None
            assert [m["body"] for m in response.json()["messages"]] == ["hello"]
        direct = await ch.execute("message_send", {"from_kind": "session", "from_id": "global-admin", "to_kind": "user", "to_id": "user", "body": "update", "_scope": {"kind": "session", "session_id": "global-admin", "elevated": True, "project_id": None}})
        assert direct["message"]["project_id"] is None
        assert await db.get_project("global") is None
    finally:
        _deps._orchestrator = None
        _deps._command_handler = None
        _deps._token_store = None
        _deps._require_session_token = False
        await db.close()


@pytest.mark.parametrize("inject", [False, True])
async def test_scoped_inbox_cannot_read_system_replies_to_user(handler, inject):
    message = await handler.db.create_message(
        project_id=None, from_kind="session", from_id="supervisor-global",
        to_kind="user", to_id="user", body="private response",
    )
    result = await handler.execute("message_inbox", {
        "to_kind": "user", "to_id": "user", "inject": inject,
        "_scope": {"kind": "session", "session_id": "scoped",
                   "project_id": "agent-queue", "elevated": False},
    })
    assert "out of scope" in result["error"]
    persisted = await handler.db.get_message(message.id)
    assert persisted.delivered_at is None
    assert persisted.read_at is None


async def test_global_supervisor_can_send_to_a_real_project(handler):
    sent = await handler.execute("message_send", {
        "project_id": "agent-queue", "from_kind": "session",
        "from_id": "supervisor-global", "to_kind": "session",
        "to_id": "supervisor-agent-queue", "body": "project instruction",
        "_scope": {"kind": "session", "session_id": "global-admin",
                   "project_id": None, "elevated": True},
    })
    assert sent["message"]["project_id"] == "agent-queue"
    reply = await handler.execute("message_reply", {
        "message_id": sent["message_id"], "body": "project response",
        "_scope": {"kind": "session", "session_id": "project-admin",
                   "project_id": "agent-queue", "elevated": True},
    })
    assert reply["reply"]["project_id"] == "agent-queue"
