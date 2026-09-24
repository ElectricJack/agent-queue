"""Reviewed disabled hierarchy policy and its real command flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, update

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import DecisionStep, TerminalStep, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)
from tests.test_integration_parent_completion import _code_receipt, _parent_tree
from src.database.tables import integration_check_evidence, task_integration_checkpoints, tasks
from src.models import AgentProfile, Project, TaskStatus


FIXTURE = Path("tests/fixtures/playbooks/historical-v2/hierarchical-delivery/artifact.json")
PARENT_FIXTURE = Path("tests/fixtures/playbooks/v2/agent-queue-parent-integration/artifact.json")


def _artifact():
    return load_definition_json(FIXTURE.read_text(encoding="utf-8"))


def test_reviewed_hierarchy_routes_lifecycle_without_invented_success():
    artifact = _artifact()
    by_rule = {rule.id: artifact.steps[rule.entry_step] for rule in artifact.rules}

    assert by_rule["reconcile-resolution-push"].command == (
        "integration_reconcile_promotion"
    )
    assert by_rule["reconcile-resolution-push"].inputs["intent_id"].path == (
        "promotion_intent_id"
    )
    assert set(by_rule["expire-repair-stage"].transitions) == {
        "expired",
        "not_due",
        "already_terminal",
        "stale",
        "runtime_error",
    }
    repair_close = by_rule["observe-repair-close"]
    assert isinstance(repair_close, TerminalStep)
    assert repair_close.outcome == "completed"
    assert isinstance(
        artifact.steps["project-delivery-readiness--failed-policy"], DecisionStep
    )
    assert {
        artifact.steps[f"{rule}--blocked"].outcome
        for rule in (
            "project-delivery-readiness",
            "completed-child-readiness",
            "failed-child-readiness",
        )
    } == {"blocked"}
    assert by_rule["completed-child-readiness"].command == (
        "integration_delivery_readiness"
    )
    assert by_rule["failed-child-readiness"].command == (
        "integration_delivery_readiness"
    )


@pytest.mark.parametrize(
    ("policy", "terminal_outcome", "gate_count"),
    [("block", "blocked", 0), ("ask", "completed", 1)],
)
async def test_failed_delivery_event_runs_real_readiness_and_policy_commands(
    command_handler_factory,
    policy,
    terminal_outcome,
    gate_count,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="integration project"))
    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(
        db, children=1, on_failed_child=policy
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )

    artifact = _artifact()
    ref = artifact_ref_for(artifact)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 100.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([ref]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_delivery_readiness", "gate_create"]
        ),
    )
    base_event = {
        "event_type": "delivery.applied",
        "project_id": "p",
        "operation_id": checkpointed["operation_id"],
        "promotion_intent_id": "intent",
        "receipt_id": "receipt",
        "source_task_id": children[0],
        "target_task_id": "parent",
        "repository_id": "repo",
        "target_branch": "aq/parent",
    }

    set_handler_provider(lambda: handler)
    try:
        for ordinal in (1, 2):
            result = await engine.dispatch_event(
                base_event | {"event_id": f"failed-delivery-{ordinal}"}, principal
            )
            assert result.rules_selected == ("project-delivery-readiness",)
    finally:
        set_handler_provider(None)

    selected = [
        snapshot
        for snapshot in runs.snapshots.values()
        if snapshot.rule_id == "project-delivery-readiness"
    ]
    assert len(selected) == 2
    assert {snapshot.lifecycle.value for snapshot in selected} == {terminal_outcome}, [
        (snapshot.error_code, snapshot.error, snapshot.current_step_id, snapshot.bindings)
        for snapshot in selected
    ] + [(r.step_id, r.outcome, r.selected_transition, r.error_code, r.result) for r in runs.receipts]
    assert len(await db.list_gates(project_id="p", status="open")) == gate_count
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED


async def test_hosted_parent_ci_event_verifies_without_a_live_workspace(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="integration project"))
    _hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    head_sha = "d" * 40
    await _code_receipt(db, children[0], "a" * 40, head_sha)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(state="verifying", checkpoint_sha=head_sha)
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=head_sha,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    assert await db.get_workspace_for_task("parent") is None

    artifact = load_definition_json(PARENT_FIXTURE.read_text(encoding="utf-8"))
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 100.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([artifact_ref_for(artifact)]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_parent_verify"]),
    )
    event = {
        "event_id": "parent-ci-green",
        "event_type": "integration.ci_completed",
        "project_id": "p",
        "operation_id": checkpointed["operation_id"],
        "target_kind": "parent",
        "task_id": "parent",
        "generation": 1,
        "head_sha": head_sha,
        "evidence_ids": ["check-unit"],
        "evidence_id": "check-unit",
        "conclusion": "success",
    }

    set_handler_provider(lambda: handler)
    try:
        result = await engine.dispatch_event(event, principal)
    finally:
        set_handler_provider(None)

    assert result.rules_selected == ("verify-parent",)
    command_receipts = [
        receipt for receipt in runs.receipts
        if receipt.step_id == "verify-parent--verify" and receipt.outcome != "started"
    ]
    assert len(command_receipts) == 1
    assert command_receipts[0].outcome == "success"
    assert command_receipts[0].selected_transition.endswith("::verified")
    assert {snapshot.lifecycle.value for snapshot in runs.snapshots.values()} == {"completed"}
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["verified_sha"] == head_sha
