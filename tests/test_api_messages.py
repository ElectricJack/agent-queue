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
        """`supervisor-global` addresses the *global* supervisor, not a project
        literally named "global". It has to resolve before that project row
        exists: the stub row is created lazily on SessionLens's cold-start
        path, which can only be reached by a request that got past this
        resolver. Requiring the row up front deadlocked the dashboard's
        global chat at `/` behind a permanent 404."""
        assert await resolve_session_project("supervisor-global", handler) == "global"

    async def test_global_supervisor_resolution_creates_nothing(self, handler):
        """Resolution must stay side-effect free. Creating the stub here meant
        every dashboard chat *load* conjured a `global` project row, which then
        showed up as a phantom "Global" entry in the sidebar. The write path
        creates it instead."""
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
