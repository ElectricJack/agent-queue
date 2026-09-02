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
    assert manager._clients["owner"].get_nowait()["task_title"] == "Private task title"
