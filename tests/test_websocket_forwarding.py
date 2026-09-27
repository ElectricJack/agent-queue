"""Guard: gate.*/session.*/task.* events reach WebSocket clients (D3/D1/D2)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from src.api.auth import RequestScope
from src.api.websocket import _FORWARDED_PREFIXES
from src.api.websocket import WebSocketManager
from src.event_bus import EventBus


def test_forwarded_prefixes_include_wave4_events() -> None:
    for prefix in ("notify.", "message.", "gate.", "session.", "task.", "pool.", "playbook."):
        assert prefix in _FORWARDED_PREFIXES, (
            f"Prefix '{prefix}' must be forwarded to WebSocket clients — "
            "the dashboard's gates/sessions/tasks pages rely on it for "
            "live invalidation."
        )


async def test_playbook_run_lifecycle_reaches_the_dashboard() -> None:
    """The V2 engine's own event names, not the unpublished ``notify.*`` ones.

    Without this the playbook cards only changed when their 30s poll landed
    inside a run, so a fleet's background work was effectively invisible.
    """
    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    queue: asyncio.Queue = asyncio.Queue()
    manager._clients["viewer"] = queue
    manager._client_scope["viewer"] = RequestScope(kind="local")
    manager.start()
    try:
        await bus.emit(
            "playbook.v2.run.started",
            {"run_id": "run-1", "playbook_id": "audit", "lifecycle": "running"},
        )
    finally:
        manager.shutdown()

    event, _frame = queue.get_nowait()
    assert event["playbook_id"] == "audit"
    assert event["lifecycle"] == "running"


_PROVIDER_EVENTS = {
    "provider.state_changed": {
        "provider": "codex",
        "vendor": "openai",
        "from_state": "available",
        "to_state": "disabled",
        "generation": 2,
        "reason": "operator disabled",
    },
    "provider.reroute_batch": {
        "provider": "codex",
        "batch_id": "batch-2",
        "moved": 2,
        "held": {"pinned": 1},
        "targets": {"standard-high-claude": 2},
        "projects": ["p1", "private-project"],
    },
    "provider.allocation_changed": {
        "provider": "codex",
        "vendor": "openai",
        "request_id": "req-1",
        "actor": "human",
        "status": "applied",
        "preview_token": "private-token",
        "request": {"interrupt_sessions": ["private-session"]},
        "preference": {"project_id": "private-project", "after": "codex"},
        "session_actions": [{"session_id": "private-session", "task_id": "private-task"}],
        "pinned": {"tasks": [{"task_id": "private-task"}]},
        "manual_agents": [{"current_task_id": "private-task"}],
        "warnings": [{"message": "private-task interrupted"}],
        "error": "private-task error",
    },
}

_PROVIDER_SCOPES = [
    pytest.param(RequestScope(kind="local"), True, False, id="local"),
    pytest.param(RequestScope(kind="session", elevated=True), True, False, id="global"),
    pytest.param(
        RequestScope(kind="session", project_id="p1", elevated=True),
        True,
        True,
        id="project",
    ),
    pytest.param(
        RequestScope(kind="session", project_id="p1", session_id="s1"),
        False,
        False,
        id="worker",
    ),
    pytest.param(RequestScope(kind="session"), False, False, id="unbound-worker"),
    pytest.param(None, False, False, id="missing-scope"),
]


def _assert_provider_frames(frames, allowed, redacted):
    assert len(frames) == (len(_PROVIDER_EVENTS) if allowed else 0)
    for frame in frames:
        event_type = frame["_event_type"]
        if redacted:
            assert frame["redacted"] is True
            assert "private" not in json.dumps(frame)
            assert frame["provider"] == "codex"
            if event_type == "provider.allocation_changed":
                assert frame["request_id"] == "req-1"
                assert frame["status"] == "applied"
                assert set(frame) <= {
                    "_event_type",
                    "seq",
                    "timestamp",
                    "provider",
                    "vendor",
                    "request_id",
                    "actor",
                    "status",
                    "redacted",
                }
            if event_type == "provider.reroute_batch":
                assert frame["moved"] == 2 and frame["held"] == {"pinned": 1}
        else:
            payload = frame.get("payload", frame)
            if isinstance(payload, str):
                payload = json.loads(payload)
            for key, value in _PROVIDER_EVENTS[event_type].items():
                assert payload[key] == value


@pytest.mark.parametrize("scope,allowed,redacted", _PROVIDER_SCOPES)
async def test_provider_events_reach_authorized_live_sockets(scope, allowed, redacted):
    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    queue = asyncio.Queue()
    manager._clients["viewer"] = queue
    manager._client_scope["viewer"] = scope
    internal = []
    bus.subscribe("provider.allocation_changed", lambda data: internal.append(dict(data)))
    manager.start()
    try:
        for event_type, payload in _PROVIDER_EVENTS.items():
            await bus.emit(event_type, dict(payload))
    finally:
        manager.shutdown()
    frames = []
    while not queue.empty():
        event, wire = queue.get_nowait()
        assert json.loads(wire) == event
        assert event["seq"] is None
        frames.append(event)
    _assert_provider_frames(frames, allowed, redacted)
    assert internal[-1]["session_actions"][0]["task_id"] == "private-task"


@pytest.mark.parametrize("scope,allowed,redacted", _PROVIDER_SCOPES[:-1])
async def test_provider_replay_uses_same_scope_projection(monkeypatch, scope, allowed, redacted):
    from src.api import dependencies

    monkeypatch.setattr(
        dependencies, "_token_store", AsyncMock(validate=AsyncMock(return_value=scope))
    )
    monkeypatch.setattr(dependencies, "_require_session_token", True)
    rows = [
        {
            "id": seq,
            "event_type": event_type,
            "payload": json.dumps(payload),
            "project_id": "private-project",
            "task_id": "private-task",
            "agent_id": "private-agent",
            "timestamp": 1.0,
        }
        for seq, (event_type, payload) in enumerate(_PROVIDER_EVENTS.items(), start=1)
    ]
    # The sentinel ends the real replay loop even when every provider row is denied.
    rows.append({"id": 4, "event_type": "session.transcript_missing", "payload": "{}"})
    db = AsyncMock(get_recent_events=AsyncMock(return_value=rows))

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

    socket = Socket()
    await asyncio.wait_for(WebSocketManager(EventBus(), db).handle(socket), timeout=5)
    frames = [
        frame for frame in socket.frames if frame.get("_event_type", "").startswith("provider.")
    ]
    _assert_provider_frames(frames, allowed, redacted)
    if allowed:
        assert [frame["seq"] for frame in frames] == [1, 2, 3]
    db.get_recent_events.assert_awaited_once_with(limit=500, after_id=0)


async def test_pool_events_are_scoped_before_websocket_fanout() -> None:
    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    manager._clients["foreign"] = asyncio.Queue()
    manager._client_scope["foreign"] = RequestScope(
        kind="session", session_id="foreign-session", project_id="other-project"
    )
    manager._clients["owner"] = asyncio.Queue()
    manager._client_scope["owner"] = RequestScope(
        kind="session", session_id="pool-session", project_id="pool-project"
    )
    manager.start()
    try:
        await bus.emit(
            "pool.session_claimed",
            {
                "project_id": "pool-project",
                "profile_id": "worker",
                "session_id": "pool-session",
                "name": "p-worker--pool-project--deadbeef",
                "task_id": "private-task",
                "task_title": "Private task title",
            },
        )
    finally:
        manager.shutdown()

    assert manager._clients["foreign"].empty()
    event, _frame = manager._clients["owner"].get_nowait()
    assert event["task_title"] == "Private task title"


async def test_live_frames_are_serialized_once_per_event_and_logged_at_debug(caplog) -> None:
    import json
    import logging

    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    manager._clients["c1"] = q1
    manager._clients["c2"] = q2
    manager._client_scope["c1"] = RequestScope(kind="local")
    manager._client_scope["c2"] = RequestScope(kind="local")
    manager.start()
    try:
        with caplog.at_level(logging.INFO, logger="src.api.websocket"):
            await bus.emit(
                "task.updated", {"task_id": "t1", "project_id": "p1", "title": "Task one"}
            )
    finally:
        manager.shutdown()

    e1, f1 = q1.get_nowait()
    e2, f2 = q2.get_nowait()
    assert f1 is f2  # the same serialized string object, not two dumps
    assert json.loads(f1)["seq"] is None and json.loads(f1)["task_id"] == "t1"
    assert e1 is e2
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "WS" in r.getMessage()]


async def test_unserializable_live_event_is_dropped_without_raising(caplog) -> None:
    import logging

    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    q1: asyncio.Queue = asyncio.Queue()
    manager._clients["c1"] = q1
    manager._client_scope["c1"] = RequestScope(kind="local")

    # Call _on_event directly (bypassing bus.emit's schema validation, which
    # would reject this payload before it ever reaches the WS fan-out) so we
    # can exercise json.dumps failing on a genuinely unserializable value.
    with caplog.at_level(logging.WARNING, logger="src.api.websocket"):
        manager._on_event(
            {
                "_event_type": "task.updated",
                "task_id": "t1",
                "project_id": "p1",
                "title": "x",
                "blob": object(),
            }
        )

    assert q1.empty()
    assert any(
        r.levelno == logging.WARNING and "unserializable" in r.getMessage() for r in caplog.records
    )
