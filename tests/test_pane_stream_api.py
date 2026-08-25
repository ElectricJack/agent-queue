"""SSE pane stream endpoint (live capture-pane screens)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.database import Database
from src.models import Project, SessionRecord, Task
from src.sessions.fake import FakeProvider
from src.sessions.pane_broadcaster import PaneBroadcaster
from src.sessions.provider import SessionSpec


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


async def _make_session(db, *, session_id, name, provider="fake"):
    await db.create_task(
        Task(id=f"t-{session_id}", project_id="p1", title="T", description="d")
    )
    row = SessionRecord(
        id=session_id,
        project_id="p1",
        profile_id="claude-agent",
        harness="claude",
        provider=provider,
        name=name,
        lifecycle="task",
        work_dir="/w",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        task_id=f"t-{session_id}",
        state="running",
        session_key="sk",
    )
    await db.create_session(row)
    return row


class OneProvider:
    def __init__(self, provider):
        self.provider = provider

    def create(self, name, config=None):
        return self.provider


class Sessions:
    pane_stream_interval_seconds = 0.02
    pane_stream_max_sessions = 12
    pane_stream_lines = 60


class Config:
    sessions = Sessions()


async def _app(db, provider) -> tuple[FastAPI, PaneBroadcaster]:
    from src.api.pane_stream import build_pane_router

    broadcaster = PaneBroadcaster(OneProvider(provider), Config())
    app = FastAPI()
    app.include_router(build_pane_router(db=db, broadcaster=broadcaster))
    return app, broadcaster


def _frames(text: str) -> list[dict]:
    return [
        json.loads(ln[len("data:"):].strip())
        for ln in text.splitlines()
        if ln.startswith("data:")
    ]


@pytest.mark.asyncio
async def test_pane_stream_emits_screen_frame(db):
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name="s-1", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    provider.sessions["s-1"].output.append("PANE CONTENT")
    await _make_session(db, session_id="sid1", name="s-1")

    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:
        async with client.stream(
            "GET", "/api/sessions/sid1/pane", params={"max_seconds": "0.3"}
        ) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
    frames = _frames(body.decode())
    assert frames
    assert frames[0]["source"] == "pane"
    assert frames[0]["type"] == "screen"
    assert "PANE CONTENT" in frames[0]["screen"]
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_pane_stream_unknown_session_404(db):
    provider = FakeProvider(config=None)
    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions/nope/pane")
    assert resp.status_code == 404
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_pane_stream_without_peek_capability_409(db):
    from src.sessions.subprocess import SubprocessProvider
    from src.sessions.provider import Cap

    provider = SubprocessProvider(config=None)
    # Force the no-PEEK shape without depending on the class's caps.
    provider.capabilities = frozenset(c for c in provider.capabilities if c != Cap.PEEK)
    await _make_session(db, session_id="sid2", name="s-2", provider="subprocess")

    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions/sid2/pane")
    assert resp.status_code == 409
    assert "peek" in resp.json()["detail"].lower()
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_cap_refusal_is_a_200_error_frame_not_a_429(db):
    """Design §3.1: a refusal is explicit and visible, never a dead socket.

    EventSource fails permanently on a non-200 and hands JS neither the
    status nor the body, so a 429 renders as an unexplained blank tile.
    """
    provider = FakeProvider(config=None)
    for name in ("s-a", "s-b"):
        await provider.start(
            SessionSpec(session_name=name, work_dir="/w", command=("x",),
                         instance_token="tok")
        )
    await _make_session(db, session_id="sidA", name="s-a")
    await _make_session(db, session_id="sidB", name="s-b")

    app, broadcaster = await _app(db, provider)
    broadcaster.max_sessions = 1
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:
        async with client.stream(
            "GET", "/api/sessions/sidA/pane", params={"max_seconds": "0.2"}
        ) as first:
            assert first.status_code == 200
            async for _ in first.aiter_bytes():
                pass
            resp = await client.get("/api/sessions/sidB/pane")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _frames(resp.text)
    assert len(frames) == 1
    assert frames[0]["source"] == "pane"
    assert frames[0]["type"] == "error"
    assert "cap" in frames[0]["message"].lower()
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_stream_end_leaves_no_subscriber_behind(db):
    """The watch's subscriber set must actually empty when a stream ends."""
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name="s-1", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    provider.sessions["s-1"].output.append("PANE")
    await _make_session(db, session_id="sid1", name="s-1")

    app, broadcaster = await _app(db, provider)
    broadcaster.linger_seconds = 30.0  # keep the watch around to inspect it
    transport = ASGITransport(app=app)
    body = b""
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:
        async with client.stream(
            "GET", "/api/sessions/sid1/pane", params={"max_seconds": "0.2"}
        ) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_bytes():
                body += chunk

    # The stream really did attach (ASGITransport only hands the body over
    # once the generator has finished, so this is the proof available here).
    assert _frames(body.decode())[0]["type"] == "screen"
    watch = broadcaster._watches["s-1"]
    assert watch.subscribers == set()
    assert watch.stop_at is not None
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_cancelled_stream_still_detaches(db):
    """Client disconnect cancels the generator; teardown must survive it.

    ``contextlib.suppress(Exception)`` does not catch ``CancelledError``, so
    a teardown that awaits can be interrupted and strand the poll loop.
    """
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name="s-1", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    provider.sessions["s-1"].output.append("PANE")
    await _make_session(db, session_id="sid1", name="s-1")

    app, broadcaster = await _app(db, provider)
    broadcaster.linger_seconds = 30.0
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:

        async def consume():
            async with client.stream("GET", "/api/sessions/sid1/pane") as resp:
                async for _ in resp.aiter_bytes():
                    pass

        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if broadcaster._watches.get("s-1", None) is not None and \
                    broadcaster._watches["s-1"].subscribers:
                break
        watch = broadcaster._watches["s-1"]
        assert watch.subscribers  # attached
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert watch.subscribers == set()
    await broadcaster.shutdown()
