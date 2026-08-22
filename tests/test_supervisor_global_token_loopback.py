"""Loopback restriction on global-admin bearer tokens.

See dashboard-shell-v2 plan §Task 2.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import dependencies as deps
from src.api.auth import RequestScope
from src.api.middleware import TokenAuthMiddleware


def _app_with_scope(scope: RequestScope) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    store = MagicMock()
    store.validate = AsyncMock(return_value=scope)
    app.add_middleware(TokenAuthMiddleware)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    return app, store


@pytest.fixture
def _patch_deps(monkeypatch):
    def _apply(store):
        monkeypatch.setattr(deps, "_token_store", store)
        monkeypatch.setattr(deps, "_require_session_token", False)
    return _apply


@pytest.mark.asyncio
async def test_global_admin_from_loopback_allowed(_patch_deps):
    scope = RequestScope(
        kind="session", session_id="supervisor-global",
        project_id=None, elevated=True,
    )
    app, store = _app_with_scope(scope)
    _patch_deps(store)
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_global_admin_from_remote_rejected(_patch_deps):
    scope = RequestScope(
        kind="session", session_id="supervisor-global",
        project_id=None, elevated=True,
    )
    app, store = _app_with_scope(scope)
    _patch_deps(store)
    transport = ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 403
        assert "loopback" in r.text.lower()


@pytest.mark.asyncio
async def test_per_project_elevated_from_remote_allowed(_patch_deps):
    scope = RequestScope(
        kind="session", session_id="s1", project_id="demo",
        elevated=True,
    )
    app, store = _app_with_scope(scope)
    _patch_deps(store)
    transport = ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 200
