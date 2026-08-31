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


@pytest.mark.asyncio
async def test_sse_tail_resolves_path_after_connect(tmp_path, db):
    """Connect before JSONL exists; create it during tail; entries arrive.

    Regression: an earlier revision resolved the transcript path once at
    connect and, if ``None``, spun the tail loop forever emitting nothing.
    """
    work_dir = "/w/late"
    slug = _slug(work_dir)
    proj_dir = tmp_path / ".claude" / "projects" / slug
    # Deliberately do NOT create the transcript directory yet.
    await _make_session(db, session_id="s6", task_id="t6",
                         work_dir=work_dir, session_key="sklate")

    from src.api.sessions import build_sessions_router
    app = FastAPI()
    app.include_router(build_sessions_router(
        db=db, base_dir=tmp_path, poll_interval=0.05, heartbeat_interval=60.0
    ))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:

        async def create_later():
            await asyncio.sleep(0.25)
            proj_dir.mkdir(parents=True, exist_ok=True)
            path = proj_dir / "sklate.jsonl"
            with path.open("w") as f:
                f.write(json.dumps({
                    "type": "assistant", "uuid": "late1", "parentUuid": None,
                    "timestamp": _now_iso(),
                    "message": {"role": "assistant", "model": "m",
                                 "content": [{"type": "text", "text": "arrived"}],
                                 "usage": {"input_tokens": 1, "output_tokens": 1}},
                }) + "\n")

        creator = asyncio.create_task(create_later())
        try:
            async with client.stream(
                "GET", "/api/sessions/s6/stream", params={"max_seconds": "1.5"}
            ) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                text = body.decode()
        finally:
            await creator

    assert "late1" in text, (
        "SSE tail must re-resolve the transcript path when the file "
        "appears after connect"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_historical_attempt_stream_uses_snapshot_and_time_window(tmp_path, legacy):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    current = SimpleNamespace(id="pooled", name="worker", task_id="new-task",
        project_id="p1", harness="claude", provider="fake", instance_token="t",
        work_dir="/reused", session_key="new-key", started_at=100, state="running")
    attempt = dict(id="attempt-old", session_id="pooled", task_id="old-task",
        project_id="p1", harness="claude", provider="fake", work_dir="/original",
        session_key="old-key", started_at=110, session_started_at=100,
        ended_at=None if legacy else 120, transcript_end_at=120 if legacy else None,
        state="stopped")
    database = SimpleNamespace(get_session=AsyncMock(return_value=current),
        get_task_session_attempt=AsyncMock(return_value=attempt))
    for directory, key, entries in [
        ("/original", "old-key", [(109, "before"), (115, "old-work"), (121, "after")]),
        ("/reused", "new-key", [(115, "other-task-secret")]),
    ]:
        _write_transcript(tmp_path, directory, key, [
            {"type":"assistant", "uuid":text, "timestamp":stamp,
             "message":{"content":text}} for stamp,text in entries])
    app = _make_app(database, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/pooled/stream",
            params={"attempt_id":"attempt-old", "replay_only":1})
    assert response.status_code == 200
    assert "old-work" in response.text
    assert "other-task-secret" not in response.text
    assert '"before"' not in response.text and '"after"' not in response.text
    assert "event: complete" in response.text
    from src.commands.handler import CommandHandler
    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = SimpleNamespace(db=database, transcript_base_dir=tmp_path)
    result = await handler._cmd_session_logs({"session_id":"pooled", "attempt_id":"attempt-old"})
    assert [entry["text"] for entry in result["entries"]] == ["old-work"]


@pytest.mark.asyncio
async def test_attempt_for_other_session_is_rejected(tmp_path, db):
    from unittest.mock import AsyncMock
    await _make_session(db, session_id="s-cross", task_id="t-cross",
        work_dir="/missing", session_key="key")
    db.get_task_session_attempt = AsyncMock(return_value={
        "id":"wrong","session_id":"somebody-else"})
    app = _make_app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/s-cross/stream",
            params={"attempt_id":"wrong", "replay_only":1})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stopped_session_missing_recording_never_peeks_new_process(tmp_path, db, monkeypatch):
    from unittest.mock import AsyncMock
    await _make_session(db, session_id="s-old", task_id="t-old",
        work_dir="/reused", session_key="gone")
    await db.update_session("s-old", state="stopped")
    peek = AsyncMock(return_value="new-process-secret")
    monkeypatch.setattr("src.api.sessions._best_effort_peek", peek)
    app = _make_app(db, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/s-old/stream", params={"replay_only":1})
    assert "event: unavailable" in response.text
    assert "new-process-secret" not in response.text
    peek.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_stream_denies_other_project_scope(tmp_path, db):
    from src.api.auth import RequestScope
    await _make_session(db, session_id="private", task_id="private-task",
        work_dir="/private", session_key="secret-key")
    _write_transcript(tmp_path, "/private", "secret-key", [
        {"type":"assistant","uuid":"secret","timestamp":time.time(),
         "message":{"content":"private recording"}}])
    app = _make_app(db, tmp_path)
    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(kind="session", project_id="other",
            session_id="supervisor-other", elevated=True)
        return await call_next(request)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/private/stream", params={"replay_only":1})
    assert response.status_code == 403
    assert "private recording" not in response.text


@pytest.mark.asyncio
async def test_stopped_cli_logs_do_not_peek(tmp_path, db):
    from unittest.mock import AsyncMock, MagicMock
    from src.commands.handler import CommandHandler
    await _make_session(db, session_id="cli-old", task_id="cli-task",
        work_dir="/reused", session_key="gone")
    await db.update_session("cli-old", state="stopped")
    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = MagicMock(db=db, transcript_base_dir=tmp_path)
    handler._active_project_id = None
    handler._cmd_session_peek = AsyncMock(return_value={"output":"unrelated"})
    response = await handler._cmd_session_logs({"session_id":"cli-old"})
    assert response["source"] == "unavailable"
    handler._cmd_session_peek.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_unknown_end_never_replays_unbounded_recording(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    current = SimpleNamespace(id="legacy", name="old", task_id="old-task",
        project_id="p1", harness="claude", provider="fake", instance_token="t",
        work_dir="/old", session_key="known", started_at=100, state="stopped")
    attempt = dict(id="legacy-attempt", session_id="legacy", task_id="old-task",
        project_id="p1", harness="claude", provider="fake", work_dir="/old",
        session_key="known", started_at=100, session_started_at=100,
        ended_at=None, transcript_end_at=None, state="stopped")
    database = SimpleNamespace(get_session=AsyncMock(return_value=current),
        get_task_session_attempt=AsyncMock(return_value=attempt))
    _write_transcript(tmp_path, "/old", "known", [
        {"type":"assistant","uuid":"other-task","timestamp":300,
         "message":{"content":"possibly later resumed work"}}])
    app = _make_app(database, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        response = await client.get("/api/sessions/legacy/stream",
            params={"attempt_id":"legacy-attempt","replay_only":1})
    assert "event: unavailable" in response.text
    assert "possibly later resumed work" not in response.text


@pytest.mark.asyncio
async def test_project_supervisor_cannot_read_projectless_session(tmp_path, db):
    from src.api.auth import RequestScope
    from src.commands.handler import CommandHandler
    from types import SimpleNamespace
    await _make_session(db, session_id="global-s", task_id="global-t",
        work_dir="/global", session_key="key")
    await db.update_session("global-s", project_id=None)
    _write_transcript(tmp_path, "/global", "key", [
        {"type":"assistant","uuid":"global-secret","timestamp":time.time(),
         "message":{"content":"global supervisor recording"}}])
    app = _make_app(db, tmp_path)
    scope = dict(kind="session", project_id="p1", session_id="project-supervisor", elevated=True)
    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(**scope)
        return await call_next(request)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/global-s/stream", params={"replay_only":1})
    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = SimpleNamespace(db=db, transcript_base_dir=tmp_path)
    handler._current_scope = scope
    try:
        logs = await handler._cmd_session_logs({"session_id":"global-s"})
    finally:
        handler._current_scope = None
    assert response.status_code == 403
    assert "error" in logs
    assert "global supervisor recording" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_during_read", [False, True])
async def test_live_stream_final_read_uses_refreshed_end_boundary(tmp_path, monkeypatch, stop_during_read):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.sessions.transcripts.base import TranscriptEntry
    current = SessionRecord(id="tail",project_id="p1",task_id="task",profile_id="p",
        harness="claude",provider="fake",name="tail",lifecycle="task",work_dir="/w",
        epoch="e",instance_token="t",started_at=100,state="running",session_key="key")
    stopped = replace(current,state="stopped",ended_at=120)
    db = SimpleNamespace(get_session=AsyncMock(side_effect=[
        current, current, current if stop_during_read else stopped, stopped]))
    entries = [TranscriptEntry(str(stamp),None,"assistant",text,None,None,stamp)
        for stamp,text in [(115,"original-output"),(125,"retry-secret")]]
    reader = SimpleNamespace(resolve_session=lambda row: tmp_path/"fixture",
        read_new=AsyncMock(side_effect=[([],0),(entries,100)]))
    monkeypatch.setattr("src.api.sessions.resolve_reader",lambda *a,**kw:reader)
    app = _make_app(db,tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        response = await client.get("/api/sessions/tail/stream",params={"max_seconds":1})
    assert "original-output" in response.text
    assert "retry-secret" not in response.text
    assert "event: complete" in response.text


@pytest.mark.asyncio
async def test_initial_replay_rechecks_end_after_file_read(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.sessions.transcripts.base import TranscriptEntry
    current = SessionRecord(id="race",project_id="p1",task_id="task",profile_id="p",
        harness="claude",provider="fake",name="race",lifecycle="task",work_dir="/w",
        epoch="e",instance_token="t",started_at=100,state="running",session_key="key")
    db = SimpleNamespace(get_session=AsyncMock(side_effect=[
        current, replace(current,state="stopped",ended_at=120)]))
    entries = [TranscriptEntry(str(stamp),None,"assistant",text,None,None,stamp)
        for stamp,text in [(115,"initial-own"),(125,"initial-retry-secret")]]
    reader = SimpleNamespace(resolve_session=lambda row: tmp_path/"fixture",
        read_new=AsyncMock(return_value=(entries,100)))
    monkeypatch.setattr("src.api.sessions.resolve_reader",lambda *a,**kw:reader)
    app = _make_app(db,tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        response = await client.get("/api/sessions/race/stream",params={"replay_only":1})
    assert "initial-own" in response.text
    assert "initial-retry-secret" not in response.text


@pytest.mark.asyncio
async def test_attempt_cli_read_refreshes_boundary_after_io(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.commands.handler import CommandHandler
    from src.sessions.transcripts.base import TranscriptEntry
    current = SimpleNamespace(id="cli-race", task_id="task", project_id="p1",
        harness="claude", work_dir="/w", session_key="key", state="running")
    attempt = dict(id="attempt",session_id="cli-race",task_id="task",project_id="p1",
        harness="claude",work_dir="/w",session_key="key",state="running",
        started_at=110,session_started_at=100,ended_at=None,transcript_end_at=None)
    db = SimpleNamespace(get_session=AsyncMock(return_value=current),
        get_task_session_attempt=AsyncMock(side_effect=[
            attempt,{**attempt,"state":"stopped","ended_at":120}]))
    entries = [TranscriptEntry(str(stamp),None,"assistant",text,None,None,stamp)
        for stamp,text in [(115,"own-cli-output"),(125,"other-cli-output")]]
    reader = SimpleNamespace(resolve_session=lambda row: tmp_path/"fixture",
        read_new=AsyncMock(return_value=(entries,100)))
    monkeypatch.setattr("src.sessions.transcripts.resolve_reader",lambda *a,**kw:reader)
    handler = CommandHandler.__new__(CommandHandler)
    handler.orchestrator = SimpleNamespace(db=db,transcript_base_dir=tmp_path)
    result = await handler._cmd_session_logs({"session_id":"cli-race","attempt_id":"attempt"})
    assert [entry["text"] for entry in result["entries"]] == ["own-cli-output"]
