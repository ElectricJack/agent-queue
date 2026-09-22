"""Recovery wiring for ``integration.stranded_fences --fix``."""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.doctor.integration_checks import _fix_stranded_fences
from src.doctor.models import CheckResult, DoctorContext, Severity
from src.integration.owner_recovery import RecoveryOutcome

integration_checks = import_module("src.doctor.integration_checks")


@pytest.mark.asyncio
async def test_stranded_fences_fix_recovers_every_listed_row_and_reports_outcomes(monkeypatch):
    recovery = SimpleNamespace(
        recover_many=AsyncMock(
            return_value=[
                RecoveryOutcome("owner-a", "released", None, {"proof": "a"}),
                RecoveryOutcome("owner-b", "not_eligible", "writer_live", {"proof": "b"}),
            ]
        )
    )
    check = AsyncMock(
        return_value=CheckResult(
            id="integration.stranded_fences",
            severity=Severity.OK,
            detail="no integration branch is held by a writer that is gone",
            data={"count": 0, "fences": []},
        )
    )
    monkeypatch.setattr(
        integration_checks,
        "_find_stranded_fences",
        AsyncMock(return_value=[{"owner_row_id": "owner-a"}, {"owner_row_id": "owner-b"}]),
    )
    monkeypatch.setattr(integration_checks, "_check_stranded_fences", check)
    monkeypatch.setattr(
        integration_checks, "owner_recovery_for", lambda orchestrator: recovery, raising=False
    )
    ctx = DoctorContext(
        config=SimpleNamespace(),
        handler=SimpleNamespace(orchestrator=object()),
    )

    result = await _fix_stranded_fences(ctx)

    recovery.recover_many.assert_awaited_once_with(
        ["owner-a", "owner-b"], principal="doctor"
    )
    check.assert_awaited_once_with(ctx)
    assert result.fixable is True
    assert result.fix_applied is True
    assert result.data["outcomes"] == [
        {"owner_row_id": "owner-a", "outcome": "released", "reason": None,
         "evidence": {"proof": "a"}, "dry_run": False},
        {"owner_row_id": "owner-b", "outcome": "not_eligible", "reason": "writer_live",
         "evidence": {"proof": "b"}, "dry_run": False},
    ]


@pytest.mark.asyncio
async def test_stranded_fences_fix_reports_when_orchestrator_is_unavailable(monkeypatch):
    check = AsyncMock(
        return_value=CheckResult(
            id="integration.stranded_fences",
            severity=Severity.WARN,
            detail="a stranded owner remains",
            data={"count": 1, "fences": [{"owner_row_id": "owner-a"}]},
        )
    )
    monkeypatch.setattr(integration_checks, "_check_stranded_fences", check)
    monkeypatch.setattr(integration_checks, "owner_recovery_for", lambda orchestrator: None, raising=False)
    ctx = DoctorContext(config=SimpleNamespace(), handler=None)

    result = await _fix_stranded_fences(ctx)

    check.assert_awaited_once_with(ctx)
    assert result.fixable is False
    assert result.fix_applied is False
    assert result.data["fix_unavailable"] == "orchestrator not available"
