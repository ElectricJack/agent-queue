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
        assert scope == RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")
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
            token_hash=real_h,
            session_id="s2",
            task_id=None,
            project_id=None,
            created_at=time.time() - 7200,
            expires_at=time.time() - 3600,
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
            session_id="sX",
            task_id=None,
            project_id=None,
            created_at=time.time() - 7200,
            expires_at=time.time() - 3600,
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
            session_id="sX",
            task_id=None,
            project_id=None,
            created_at=time.time() - 7200,
            expires_at=time.time() - 3600,
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


# ---------------------------------------------------------------------------
# Task 6 — mint at session start + prime/handoff scope resolution
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402


class TestPrimeScopeResolution:
    async def test_prime_resolves_task_id_from_scope(self, tmp_path):
        from src.commands.handler import CommandHandler as _CH
        from src.models import Project, Task, TaskStatus

        db = Database(str(tmp_path / "prime.db"))
        await db.initialize()
        await db.create_project(Project(id="p1", name="P1"))
        await db.create_task(
            Task(
                id="t1",
                project_id="p1",
                title="do it",
                status=TaskStatus.DEFINED,
                description="",
            )
        )

        orch = MagicMock()
        orch.db = db
        orch.plugin_registry = None
        config = MagicMock()
        config.messages = MagicMock(enabled=False)
        config.playbooks = MagicMock(enabled=True)
        config.memory = MagicMock(enabled=True)
        ch = _CH(orch, config)

        # Patch PrimeRenderer to avoid hitting the vault.
        from src.prime import PrimeRenderer as _PR

        class _Doc:
            sections = ()
            source = "default"

            def to_markdown(self):
                return f"# task {self._tid}"

            def tokens_est(self):
                return 3

        async def _render_for_task(self, task_id, **_kw):
            d = _Doc()
            d._tid = task_id
            return d

        orig = _PR.render_for_task
        _PR.render_for_task = _render_for_task  # type: ignore[assignment]
        try:
            result = await ch.execute(
                "prime",
                {
                    "_scope": {
                        "kind": "session",
                        "session_id": "s1",
                        "task_id": "t1",
                        "project_id": "p1",
                    }
                },
            )
        finally:
            _PR.render_for_task = orig
        assert result.get("success") is True, result
        assert "task t1" in result["body"]
        await db.close()

    async def test_prime_without_scope_or_arg_still_errors(self, tmp_path):
        from src.commands.handler import CommandHandler as _CH

        db = Database(str(tmp_path / "prime2.db"))
        await db.initialize()
        orch = MagicMock()
        orch.db = db
        orch.plugin_registry = None
        config = MagicMock()
        config.messages = MagicMock(enabled=False)
        config.playbooks = MagicMock(enabled=True)
        config.memory = MagicMock(enabled=True)
        ch = _CH(orch, config)

        result = await ch.execute("prime", {})
        assert "error" in result and "no task in scope" in result["error"]
        await db.close()

    async def test_task_close_revokes_session_token(self, tmp_path):
        """After _cmd_task_close succeeds, the session's token is revoked."""
        from src.commands.handler import CommandHandler as _CH
        from src.models import Project, SessionRecord, Task, TaskStatus

        db = Database(str(tmp_path / "close.db"))
        await db.initialize()
        await db.create_project(Project(id="p1", name="P1"))
        await db.create_task(
            Task(
                id="t1",
                project_id="p1",
                title="do it",
                status=TaskStatus.IN_PROGRESS,
                description="",
            )
        )
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                project_id="p1",
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-t1",
                lifecycle="task",
                task_id="t1",
                state="running",
                work_dir=str(tmp_path),
                instance_token="tok",
                epoch="e1",
                started_at=now,
                last_activity=now,
            )
        )
        store = SessionTokenStore(db, ttl_hours=1)
        token = await store.mint(session_id="s1", task_id="t1", project_id="p1")
        assert await store.validate(token) is not None

        orch = MagicMock()
        orch.db = db
        orch.plugin_registry = None
        orch.token_store = store
        orch.complete_session_task = AsyncMock(return_value={})
        config = MagicMock()
        config.messages = MagicMock(enabled=False)
        config.playbooks = MagicMock(enabled=True)
        config.memory = MagicMock(enabled=True)
        ch = _CH(orch, config)

        result = await ch.execute(
            "task_close",
            {"task_id": "t1", "session_id": "s1", "outcome": "pass"},
        )
        assert result.get("success") is True, result
        assert await store.validate(token) is None  # revoked at close
        await db.close()


