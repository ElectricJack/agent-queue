"""Registration, explanation, and event contracts for integration trains."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

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
    "integration.candidate_green",
    "integration.candidate_red",
    "integration.repair_exhausted",
    "integration.repair_deadline_due",
    "integration.human_blocked",
    "integration.promoted",
    "integration.resolution_push_observed",
    "integration.cleanup_pending",
    "integration.root_delivered",
    "integration.batch_promoted",
    "integration.cleanup_requested",
    "task.integration_configuration_blocked",
    "integration.repair_delegate_closed",
}


def test_all_design_events_require_project_and_operation_identity():
    assert DESIGN_EVENTS <= EVENT_SCHEMAS.keys()
    for event_type in DESIGN_EVENTS:
        schema = EVENT_SCHEMAS[event_type]
        assert {"project_id", "operation_id"} <= set(schema["required"])
        payload = {"project_id": "p", "operation_id": "op"}
        if event_type.startswith("task."):
            payload |= {"task_id": "task", "title": "Task"}
        if event_type == "integration.resolution_push_observed":
            payload["promotion_intent_id"] = "intent"
        if event_type == "integration.root_delivered":
            payload |= {
                "batch_id": "batch",
                "revision": 0,
                "member_ordinal": 0,
                "receipt_id": "receipt",
            }
        if event_type in {"integration.batch_promoted", "integration.cleanup_requested"}:
            payload |= {
                "batch_id": "batch",
                "revision": 0,
                "intent_id": "intent",
                "head_sha": "a" * 40,
            }
        if event_type in {"integration.candidate_green", "integration.candidate_red"}:
            payload |= {"batch_id": "batch", "revision": 0, "head_sha": "a" * 40}
        assert not validate_event(
            event_type,
            payload,
        )


def test_hierarchy_event_payloads_expose_exact_typed_command_inputs():
    expected = {
        "task.child_added": {
            "parent_id": str,
            "children": list,
            "expected_generation": int,
        },
        "task.parent_checkpointed": {
            "task_id": str,
            "head_sha": str,
            "generation": int,
        },
        "delivery.ready": {
            "operation_key": str,
            "source_task_id": str,
            "source_head": str,
            "source_base": str,
            "expected_target": str,
            "fence": dict,
        },
        "delivery.applied": {
            "promotion_intent_id": str,
            "receipt_id": str,
            "source_task_id": str,
            "target_task_id": str,
            "repository_id": str,
            "target_branch": str,
        },
        "task.integration_ready": {
            "task_id": str,
            "episode_id": str,
            "generation": int,
            "head_sha": str,
            "verifier_task_id": (str, type(None)),
            "target": dict,
            "expected_token": int,
            "next_owner_id": str,
            "next_role": str,
        },
        "task.integration_verified": {
            "task_id": str,
            "generation": int,
            "head_sha": str,
            "verification_id": str,
        },
        "integration.repair_exhausted": {"stage": int},
        "integration.repair_deadline_due": {"stage": int},
        "integration.ci_completed": {
            "task_id": str,
            "generation": int,
            "head_sha": str,
            "evidence_id": str,
            "evidence_ids": list,
            "conclusion": str,
            "target_kind": str,
        },
        "integration.candidate_green": {
            "batch_id": str,
            "revision": int,
            "head_sha": str,
        },
        "integration.candidate_red": {
            "batch_id": str,
            "revision": int,
            "head_sha": str,
        },
        "integration.resolution_push_observed": {"promotion_intent_id": str},
        "integration.cleanup_pending": {"promotion_intent_id": str},
        "integration.root_delivered": {
            "batch_id": str,
            "revision": int,
            "member_ordinal": int,
            "receipt_id": str,
        },
        "integration.batch_promoted": {
            "batch_id": str,
            "revision": int,
            "intent_id": str,
            "head_sha": str,
        },
        "integration.cleanup_requested": {
            "batch_id": str,
            "revision": int,
            "intent_id": str,
            "head_sha": str,
        },
        "task.integration_configuration_blocked": {
            "task_id": str,
            "reason": str,
        },
        "integration.repair_delegate_closed": {
            "stage": int,
            "task_id": str,
            "session_id": str,
            "instance_token": str,
            "workspace_id": str,
            "fence_token": int,
        },
    }
    for event_type, payload_fields in expected.items():
        schema = EVENT_SCHEMAS[event_type]
        base_fields = {
            "project_id",
            "operation_id",
        }
        if event_type.startswith("task."):
            base_fields |= {"task_id", "title"}
        assert set(schema["required"] + schema["optional"]) == {
            *base_fields,
            *payload_fields,
        }
        expected_types = {
            "project_id": str,
            "operation_id": str,
            **payload_fields,
        }
        if event_type.startswith("task."):
            expected_types |= {"task_id": str, "title": str}
        assert schema["types"] == expected_types
        assert set(schema["fields"]) == {
            *base_fields,
            *payload_fields,
        }


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
    implemented = {
        "integration_schedule_due",
        "integration_seal",
        "integration_transfer_owner",
        "integration_file_children",
        "integration_checkpoint_parent",
        "integration_delivery_readiness",
        "integration_parent_verify",
        "integration_complete_parent",
        "integration_mutate_hierarchy",
        "delivery_promote",
        "delivery_receipts",
        "integration_reconcile_promotion",
        "integration_resolve_conflict",
        "integration_push_conflict_resolution",
        "integration_promote_main",
        "integration_build_candidate",
        "integration_ci_evidence",
        "integration_release",
        "integration_cleanup",
        "integration_repair_start",
        "integration_repair_dispatch",
        "integration_record_repair",
        "integration_repair_timeout",
        "integration_status",
        "integration_flush",
        "integration_enable",
        "integration_waive_history",
        "integration_resume",
        "integration_abort",
        "integration_retry_cleanup",
    }
    assert registry.names() & DESIGN_INTEGRATION_COMMANDS == implemented
    assert not (registry.names() & (DESIGN_INTEGRATION_COMMANDS - implemented))


def test_schedule_contract_is_typed_and_retry_safe():
    registry = ContractRegistry()
    register_integration_contracts(registry)
    schedule = registry.require("integration_schedule_due").contract.execution

    assert {outcome.name for outcome in schedule.outcomes} == {
        "due",
        "not_due",
        "coalesced",
        "disabled",
    }
    assert schedule.idempotency.mode == "natural"
    assert schedule.retry_safe is True
    assert schedule.args_model(project_id="p", now=1.0, trigger="manual").trigger == "manual"
    with pytest.raises(Exception):
        schedule.args_model(project_id="p", now=1.0, trigger="caller-defined")


def test_repair_contracts_expose_exact_typed_public_protocol():
    registry = ContractRegistry()
    register_integration_contracts(registry)

    start = registry.require("integration_repair_start").contract.execution
    dispatch = registry.require("integration_repair_dispatch").contract.execution
    record = registry.require("integration_record_repair").contract.execution
    timeout = registry.require("integration_repair_timeout").contract.execution

    assert {row.name for row in start.outcomes} == {
        "started",
        "already_started",
        "stale",
        "invariant_error",
    }
    assert {row.name for row in dispatch.outcomes} == {
        "dispatched",
        "already_dispatched",
        "writer_reused",
        "busy",
        "configuration_blocked",
        "stale",
        "human_required",
    }
    assert {row.name for row in record.outcomes} == {
        "continue",
        "escalate",
        "human_required",
        "budget_exhausted",
    }
    assert {row.name for row in timeout.outcomes} == {
        "expired",
        "not_due",
        "already_terminal",
        "stale",
    }
    assert start.side_effect.value == "composite"
    assert dispatch.side_effect.value == "composite"
    assert start.idempotency.mode == dispatch.idempotency.mode == "natural"
    assert start.retry_safe is dispatch.retry_safe is True
    assert {clause.subject for clause in dispatch.effects} == {
        EffectSubject.INTEGRATION_OPERATION,
        EffectSubject.BRANCH_OWNERSHIP,
        EffectSubject.TASK_EXECUTION,
    }
    assert start.args_model(
        operation_id="op", starting_sha="a" * 40, trigger_id="trigger"
    ).starting_sha == "a" * 40
    with pytest.raises(Exception):
        start.args_model(
            operation_id="op", starting_sha="A" * 40, trigger_id="trigger"
        )
    with pytest.raises(Exception):
        dispatch.args_model(operation_id="op", stage=2)
    for action in {
        "repair",
        "infrastructure_retry",
        "inconclusive",
        "completion_ready",
        "dispatch_debug",
        "block_for_human",
        "duplicate",
        "stale",
    }:
        assert record.result_model(action=action).action == action
    for action in {
        "ignore",
        "dispatch_debug",
        "block_for_human",
        "none",
        "wait",
        "awaiting_promotion",
    }:
        assert timeout.result_model(action=action).action == action
    with pytest.raises(Exception):
        record.result_model(action="caller_selected_action")
    with pytest.raises(Exception):
        timeout.result_model(action="caller_selected_action")


def test_parent_completion_contracts_expose_prescribed_outcomes():
    registry = ContractRegistry()
    register_integration_contracts(registry)

    assert {row.name for row in registry.require("integration_delivery_readiness").contract.execution.outcomes} == {
        "ready", "waiting", "failed", "invariant_error"
    }
    readiness_model = registry.require(
        "integration_delivery_readiness"
    ).contract.execution.result_model
    assert readiness_model.model_fields["on_failed_child"].annotation == (
        Literal["block", "ask"] | None
    )
    assert {row.name for row in registry.require("integration_parent_verify").contract.execution.outcomes} == {
        "verified", "stale_generation", "stale_head", "invalid_evidence"
    }
    assert {row.name for row in registry.require("integration_complete_parent").contract.execution.outcomes} == {
        "completed", "waiting", "stale_verification", "invariant_error"
    }


def test_promotion_contracts_declare_retry_and_domain_identity():
    registry = ContractRegistry()
    register_integration_contracts(registry)

    promote = registry.require("delivery_promote").contract.execution
    assert promote.idempotency.mode == "keyed"
    assert promote.idempotency.key_field == "operation_key"
    assert promote.retry_safe is True
    assert {outcome.name for outcome in promote.outcomes} == {
        "promoted",
        "already_promoted",
        "conflict",
        "source_moved",
        "target_moved",
    }
    reconcile = registry.require("integration_reconcile_promotion").contract.execution
    assert reconcile.idempotency.mode == "keyed"
    assert reconcile.idempotency.key_field == "intent_id"
    assert reconcile.retry_safe is True
    resolve = registry.require("integration_resolve_conflict").contract.execution
    assert resolve.idempotency.key_field == "intent_id"
    assert resolve.retry_safe is True
    assert {outcome.name for outcome in resolve.outcomes} == {
        "reserved",
        "already_reserved",
        "stale",
        "invariant_error",
    }
    push = registry.require("integration_push_conflict_resolution").contract.execution
    assert push.idempotency.key_field == "intent_id"
    assert push.retry_safe is True
    assert {outcome.name for outcome in push.outcomes} == {
        "pushed",
        "already_applied",
        "target_moved",
        "stale",
    }
    assert set(push.args_model.model_fields) == {"intent_id", "fence"}
    root = registry.require("integration_promote_main").contract.execution
    assert root.idempotency.mode == "natural"
    assert set(root.args_model.model_fields) == {"batch_id", "revision"}
    assert {outcome.name for outcome in root.outcomes} == {
        "promoted", "already_promoted", "base_moved", "ci_missing",
        "non_fast_forward", "wait", "reconciliation_blocked", "stale",
        "configuration_blocked",
    }


@pytest.mark.asyncio
async def test_root_promotion_command_is_registered_and_strictly_typed():
    handler = object.__new__(CommandHandler)
    handler.orchestrator = SimpleNamespace(plugin_registry=None)
    handler.config = SimpleNamespace(
        playbooks=SimpleNamespace(enabled=True),
        memory=SimpleNamespace(enabled=True),
        security=SimpleNamespace(capability_enforcement="off"),
    )
    result = await handler.execute("integration_promote_main", {})
    assert result["outcome"] == "runtime_error"
