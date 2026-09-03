from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

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
        claim_pending_event_dispatch=AsyncMock(return_value="claim-1"),
        renew_pending_event_dispatch_claim=AsyncMock(return_value=True),
        finalize_pending_event_dispatch=AsyncMock(return_value=True),
        record_pending_event_dispatch_failure=AsyncMock(return_value=True),
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
    handler.db.claim_pending_event_dispatch.assert_awaited_once()
    handler.db.finalize_pending_event_dispatch.assert_awaited_once_with(
        "event-1",
        claim_token="claim-1",
        resolved_by="service:playbook-pending-event",
        now=handler.db.finalize_pending_event_dispatch.await_args.kwargs["now"],
    )
    handler.db.resolve_pending_event.assert_not_awaited()
    assert engine.dispatch_event.await_args.kwargs["playbook_ids"] == ["default-pipeline"]
    assert engine.dispatch_event.await_args.kwargs["dispatch_id"] == "f88718f435a3"
    assert engine.dispatch_event.await_args.args[1].describe() == "service:playbook-pending-event"


async def test_dispatch_failure_restores_the_event_for_retry():
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {"type": "task.completed", "task_id": "t1"},
    }
    handler, engine = _handler(row)
    engine.dispatch_event.side_effect = RuntimeError("engine unavailable")

    result = await handler._cmd_playbook_pending_event_action(
        {"action": "dispatch", "pending_event_ids": ["event-1"]}
    )

    assert result["success"] is False
    assert result["errors"] == ["event-1: engine unavailable"]
    handler.db.record_pending_event_dispatch_failure.assert_awaited_once_with(
        "event-1",
        claim_token="claim-1",
        error="engine unavailable",
    )


async def test_dispatch_cancellation_restores_the_event_before_propagating():
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {"type": "task.completed", "task_id": "t1"},
    }
    handler, engine = _handler(row)
    engine.dispatch_event.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await handler._cmd_playbook_pending_event_action(
            {"action": "dispatch", "pending_event_ids": ["event-1"]}
        )

    handler.db.record_pending_event_dispatch_failure.assert_awaited_once_with(
        "event-1",
        claim_token="claim-1",
        error="dispatch cancelled",
    )


async def test_cancellation_during_failure_recovery_stops_the_batch():
    rows = [
        {
            "pending_event_id": f"event-{index}",
            "playbook_id": "default-pipeline",
            "event": {"type": "task.completed", "task_id": f"t{index}"},
        }
        for index in (1, 2)
    ]
    handler, engine = _handler(rows[0])
    handler.db.get_pending_events.return_value = rows
    engine.dispatch_event.side_effect = RuntimeError("engine unavailable")
    recovery_started = asyncio.Event()
    recovery_may_finish = asyncio.Event()

    async def gated_recovery(*_args, **_kwargs):
        recovery_started.set()
        await recovery_may_finish.wait()
        return True

    handler.db.record_pending_event_dispatch_failure.side_effect = gated_recovery
    command = asyncio.create_task(
        handler._cmd_playbook_pending_event_action(
            {"action": "dispatch", "pending_event_ids": ["event-1", "event-2"]}
        )
    )
    await recovery_started.wait()
    command.cancel()
    recovery_may_finish.set()

    with pytest.raises(asyncio.CancelledError):
        await command
    assert engine.dispatch_event.await_count == 1


async def test_long_dispatch_renews_its_claim(monkeypatch):
    import src.commands.playbook_v2_commands as commands

    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {"type": "task.completed", "task_id": "t1"},
    }
    handler, engine = _handler(row)
    dispatch_may_finish = asyncio.Event()

    async def slow_dispatch(*_args, **_kwargs):
        await dispatch_may_finish.wait()
        return SimpleNamespace(run_ids=("run-1",))

    async def renew(*_args, **_kwargs):
        dispatch_may_finish.set()
        return True

    monkeypatch.setattr(commands, "_PENDING_EVENT_DISPATCH_RENEW_SECONDS", 0.01)
    engine.dispatch_event.side_effect = slow_dispatch
    handler.db.renew_pending_event_dispatch_claim.side_effect = renew

    result = await handler._cmd_playbook_pending_event_action(
        {"action": "dispatch", "pending_event_ids": ["event-1"]}
    )

    assert result["dispatched_run_ids"] == ["run-1"]
    handler.db.renew_pending_event_dispatch_claim.assert_awaited_once_with(
        "event-1", claim_token="claim-1", now=ANY
    )


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
    assert handler.db.resolve_pending_event.await_args.kwargs["resolution"] == "discarded"
    handler.db.claim_pending_event_dispatch.assert_not_awaited()
    engine.dispatch_event.assert_not_awaited()
