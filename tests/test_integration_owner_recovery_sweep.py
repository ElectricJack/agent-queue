"""Owner-recovery hooks around the integration reconciliation loop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.config import IntegrationConfig
from src.integration.repair import RepairService
from src.integration.service import IntegrationService
from src.orchestrator.core import Orchestrator


async def test_tick_isolates_an_owner_recovery_failure_and_runs_other_sources():
    repair = SimpleNamespace(retire_terminal_delegates=AsyncMock(return_value=[]))
    recovery = AsyncMock(side_effect=RuntimeError("temporary Git failure"))
    outbox = SimpleNamespace(dispatch_due=AsyncMock())
    service = IntegrationService(
        SimpleNamespace(),
        SimpleNamespace(),
        repair,
        outbox,
        owner_recovery_handler=recovery,
    )

    await service.tick(100.0)

    recovery.assert_awaited_once_with(100.0)
    repair.retire_terminal_delegates.assert_awaited_once_with(100.0)
    outbox.dispatch_due.assert_awaited_once_with(100.0)


async def test_sweep_does_nothing_while_the_switch_is_off(monkeypatch):
    recovery = SimpleNamespace(candidates=AsyncMock(), recover_many=AsyncMock())
    monkeypatch.setattr(
        "src.integration.owner_recovery.owner_recovery_for", lambda _orchestrator: recovery
    )
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(integration=IntegrationConfig(owner_recovery_sweep=False)),
        _owner_recovery_next_due=0.0,
    )

    await Orchestrator._sweep_stranded_owners(orchestrator, 100.0)

    recovery.candidates.assert_not_awaited()
    recovery.recover_many.assert_not_awaited()


async def test_sweep_recovers_quiet_candidates_only_once_per_five_minutes(monkeypatch):
    recovery = SimpleNamespace(
        candidates=AsyncMock(return_value=[{"id": "first"}, {"id": "second"}]),
        recover_many=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.integration.owner_recovery.owner_recovery_for", lambda _orchestrator: recovery
    )
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(integration=IntegrationConfig(owner_recovery_sweep=True)),
        _owner_recovery_next_due=0.0,
    )

    await Orchestrator._sweep_stranded_owners(orchestrator, 1.0)
    await Orchestrator._sweep_stranded_owners(orchestrator, 300.0)
    await Orchestrator._sweep_stranded_owners(orchestrator, 301.0)

    assert recovery.candidates.await_count == 2
    recovery.candidates.assert_awaited_with(quiet_seconds=600, limit=50)
    assert recovery.recover_many.await_args_list[0].args == (["first", "second"],)
    assert recovery.recover_many.await_args_list[0].kwargs == {"principal": "sweep"}
    assert recovery.recover_many.await_args_list[1].args == (["first", "second"],)


async def test_delegate_retirement_recovers_the_owner_rows_named_by_cleanup(monkeypatch):
    released = [
        {
            "task_id": "delegate",
            "cleanup": {
                "blockers": [
                    {"code": "branch_owner_retained", "owner_row_id": "owner-a"},
                    {"code": "workspace_locked", "workspace_id": "workspace-a"},
                    {"code": "branch_owner_retained", "owner_row_id": "owner-b"},
                ]
            },
        }
    ]
    monkeypatch.setattr(
        "src.integration.delegate_release.release_delegates", AsyncMock(return_value=released)
    )
    recovery = SimpleNamespace(recover_many=AsyncMock(return_value=[]))
    service = RepairService(SimpleNamespace(), owner_recovery=recovery)

    assert await service.retire_terminal_delegates(100.0) == ["delegate"]

    recovery.recover_many.assert_awaited_once_with(
        ["owner-a", "owner-b"], principal="delegate_retirement"
    )
