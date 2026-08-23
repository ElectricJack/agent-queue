"""Streams API tests (spec §8.7). Mirrors tests/test_session_stream_api.py's
fixture shape."""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("bad_command", [None, 5, {}], ids=["null", "int", "object"])
@pytest.mark.asyncio
async def test_start_stream_rejects_non_list_command_shapes(db, tmp_path, bad_command):
    """Finding 1 regression: ANY non-list command shape must 400, not 422
    (Pydantic's default request-validation error for a shape it can't
    coerce). Covers null / int / object in addition to the pre-existing
    string case above."""
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": bad_command, "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 400


class _FakeFailingPipe:
    """Stdout pipe whose first ``readline`` raises, simulating a pump crash."""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail
        self._raised = False

    async def readline(self) -> bytes:
        if self._fail and not self._raised:
            self._raised = True
            raise RuntimeError("simulated pump failure")
        return b""


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process`` used to force a
    pump failure deterministically (Finding 2 regression test)."""

    def __init__(self) -> None:
        self.stdout = _FakeFailingPipe(fail=True)
        self.stderr = _FakeFailingPipe(fail=False)
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9

    def send_signal(self, sig) -> None:  # pragma: no cover - not exercised here
        pass


@pytest.mark.asyncio
async def test_pump_failure_reaches_terminal_status_and_frees_concurrency_slot(
    db, tmp_path, monkeypatch
):
    """Finding 2 regression: an exception inside ``_pump``/``asyncio.gather``
    must not leave the stream stuck "running" nor leak the session's
    concurrency slot."""
    registry = StreamRegistry(buffer_max_lines=100)

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 200
    stream_id = resp.json()["stream_id"]

    handle = registry.get(stream_id)
    for _ in range(100):
        if handle.status != "running":
            break
        await asyncio.sleep(0.01)

    assert handle.status in ("exited", "killed")
    assert registry.concurrent_count("s1") == 0


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


@pytest.mark.asyncio
async def test_metadata_returns_running_status_then_exited(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        for _ in range(50):
            resp = await client.get(f"/api/streams/{stream_id}")
            if resp.json()["status"] == "exited":
                break
            await asyncio.sleep(0.05)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "exited"
        assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_metadata_404_for_unknown_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/streams/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metadata_403_for_wrong_session_ownership(db, tmp_path):
    registry = StreamRegistry(buffer_max_lines=100)
    owner_app = _app_with_scope(db, tmp_path, LOCAL_SCOPE, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "owner-session"},
        )
        stream_id = start.json()["stream_id"]

    other_scope = RequestScope(kind="session", session_id="other-session", elevated=False)
    other_app = _app_with_scope(db, tmp_path, other_scope, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=other_app), base_url="http://test") as client:
        resp = await client.get(f"/api/streams/{stream_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_subscribe_replays_then_closes_on_exit(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "line one"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        await asyncio.sleep(0.3)

        frames = []
        async with client.stream("GET", f"/api/streams/{stream_id}/subscribe") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
                if frames and frames[-1]["type"] in ("exit", "killed"):
                    break

    types = [f["type"] for f in frames]
    assert "line" in types
    assert types[-1] == "exit"
    assert frames[-1]["rc"] == 0


@pytest.mark.asyncio
async def test_subscribe_404_for_unknown_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/streams/does-not-exist/subscribe")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_subscribe_replay_with_after_seq_skips_seen_frames(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        await asyncio.sleep(0.3)

        frames = []
        async with client.stream(
            "GET", f"/api/streams/{stream_id}/subscribe", params={"after_seq": 0}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
                if frames and frames[-1]["type"] in ("exit", "killed"):
                    break

    assert all(f["seq"] > 0 for f in frames)
