"""TranscriptWatcher tests (S3, Task B2)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.database import Database
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions.transcripts.watcher import TranscriptWatcher


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def payloads(self, event_type):
        return [p for t, p in self.events if t == event_type]


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


@pytest.fixture
def bus():
    return _Bus()


def _slug(work_dir: str) -> str:
    return work_dir.replace("/", "-").replace(".", "-")


def _make_transcript(base_dir: Path, work_dir: str, session_key: str) -> Path:
    """Create a Claude-style transcript in the fake base_dir."""
    slug = _slug(work_dir)
    proj = base_dir / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_key}.jsonl"
    path.touch()
    return path


def _append(path: Path, entries: list[dict]) -> None:
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def _make_session(db, work_dir: str, session_key: str,
                         *, session_id="s1", task_id="t1") -> SessionRecord:
    if task_id:
        await db.create_task(
            Task(id=task_id, project_id="p1", title="T", description="d")
        )
    row = SessionRecord(
        id=session_id,
        project_id="p1",
        profile_id="claude-agent",
        harness="claude",
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


@pytest.mark.asyncio
async def test_new_entry_emits_task_message(tmp_path, db, bus):
    work_dir = "/work/one"
    path = _make_transcript(tmp_path, work_dir, "sk1")
    now = time.time()
    _append(path, [{
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                     time.gmtime(now)),
        "message": {
            "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": "hello world"}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
    }])
    await _make_session(db, work_dir, "sk1", session_id="s1a", task_id="t1a")
    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    await w.tick()

    msgs = bus.payloads("notify.task_message")
    assert len(msgs) == 1
    assert msgs[0]["stream_id"] == "s1a"
    assert msgs[0]["task_id"] == "t1a"
    assert "hello world" in msgs[0]["message"]


@pytest.mark.asyncio
async def test_offset_persistence_no_duplicate_emissions(tmp_path, db, bus):
    work_dir = "/work/two"
    path = _make_transcript(tmp_path, work_dir, "sk2")
    now = time.time()
    _append(path, [{
        "type": "assistant", "uuid": "a1", "parentUuid": None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now)),
        "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "one"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}},
    }])
    await _make_session(db, work_dir, "sk2", session_id="s2", task_id="t2")

    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    await w.tick()
    await w.tick()  # no new lines → no new emissions
    assert len(bus.payloads("notify.task_message")) == 1


@pytest.mark.asyncio
async def test_usage_delta_lands_one_ledger_row_per_assistant(tmp_path, db, bus):
    work_dir = "/work/three"
    path = _make_transcript(tmp_path, work_dir, "sk3")
    now = time.time()
    _append(path, [
        {"type": "assistant", "uuid": "a1", "parentUuid": None,
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                     time.gmtime(now)),
         "message": {"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "one"}],
                      "usage": {"input_tokens": 100, "output_tokens": 25}}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1",
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                     time.gmtime(now)),
         "message": {"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "two"}],
                      "usage": {"input_tokens": 50, "output_tokens": 10}}},
        # user turn with no usage → no ledger row
        {"type": "user", "uuid": "u1", "parentUuid": "a2",
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                     time.gmtime(now)),
         "message": {"role": "user", "content": "hi"}},
    ])
    await _make_session(db, work_dir, "sk3", session_id="s3", task_id="t3")
    await db.create_agent(Agent(
        id="agent-3", name="ag3", profile_id="claude-agent",
        state=AgentState.IDLE,
    ))
    await db.transition_task("t3", TaskStatus.IN_PROGRESS,
                              assigned_agent_id="agent-3")

    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    await w.tick()

    rollup = await db.get_cost_rollup(project_id="p1")
    total = sum(r["tokens_used"] for r in rollup)
    # 100+25 + 50+10 = 185
    assert total == 185


@pytest.mark.asyncio
async def test_in_turn_touches_session_activity(tmp_path, db, bus):
    work_dir = "/work/four"
    path = _make_transcript(tmp_path, work_dir, "sk4")
    now = time.time()
    _append(path, [{
        "type": "assistant", "uuid": "a1", "parentUuid": None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                     time.gmtime(now)),
        "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "busy"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}},
    }])
    row = await _make_session(db, work_dir, "sk4", session_id="s4", task_id="t4")
    assert row.last_activity is None

    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    await w.tick()

    fresh = await db.get_session(row.id)
    assert fresh.last_activity is not None
    assert fresh.last_activity > 0


@pytest.mark.asyncio
async def test_missing_transcript_emits_once_and_falls_back(tmp_path, db, bus):
    work_dir = "/nowhere/here"
    # No transcript directory created.
    await _make_session(db, work_dir, "skmiss", session_id="s5", task_id="t5")

    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    await w.tick()
    await w.tick()  # second tick must NOT re-emit

    missing = bus.payloads("session.transcript_missing")
    assert len(missing) == 1
    assert missing[0]["session_id"] == "s5"


@pytest.mark.asyncio
async def test_watcher_ignores_non_claude_harness(tmp_path, db, bus):
    await db.create_task(Task(id="tX", project_id="p1", title="T", description="d"))
    row = SessionRecord(
        id="sX",
        project_id="p1",
        profile_id="unknown-agent",
        harness="unknown-harness",
        provider="fake",
        name="s-tX",
        lifecycle="task",
        work_dir="/w",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        task_id="tX",
        state="running",
    )
    await db.create_session(row)
    w = TranscriptWatcher(db=db, bus=bus, base_dir=tmp_path)
    # Should tick cleanly, emit nothing.
    await w.tick()
    assert not bus.events
