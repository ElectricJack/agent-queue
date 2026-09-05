"""Question invalidations are scoped and contain no worker content/secrets."""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.websocket import WebSocketManager
from src.database import Database
from src.event_bus import EventBus


QUESTION = {
    "id": "q1",
    "session_id": "s1",
    "session_name": "p-worker",
    "instance_token": "private-token",
    "task_id": "t1",
    "project_id": "p1",
    "agent_id": "a1",
    "turn_id": "turn1",
    "question": "private question",
    "answer": "private answer",
    "answered_by": "human",
    "requires_human": True,
    "state": "answered",
    "created_at": 1.0,
    "updated_at": 2.0,
    "delivery_token": "internal-lease",
    "reason": "private reason",
}


@pytest.mark.parametrize("event", ["agent.question", "agent.question.updated"])
async def test_live_question_events_only_send_scoped_redacted_invalidation(event):
    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    scopes = {
        "local": LOCAL_SCOPE,
        "global": RequestScope(kind="session", session_id="super", elevated=True),
        "same_project": RequestScope(
            kind="session", session_id="ps", project_id="p1", elevated=True
        ),
        "foreign_project": RequestScope(
            kind="session", session_id="pf", project_id="p2", elevated=True
        ),
        "origin": RequestScope(kind="session", session_id="s1", project_id="p1", task_id="t1"),
        "other_worker": RequestScope(kind="session", session_id="s2", project_id="p1"),
        "old_task": RequestScope(kind="session", session_id="s1", project_id="p1", task_id="old"),
    }
    for name, scope in scopes.items():
        manager._clients[name] = asyncio.Queue()
        manager._client_scope[name] = scope
    manager._clients["unscoped"] = asyncio.Queue()
    internal = []
    bus.subscribe(event, lambda payload: internal.append(dict(payload)))
    manager.start()
    try:
        await bus.emit(event, dict(QUESTION))
    finally:
        manager.shutdown()
    expected = {"local", "global", "same_project", "origin"}
    for name, queue in manager._clients.items():
        if name not in expected:
            assert queue.empty(), f"question leaked to {name}"
            continue
        item, wire = queue.get_nowait()
        assert item == {
            "_event_type": event,
            "id": "q1",
            "session_id": "s1",
            "task_id": "t1",
            "project_id": "p1",
            "agent_id": "a1",
            "state": "answered",
            "seq": None,
        }
        assert json.loads(wire) == item
    assert internal[0]["question"] == "private question"
    assert internal[0]["instance_token"] == "private-token"


@pytest.mark.parametrize("project,allowed", [(None, True), ("p1", True), ("p2", False)])
async def test_replay_question_payload_is_redacted_and_project_scoped(
    tmp_path, monkeypatch, project, allowed
):
    from src.api import dependencies

    db = Database(str(tmp_path / "replay.db"))
    await db.initialize()
    # Audit log FKs are deliberately soft; only this isolated file is used.
    await db.log_event("agent.question.updated", payload=json.dumps(QUESTION))
    scope = RequestScope(kind="session", session_id="super", project_id=project, elevated=True)

    class Tokens:
        async def validate(self, token):
            return scope

    monkeypatch.setattr(dependencies, "_token_store", Tokens())
    monkeypatch.setattr(dependencies, "_require_session_token", True)

    class Socket:
        headers = {"Authorization": "Bearer test"}
        query_params = {"after_seq": "0"}

        def __init__(self):
            self.frames = []

        async def accept(self):
            pass

        async def send_json(self, frame):
            self.frames.append(frame)
            if frame.get("_event_type") == "session.transcript_missing":
                raise WebSocketDisconnect()

    # A following non-question sentinel exits the real handle/replay loop
    # even when the question itself must be filtered out.
    await db.log_event("session.transcript_missing", payload="{}")
    socket = Socket()
    try:
        await asyncio.wait_for(WebSocketManager(EventBus(), db).handle(socket), timeout=5)
        questions = [f for f in socket.frames if f.get("_event_type") == "agent.question.updated"]
        assert bool(questions) is allowed
        if allowed:
            frame = questions[0]
            assert frame["id"] == "q1" and frame["session_id"] == "s1"
            assert set(frame) <= {
                "_event_type",
                "seq",
                "id",
                "session_id",
                "task_id",
                "project_id",
                "agent_id",
                "state",
            }
            assert "private" not in json.dumps(frame)
    finally:
        await db.close()
