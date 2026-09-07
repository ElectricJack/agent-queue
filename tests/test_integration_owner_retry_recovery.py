"""Automatic recovery of ended integration owners for reopened work."""
import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.integration import completion_recovery


async def test_ready_owner_recovery_is_periodic_and_project_scoped(monkeypatch):
    projects = [
        SimpleNamespace(id="train", hierarchical_integration_mode="train"),
        SimpleNamespace(id="hierarchy", hierarchical_integration_mode="hierarchy"),
        SimpleNamespace(id="legacy", hierarchical_integration_mode="disabled"),
    ]
    orch = SimpleNamespace(db=SimpleNamespace(list_projects=AsyncMock(return_value=projects)))
    recover = AsyncMock(return_value=[])
    monkeypatch.setattr(completion_recovery, "reconcile_closed_integration_owners", recover)
    monkeypatch.setattr(completion_recovery.time, "monotonic", lambda: 100.0)
    await completion_recovery.reconcile_ready_integration_owners(orch)
    assert recover.await_count == 2
    recover.assert_any_await(orch, "train", ready_only=True)
    recover.assert_any_await(orch, "hierarchy", ready_only=True)
    await completion_recovery.reconcile_ready_integration_owners(orch)
    assert recover.await_count == 2
    monkeypatch.setattr(completion_recovery.time, "monotonic", lambda: 130.0)
    await completion_recovery.reconcile_ready_integration_owners(orch)
    assert recover.await_count == 4


async def test_project_recovery_failure_does_not_skip_other_projects(monkeypatch):
    orch = SimpleNamespace(db=SimpleNamespace(list_projects=AsyncMock(return_value=[
        SimpleNamespace(id="bad", hierarchical_integration_mode="train"),
        SimpleNamespace(id="good", hierarchical_integration_mode="train"),
    ])))
    recover = AsyncMock(side_effect=[RuntimeError("unavailable"), ["reopened-task"]])
    monkeypatch.setattr(completion_recovery, "reconcile_closed_integration_owners", recover)
    await completion_recovery.reconcile_ready_integration_owners(orch)
    recover.assert_awaited_with(orch, "good", ready_only=True)


async def test_slow_probe_runs_once_in_background_and_stops_on_shutdown(monkeypatch):
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def slow(_orch):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(completion_recovery, "reconcile_ready_integration_owners", slow)
    orch = SimpleNamespace()
    completion_recovery.schedule_ready_owner_recovery(orch)
    pending = orch._integration_owner_recovery_task
    await entered.wait()
    completion_recovery.schedule_ready_owner_recovery(orch)
    assert orch._integration_owner_recovery_task is pending
    await completion_recovery.stop_ready_owner_recovery(orch)
    assert finished.is_set()
    assert pending.cancelled()
    completion_recovery.schedule_ready_owner_recovery(orch)
    assert orch._integration_owner_recovery_task is pending  # cooldown after completion
