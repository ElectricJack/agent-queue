"""SSE session stream and transcript-backed session_logs tests (S3, B3)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.database import Database
from src.models import Project, SessionRecord, Task


def _slug(work_dir: str) -> str:
    return work_dir.replace("/", "-").replace(".", "-")


def _write_transcript(base_dir: Path, work_dir: str, session_key: str,
                       entries: list[dict]) -> Path:
    slug = _slug(work_dir)
    proj = base_dir / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_key}.jsonl"
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _now_iso(offset: float = 0.0) -> str:
    t = time.time() + offset
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


async def _make_session(db, *, session_id, task_id, work_dir, session_key,
                         harness="claude"):
    await db.create_task(
        Task(id=task_id, project_id="p1", title="T", description="d")
    )
    row = SessionRecord(
        id=session_id,
        project_id="p1",
        profile_id="claude-agent",
        harness=harness,
        provider="fake",
        name=f"s-{task_id}",
        lifecycle="task",
        work_dir=work_dir,
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        task_id=task_id,
        state="running",
        session_key=session_key,
    )
    await db.create_session(row)
    return row


def _make_app(db, base_dir: Path) -> FastAPI:
    from src.api.sessions import build_sessions_router

    app = FastAPI()
    app.include_router(build_sessions_router(db=db, base_dir=base_dir))
    return app


@pytest.mark.asyncio
async def test_sse_replays_all_fixture_entries(tmp_path, db):
    work_dir = "/w/one"
    _write_transcript(tmp_path, work_dir, "sk", [
        {"type": "assistant", "uuid": "a1", "parentUuid": None,
         "timestamp": _now_iso(-10),
         "message": {"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "hello"}],
                      "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "user", "uuid": "u1", "parentUuid": "a1",
         "timestamp": _now_iso(-9),
         "message": {"role": "user", "content": "ack"}},
    ])
    await _make_session(db, session_id="s1", task_id="t1",
                         work_dir=work_dir, session_key="sk")

    app = _make_app(db, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:
        async with client.stream(
            "GET", "/api/sessions/s1/stream", params={"replay_only": "1"}
        ) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
            text = body.decode()
    # Two data lines expected (assistant + user), plus source label.
    data_lines = [ln for ln in text.splitlines() if ln.startswith("data:")]
    assert len(data_lines) >= 2
    joined = "\n".join(data_lines)
    assert "a1" in joined
    assert "u1" in joined


@pytest.mark.asyncio
async def test_sse_unknown_session_returns_404(tmp_path, db):
    app = _make_app(db, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions/does-not-exist/stream")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sse_no_transcript_yields_peek_fallback_frame(tmp_path, db):
    """When no transcript resolves, the endpoint emits a peek-fallback frame."""
    # Session references a work_dir with no ~/.claude/projects entry.
    await _make_session(db, session_id="s2", task_id="t2",
                         work_dir="/nowhere/here", session_key="sk")
    app = _make_app(db, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "GET", "/api/sessions/s2/stream", params={"replay_only": "1"}
        ) as resp:
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
            text = body.decode()
    assert "peek" in text  # fallback source label


@pytest.mark.asyncio
async def test_session_logs_command_uses_transcript_when_available(tmp_path, db):
    from src.commands.handler import CommandHandler
    from unittest.mock import MagicMock

    work_dir = "/w/logs"
    _write_transcript(tmp_path, work_dir, "sklog", [
        {"type": "assistant", "uuid": "a1", "parentUuid": None,
         "timestamp": _now_iso(-1),
         "message": {"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "output line"}],
                      "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    await _make_session(db, session_id="s3", task_id="t3",
                         work_dir=work_dir, session_key="sklog")

    # Build a minimal CommandHandler pointed at our db.  session_logs
    # only reads the db and the reader.
    orch = MagicMock()
    orch.session_providers = None
    orch._command_handler = None
    orch.db = db
    orch.bus = None
    # transcript_base_dir override so we hit tmp_path, not ~/.claude.
    orch.transcript_base_dir = tmp_path

    from src.config import AppConfig
    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = orch
    handler.config = AppConfig()
    handler._active_project_id = None

    result = await handler._cmd_session_logs({"session_id": "s3"})
    assert result.get("success") is True
    assert result.get("source") == "transcript"
    # Includes the assistant text
    assert any("output line" in str(x)
                for x in result.get("entries", []) or [result.get("output", "")])


@pytest.mark.asyncio
async def test_session_logs_falls_back_to_peek_when_no_transcript(tmp_path, db):
    from unittest.mock import MagicMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig

    await _make_session(db, session_id="s4", task_id="t4",
                         work_dir="/nowhere", session_key="skn")

    # Provider that reports no PEEK capability — logs should still succeed
    # with source="peek".
    class _NoopProvider:
        name = "fake"

        def supports(self, cap):
            return False

    class _Reg:
        def create(self, name, config=None):
            return _NoopProvider()

    orch = MagicMock()
    orch.session_providers = _Reg()
    orch.db = db
    orch.bus = None
    orch.transcript_base_dir = tmp_path

    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = orch
    handler.config = AppConfig()
    handler._active_project_id = None

    result = await handler._cmd_session_logs({"session_id": "s4"})
    assert result.get("success") is True
    assert result.get("source") == "peek"


@pytest.mark.asyncio
async def test_sse_tail_delivers_appended_entries(tmp_path, db):
    """Entries appended after connect arrive on the live tail."""
    work_dir = "/w/tail"
    path = _write_transcript(tmp_path, work_dir, "sk", [
        {"type": "assistant", "uuid": "a1", "parentUuid": None,
         "timestamp": _now_iso(-5),
         "message": {"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "first"}],
                      "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    await _make_session(db, session_id="s5", task_id="t5",
                         work_dir=work_dir, session_key="sk")

    from src.api.sessions import build_sessions_router
    app = FastAPI()
    app.include_router(build_sessions_router(
        db=db, base_dir=tmp_path, poll_interval=0.1, heartbeat_interval=60.0
    ))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:

        async def append_later():
            await asyncio.sleep(0.3)
            with path.open("a") as f:
                f.write(json.dumps({
                    "type": "assistant", "uuid": "a2", "parentUuid": "a1",
                    "timestamp": _now_iso(),
                    "message": {"role": "assistant", "model": "m",
                                 "content": [{"type": "text", "text": "second"}],
                                 "usage": {"input_tokens": 1, "output_tokens": 1}},
                }) + "\n")

        appender = asyncio.create_task(append_later())
        try:
            async with client.stream(
                "GET", "/api/sessions/s5/stream", params={"max_seconds": "1.5"}
            ) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                text = body.decode()
        finally:
            await appender

    assert "a1" in text
    assert "a2" in text
