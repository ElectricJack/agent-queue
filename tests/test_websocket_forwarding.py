"""Guard: gate.*/session.*/task.* events reach WebSocket clients (D3/D1/D2)."""

from __future__ import annotations

import asyncio

from src.api.auth import RequestScope
from src.api.websocket import _FORWARDED_PREFIXES
from src.api.websocket import WebSocketManager
from src.event_bus import EventBus


def test_forwarded_prefixes_include_wave4_events() -> None:
    for prefix in ("notify.", "message.", "gate.", "session.", "task.", "pool."):
        assert prefix in _FORWARDED_PREFIXES, (
            f"Prefix '{prefix}' must be forwarded to WebSocket clients — "
            "the dashboard's gates/sessions/tasks pages rely on it for "
            "live invalidation."
        )


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
        r.levelno == logging.WARNING and "unserializable" in r.getMessage()
        for r in caplog.records
    )
