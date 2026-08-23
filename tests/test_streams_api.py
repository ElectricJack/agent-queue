"""Streams API tests (spec §8.7). Mirrors tests/test_session_stream_api.py's
fixture shape."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.streams import StreamRegistry, build_streams_router
from src.database import Database
from src.models import Project


class _FakeStreamsConfig:
    buffer_max_lines = 100
    buffer_max_bytes = 1024
    retention_seconds = 300
    kill_grace_seconds = 3.0
    max_concurrent_per_session = 3


class _FakeAppConfig:
    def __init__(self):
        self.streams = _FakeStreamsConfig()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="demo", name="Demo"))
    yield database
    await database.close()


def _app_with_scope(db, workspace_dir, scope: RequestScope, registry=None) -> FastAPI:
    app = FastAPI()
    router = build_streams_router(
        db=db, config=_FakeAppConfig(), workspace_dir=str(workspace_dir), registry=registry,
    )
    app.include_router(router)

    @app.middleware("http")
    async def _inject_scope(request: Request, call_next):
        request.state.scope = scope
        return await call_next(request)

    return app


LOCAL_SCOPE = RequestScope(kind="local")


@pytest.mark.asyncio
async def test_start_stream_returns_stream_id(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "stream_id" in data
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_start_stream_rejects_non_list_command(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": "echo hi", "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_stream_rejects_non_local_non_elevated_scope(db, tmp_path):
    scope = RequestScope(kind="session", session_id="s1", elevated=False)
    app = _app_with_scope(db, tmp_path, scope)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_start_stream_rejects_cwd_outside_workspace(db, tmp_path):
    outside = tmp_path.parent / "not-a-workspace"
    outside.mkdir(exist_ok=True)
    app = _app_with_scope(db, tmp_path / "workspace", outside, LOCAL_SCOPE) \
        if False else _app_with_scope(db, tmp_path / "workspace", LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(outside), "session_id": "s1"},
        )
    assert resp.status_code == 403
