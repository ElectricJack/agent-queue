"""Reviewed disabled hierarchy policy and its real command flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import DecisionStep, TerminalStep, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.profiles.capabilities import CapabilityPolicy
from src.vault import ensure_default_playbooks
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)
from tests.test_integration_parent_completion import _parent_tree
from src.database.tables import tasks
from src.models import AgentProfile, Project, TaskStatus


FIXTURE = Path("tests/fixtures/playbooks/v2/hierarchical-delivery/artifact.json")
SOURCE = Path("src/prompts/default_playbooks/hierarchical-delivery.md")


def _artifact():
    return load_definition_json(FIXTURE.read_text(encoding="utf-8"))


def test_hierarchy_source_is_seeded_write_if_absent_and_remains_disabled(tmp_path):
    first = ensure_default_playbooks(str(tmp_path))
    installed = tmp_path / "vault/system/playbooks/hierarchical-delivery.md"

    assert "hierarchical-delivery.md" in first["created"]
    assert installed.read_bytes() == SOURCE.read_bytes()
    installed.write_text("operator-owned\n", encoding="utf-8")

    second = ensure_default_playbooks(str(tmp_path))

    assert "hierarchical-delivery.md" in second["skipped"]
    assert installed.read_text(encoding="utf-8") == "operator-owned\n"
    assert "enabled: false" in SOURCE.read_text(encoding="utf-8")


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
    assert by_rule["completed-child-readiness"].command == (
        "integration_delivery_readiness"
    )
    assert by_rule["failed-child-readiness"].command == (
        "integration_delivery_readiness"
    )


@pytest.mark.parametrize(
    ("policy", "terminal_outcome", "gate_count"),
    [("block", "failed", 0), ("ask", "completed", 1)],
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