# ---------------------------------------------------------------------------
# C1 — typed codegen routes enforce scope (parity with /api/execute)
# ---------------------------------------------------------------------------


from pydantic import BaseModel as _CodegenBaseModel  # noqa: E402


class _EchoRequest(_CodegenBaseModel):
    task_id: str | None = None
    project_id: str | None = None


async def _seed_codegen_app(tmp_path, cmd_name: str, *, require: bool = False):
    """Build an app with a single codegen-generated typed route for ``cmd_name``."""
    from src.api.codegen import _make_route_handler

    db = Database(str(tmp_path / f"{cmd_name}.db"))
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

    ch._recorded: list[tuple[str, dict]] = []

    async def _stub(args):
        ch._recorded.append((cmd_name, dict(args)))
        return {"success": True, "echo": args}

    setattr(ch, f"_cmd_{cmd_name}", _stub)

    store = SessionTokenStore(db, ttl_hours=72)
    deps._orchestrator = orch
    deps._command_handler = ch
    deps._token_store = store
    deps._require_session_token = require

    handler = _make_route_handler(cmd_name, _EchoRequest)
    app = FastAPI()
    app.add_api_route(f"/api/task/{cmd_name}", handler, methods=["POST"])
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TokenAuthMiddleware)
    return db, store, ch, app


class TestCodegenRouteScopeEnforcement:
    async def test_session_token_blocked_on_out_of_scope_typed_route(self, tmp_path):
        """A session-scoped token cannot delete a task via the typed route."""
        db, store, ch, app = await _seed_codegen_app(tmp_path, "delete_task")
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/task/delete_task",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"task_id": "t1"},
                )
            assert r.status_code == 403, r.text
            body = r.json()
            assert "out of scope" in body["error"]
            assert "delete_task" in body["error"]
            # Handler must not have been reached.
            assert ch._recorded == []
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_local_scope_succeeds_on_typed_route(self, tmp_path):
        """No-token / LOCAL_SCOPE requests still succeed on typed routes."""
        db, store, ch, app = await _seed_codegen_app(tmp_path, "delete_task")
        try:
            with TestClient(app) as c:
                r = c.post("/api/task/delete_task", json={"task_id": "t1"})
            assert r.status_code == 200, r.text
            assert r.json()["success"] is True
            assert ch._recorded and ch._recorded[-1][0] == "delete_task"
            # _scope injected on LOCAL requests too, but stripped by handler.
            _, recorded_args = ch._recorded[-1]
            assert "_scope" not in recorded_args
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_session_token_allowed_on_in_scope_typed_route(self, tmp_path):
        """A session token calling an in-allowlist command within its scope succeeds."""
        db, store, ch, app = await _seed_codegen_app(tmp_path, "task_show")
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/task/task_show",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"task_id": "t1"},
                )
            assert r.status_code == 200, r.text
            assert r.json()["success"] is True
            assert ch._recorded and ch._recorded[-1][0] == "task_show"
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()

    async def test_session_token_task_id_mismatch_on_typed_route_403(self, tmp_path):
        """Session token cannot spoof a different task_id via the typed route."""
        db, store, ch, app = await _seed_codegen_app(tmp_path, "task_show")
        try:
            tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
            with TestClient(app) as c:
                r = c.post(
                    "/api/task/task_show",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"task_id": "OTHER"},
                )
            assert r.status_code == 403, r.text
            assert "task_id mismatch" in r.json()["error"]
            assert ch._recorded == []
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None
            deps._require_session_token = False
            await db.close()
