from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.config import PlaybooksConfig


def _handler(row):
    handler = PlaybookV2CommandsMixin()
    handler.config = SimpleNamespace(
        playbooks=PlaybooksConfig(
            v2_api=True,
            v2_storage_enabled=True,
            v2_activation_writes=True,
        )
    )
    handler.db = SimpleNamespace(
        get_pending_events=AsyncMock(return_value=[row]),
        resolve_pending_event=AsyncMock(return_value=True),
    )
    engine = MagicMock()
    engine.dispatch_event = AsyncMock(return_value=SimpleNamespace(run_ids=("run-1",)))
    handler._v2_engine = MagicMock(return_value=engine)
    return handler, engine


async def test_dispatch_reenters_the_engine():
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {"type": "task.completed", "task_id": "t1"},
    }
    handler, engine = _handler(row)
    result = await handler._cmd_playbook_pending_event_action(
        {"action": "dispatch", "pending_event_ids": ["event-1"]}
    )
    assert result["dispatched_run_ids"] == ["run-1"]
    assert engine.dispatch_event.await_args.kwargs["playbook_ids"] == ["default-pipeline"]
    assert engine.dispatch_event.await_args.args[1].describe() == "service:playbook-pending-event"


async def test_discard_marks_resolved_without_dispatch():
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {},
    }
    handler, engine = _handler(row)
    result = await handler._cmd_playbook_pending_event_action(
        {"action": "discard", "pending_event_ids": ["event-1"]}
    )
    assert result["discarded_ids"] == ["event-1"]
    handler.db.resolve_pending_event.assert_awaited_once()
    engine.dispatch_event.assert_not_awaited()
