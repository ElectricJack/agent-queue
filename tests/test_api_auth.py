"""Tests for aq-surface Phase S2 session-scoped API auth."""

from __future__ import annotations

import hashlib
import time

import pytest

from src.api.auth import LOCAL_SCOPE, RequestScope, SessionTokenStore, TOKEN_PREFIX
from src.database import Database


class TestApiSessionTokenQueries:
    async def _db(self, tmp_path):
        db = Database(str(tmp_path / "auth.db"))
        await db.initialize()
        return db

    async def test_insert_and_get_roundtrip(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="h" * 64,
            session_id="s1",
            task_id="t1",
            project_id="p1",
            created_at=now,
            expires_at=now + 3600,
        )
        row = await db.get_api_token("h" * 64)
        assert row is not None
        assert row["session_id"] == "s1"
        assert row["task_id"] == "t1"
        assert row["project_id"] == "p1"
        assert row["revoked_at"] is None
        assert row["expires_at"] == pytest.approx(now + 3600)
        await db.close()

    async def test_get_unknown_returns_none(self, tmp_path):
        db = await self._db(tmp_path)
        assert await db.get_api_token("nope") is None
        await db.close()

    async def test_revoke_marks_all_for_session(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        for i in range(3):
            await db.insert_api_token(
                token_hash=f"h{i:0>63}",
                session_id="s1",
                task_id=None,
                project_id=None,
                created_at=now,
                expires_at=now + 3600,
            )
        await db.insert_api_token(
            token_hash="other" + "0" * 59,
            session_id="s2",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        n = await db.revoke_api_tokens_for_session("s1", now=now)
        assert n == 3
        for i in range(3):
            row = await db.get_api_token(f"h{i:0>63}")
            assert row["revoked_at"] == pytest.approx(now)
        assert (await db.get_api_token("other" + "0" * 59))["revoked_at"] is None
        await db.close()

    async def test_revoke_is_idempotent(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="h" * 64,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        assert await db.revoke_api_tokens_for_session("s1", now=now) == 1
        assert await db.revoke_api_tokens_for_session("s1", now=now + 1) == 0
        await db.close()

    async def test_delete_expired_removes_only_past(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="live" + "0" * 60,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        await db.insert_api_token(
            token_hash="dead" + "0" * 60,
            session_id="s2",
            task_id=None,
            project_id=None,
            created_at=now - 7200,
            expires_at=now - 3600,
        )
        n = await db.delete_expired_api_tokens(now=now)
        assert n == 1
        assert await db.get_api_token("live" + "0" * 60) is not None
        assert await db.get_api_token("dead" + "0" * 60) is None
        await db.close()

    async def test_delete_expired_reaps_revoked_after_grace(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="r" * 64,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now - 7200,
            expires_at=now + 3600,
        )
        await db.revoke_api_tokens_for_session("s1", now=now - 3600)
        n = await db.delete_expired_api_tokens(now=now)
        assert n == 1
        await db.close()


class TestSessionTokenStore:
    async def _store(self, tmp_path, *, ttl_hours=72):
        db = Database(str(tmp_path / "store.db"))
        await db.initialize()
        return db, SessionTokenStore(db, ttl_hours=ttl_hours)

    async def test_mint_returns_prefixed_plaintext_once(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
        assert tok.startswith(TOKEN_PREFIX)
        assert len(tok) >= len(TOKEN_PREFIX) + 32
        h = hashlib.sha256(tok.encode()).hexdigest()
        row = await db.get_api_token(h)
        assert row is not None and row["session_id"] == "s1"
        await db.close()

    async def test_validate_happy_path(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
        scope = await store.validate(tok)
        assert scope == RequestScope(
            kind="session", session_id="s1", task_id="t1", project_id="p1"
        )
        await db.close()

    async def test_validate_missing_prefix_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        assert await store.validate("bearer-without-prefix") is None
        await db.close()

    async def test_validate_unknown_token_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        assert await store.validate(TOKEN_PREFIX + "z" * 43) is None
        await db.close()

    async def test_validate_revoked_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.revoke_session("s1") == 1
        assert await store.validate(tok) is None
        await db.close()

    async def test_validate_expired_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        # Insert directly with an already-expired row keyed by the hash of a
        # plaintext we control.
        pt = TOKEN_PREFIX + "x" * 43
        real_h = hashlib.sha256(pt.encode()).hexdigest()
        await db.insert_api_token(
            token_hash=real_h, session_id="s2", task_id=None, project_id=None,
            created_at=time.time() - 7200, expires_at=time.time() - 3600,
        )
        assert await store.validate(pt) is None
        await db.close()

    async def test_cache_short_circuits_second_validate(self, tmp_path, monkeypatch):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.validate(tok) is not None

        async def _boom(*_a, **_k):
            raise AssertionError("cache miss")

        monkeypatch.setattr(db, "get_api_token", _boom)
        assert await store.validate(tok) is not None
        await db.close()

    async def test_revoke_session_invalidates_cache(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.validate(tok) is not None  # populates cache
        await store.revoke_session("s1")
        assert await store.validate(tok) is None
        await db.close()

    async def test_revoke_expired_drops_and_reports(self, tmp_path):
        db, store = await self._store(tmp_path)
        await db.insert_api_token(
            token_hash="dead" + "0" * 60,
            session_id="sX", task_id=None, project_id=None,
            created_at=time.time() - 7200, expires_at=time.time() - 3600,
        )
        n = await store.revoke_expired()
        assert n == 1
        await db.close()

    def test_local_scope_singleton(self):
        assert LOCAL_SCOPE.kind == "local"
        assert LOCAL_SCOPE.session_id is None


# ---------------------------------------------------------------------------
# Task 4 — TokenAuthMiddleware + execute-scope integration
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import dependencies as deps  # noqa: E402
from src.api.execute import router as execute_router  # noqa: E402
from src.api.middleware import RequestContextMiddleware, TokenAuthMiddleware  # noqa: E402
from src.commands.handler import CommandHandler  # noqa: E402


async def _seed_app(tmp_path, *, require=False):
    db = Database(str(tmp_path / "mw.db"))
    await db.initialize()

    orch = MagicMock()
    orch.db = db
    orch._command_handler = None
    orch.plugin_registry = None
    config = MagicMock()
    config.messages = MagicMock(enabled=False)
    config.playbooks = MagicMock(enabled=True)
    config.memory = MagicMock(enabled=True)
    ch = CommandHandler(orch, config)

    # Register stubs via setattr so CommandHandler._cmd_* dispatch picks them up.
    ch._recorded: list[tuple[str, dict]] = []

    async def _stub(args):
        ch._recorded.append(("stub_admin_only", dict(args)))
        return {"success": True, "echo": args}

    async def _stub_agent(args):
        ch._recorded.append(("task_show", dict(args)))
        return {"success": True, "echo": args}

    ch._cmd_stub_admin_only = _stub
    ch._cmd_task_show = _stub_agent

    store = SessionTokenStore(db, ttl_hours=72)
    deps._orchestrator = orch
    deps._command_handler = ch
    deps._token_store = store
    deps._require_session_token = require

    app = FastAPI()
    app.include_router(execute_router)
    # LIFO application: RequestContext added FIRST -> outer; TokenAuth LAST -> inner.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TokenAuthMiddleware)
    return db, store, ch, app


class TestTokenAuthMiddleware:
    async def test_no_token_local_scope_allows_any_command(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            with TestClient(app) as c:
                r = c.post("/api/execute", json={"command": "stub_admin_only", "args": {}})
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_invalid_token_returns_401(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            with TestClient(app) as c:
                r = c.post(
                    "/api/execute",
                    headers={"Authorization": "Bearer aqs_bogusbogusbogusbogus"},
                    json={"command": "task_show", "args": {}},
                )
            assert r.status_code == 401
            assert r.json()["ok"] is False
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            await db.close()

    async def test_valid_token_session_scope_allows_agent_command(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/execute",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"command": "task_show", "args": {"task_id": "t1"}},
                )
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
            # The stub records the args it was CALLED with (post _scope strip);
            # verify the CommandHandler saw the server-injected scope by
            # checking _current_scope was set during dispatch.  Instead we
            # verify no _scope leaks into stub args:
            _, recorded_args = ch._recorded[-1]
            assert "_scope" not in recorded_args
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            await db.close()

    async def test_valid_token_out_of_scope_command_403(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/execute",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"command": "stub_admin_only", "args": {}},
                )
            assert r.status_code == 403
            body = r.json()
            assert body["ok"] is False and "stub_admin_only" in body["error"]
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            await db.close()

    async def test_valid_token_task_id_mismatch_403(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/execute",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"command": "task_show", "args": {"task_id": "OTHER"}},
                )
            assert r.status_code == 403
            assert "task_id mismatch" in r.json()["error"]
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            await db.close()

    async def test_client_supplied_scope_is_stripped(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path)
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            # Capture what CommandHandler saw as _current_scope during dispatch.
            captured: dict = {}

            async def _stub_capture(args):
                captured["scope"] = ch._current_scope
                ch._recorded.append(("task_show", dict(args)))
                return {"success": True}

            ch._cmd_task_show = _stub_capture
            with TestClient(app) as c:
                r = c.post(
                    "/api/execute",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={
                        "command": "task_show",
                        "args": {
                            "task_id": "t1",
                            "_scope": {
                                "kind": "session",
                                "session_id": "SPOOFED",
                                "task_id": "SPOOFED",
                                "project_id": "SPOOFED",
                            },
                        },
                    },
                )
            assert r.status_code == 200, r.text
            # The scope that reached the handler is the server one, not the spoof.
            assert captured["scope"]["session_id"] == "s1"
            assert captured["scope"]["task_id"] == "t1"
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            await db.close()

    async def test_require_session_token_true_rejects_absent(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path, require=True)
        try:
            with TestClient(app) as c:
                r = c.post("/api/execute", json={"command": "task_show", "args": {}})
            assert r.status_code == 401
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_require_session_token_true_exempts_health(self, tmp_path):
        db, store, ch, app = await _seed_app(tmp_path, require=True)
        try:
            with TestClient(app) as c:
                r = c.get("/api/health")
            assert r.status_code == 200
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()


# ---------------------------------------------------------------------------
# Task 5 — revoke-expired cascade + WS handshake
# ---------------------------------------------------------------------------


class TestRevokeExpiredCascade:
    async def test_revoke_expired_runs_when_flag_on(self, tmp_path):
        db = Database(str(tmp_path / "cascade.db"))
        await db.initialize()
        store = SessionTokenStore(db, ttl_hours=72)
        await db.insert_api_token(
            token_hash="d" * 64,
            session_id="sX", task_id=None, project_id=None,
            created_at=time.time() - 7200, expires_at=time.time() - 3600,
        )
        assert await store.revoke_expired() == 1
        assert await store.revoke_expired() == 0
        await db.close()


class TestWebSocketAuth:
    async def test_invalid_token_rejects_handshake(self, tmp_path):
        db = Database(str(tmp_path / "ws.db"))
        await db.initialize()
        from fastapi import FastAPI, WebSocket
        from src.api.websocket import WebSocketManager
        from src.event_bus import EventBus

        bus = EventBus()
        store = SessionTokenStore(db, ttl_hours=72)
        deps._token_store = store
        deps._require_session_token = False

        app = FastAPI()
        app.add_middleware(TokenAuthMiddleware)
        mgr = WebSocketManager(bus, db=db)
        mgr.start()

        @app.websocket("/ws/events")
        async def ws(websocket: WebSocket):
            await mgr.handle(websocket)

        try:
            with TestClient(app) as c:
                # Invalid token: connection should close before any frame.
                with pytest.raises(Exception):
                    with c.websocket_connect(
                        "/ws/events",
                        headers={"Authorization": "Bearer aqs_bogusbogusbogusbogus"},
                    ) as w:
                        w.receive_json()
        finally:
            mgr.shutdown()
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_no_token_permits_connection_when_not_required(self, tmp_path):
        db = Database(str(tmp_path / "ws2.db"))
        await db.initialize()
        from starlette.applications import Starlette
        from starlette.routing import WebSocketRoute
        from src.api.websocket import WebSocketManager
        from src.event_bus import EventBus

        bus = EventBus()
        deps._token_store = SessionTokenStore(db, ttl_hours=72)
        deps._require_session_token = False

        mgr = WebSocketManager(bus, db=db)
        mgr.start()

        async def ws_endpoint(ws_conn):
            await mgr.handle(ws_conn)

        app = Starlette(routes=[WebSocketRoute("/ws/events", ws_endpoint)])
        app.add_middleware(TokenAuthMiddleware)

        try:
            with TestClient(app) as c:
                with c.websocket_connect("/ws/events") as w:
                    w.close()
        finally:
            mgr.shutdown()
            deps._token_store = None
            await db.close()
