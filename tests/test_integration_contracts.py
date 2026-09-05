"""Registration, explanation, and event contracts for integration trains."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands.contracts.builtin import register_builtin_contracts
from src.commands.contracts.integration import (
    DESIGN_INTEGRATION_COMMANDS,
    register_integration_contracts,
)
from src.commands.contracts.models import EffectSubject
from src.commands.contracts.registry import ContractRegistry
from src.commands.handler import CommandHandler
from src.event_schemas import EVENT_SCHEMAS, validate_event
from src.playbooks.explanation import can_render


DESIGN_EVENTS = {
    "task.child_added",
    "task.parent_checkpointed",
    "delivery.ready",
    "delivery.applied",
    "task.integration_ready",
    "task.integration_verified",
    "integration.sweep_due",
    "integration.sealed",
    "integration.ci_completed",
    "integration.repair_exhausted",
    "integration.repair_deadline_due",
    "integration.human_blocked",
    "integration.promoted",
    "integration.cleanup_pending",
}


def test_all_design_events_require_project_and_operation_identity():
    assert DESIGN_EVENTS <= EVENT_SCHEMAS.keys()
    for event_type in DESIGN_EVENTS:
        schema = EVENT_SCHEMAS[event_type]
        assert {"project_id", "operation_id"} <= set(schema["required"])
        assert not validate_event(
            event_type,
            {"project_id": "p", "operation_id": "op"},
        )


def test_integration_effect_subjects_use_existing_renderable_clauses():
    from src.commands.contracts.models import CreateClause, ReadClause, UpdateClause

    clauses = (
        CreateClause(subject=EffectSubject.INTEGRATION_OPERATION),
        UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
        ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),
    )
    assert all(can_render(clause) for clause in clauses)


def test_builtin_registration_invokes_integration_registration(monkeypatch):
    called = 0

    def record(_registry):
        nonlocal called
        called += 1

    monkeypatch.setattr("src.commands.contracts.integration.register_integration_contracts", record)
    register_builtin_contracts(ContractRegistry())
    assert called == 1


def test_unimplemented_integration_operations_are_not_registered():
    registry = ContractRegistry()
    register_integration_contracts(registry)
    assert registry.names() & DESIGN_INTEGRATION_COMMANDS == {"integration_transfer_owner"}
    assert not (
        registry.names() & (DESIGN_INTEGRATION_COMMANDS - {"integration_transfer_owner"})
    )


@pytest.mark.asyncio
async def test_unimplemented_integration_operation_is_explicitly_rejected():
    handler = object.__new__(CommandHandler)
    handler.orchestrator = SimpleNamespace(plugin_registry=None)
    handler.config = SimpleNamespace(
        playbooks=SimpleNamespace(enabled=True),
        memory=SimpleNamespace(enabled=True),
        security=SimpleNamespace(capability_enforcement="off"),
    )
    result = await handler.execute("integration_promote_main", {})
    assert result == {"error": "Unknown command: integration_promote_main"}
