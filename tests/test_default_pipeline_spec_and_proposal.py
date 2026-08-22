"""Dv2 Phase 6 Group C, Task 6: default-pipeline reactions to spec.approved
and proposal.ready.

Verifies:
1. default-pipeline.md compiles with the two new triggers registered, plus
   the gate.resolved trigger scoped to gate_type=human via a structured
   trigger filter.
2. Dispatching ``spec.approved`` creates exactly one ``spec-ingest`` task,
   keyed by ``dedup_key = "spec-ingest:<spec_path>"``.
3. Dispatching ``spec.approved`` twice for the same spec path is idempotent
   (ensure_task dedup) — only one ingest task exists.
4. Dispatching ``proposal.ready`` raises an open ``human`` gate whose
   ``await_id`` is the proposal id.

Note: the ``gate.resolved`` -> ``task_batch_commit`` fan-in rule is compiled
and included in the pipeline, but ``task_batch_commit`` (Group A, Task 3) is
not yet implemented in this checkout, and the ``gate.resolved`` bus payload
does not currently carry ``await_id`` (see ``Orchestrator._resolve_gate_and_emit``).
Wiring the full commit leg end-to-end is left to Group A; this file only
proves the rule compiles and is reachable from the gate.resolved trigger.
"""
from __future__ import annotations

import pytest

from src.models import AgentProfile, Project

# ``command_handler_factory`` and ``pipeline_engine_factory`` fixtures, plus
# the ``PipelineEngine`` test helper, live in tests/conftest.py.
from tests.conftest import DEFAULT_PIPELINE_PATH as _DEFAULT_PIPELINE


def test_default_pipeline_compiles_with_spec_and_proposal_triggers():
    """default-pipeline.md must compile and register the three new rules."""
    from src.playbooks.pipeline_compiler import compile_pipeline

    src = _DEFAULT_PIPELINE.read_text(encoding="utf-8")
    result = compile_pipeline(src)
    assert result.success, result.errors

    pb = result.playbook
    assert pb is not None
    assert "spec.approved" in pb.pipeline_rules
    assert "proposal.ready" in pb.pipeline_rules
    assert "gate.resolved" in pb.pipeline_rules

    # The gate.resolved trigger is scoped via a structured filter, not a
    # rule-level ``when`` (the deterministic ``when`` evaluator only
    # supports truthy/not_null checks, not string equality).
    gate_resolved_trigger = next(
        t for t in pb.triggers if t.event_type == "gate.resolved"
    )
    assert gate_resolved_trigger.filter == {"gate_type": "human"}

    # ensure_task node with a spec-ingest dedup key + profile.
    spec_ingest_found = any(
        node.action
        and node.action.get("command") == "ensure_task"
        and node.action.get("args", {}).get("profile_id") == "spec-ingest"
        and "spec-ingest:" in str(node.action.get("args", {}).get("dedup_key", ""))
        for node in pb.nodes.values()
    )
    assert spec_ingest_found, "No ensure_task node wired to profile_id=spec-ingest"

    # gate_create node with gate_type=human for the proposal gate.
    proposal_gate_found = any(
        node.action
        and node.action.get("command") == "gate_create"
        and node.action.get("args", {}).get("gate_type") == "human"
        for node in pb.nodes.values()
    )
    assert proposal_gate_found, "No gate_create(gate_type=human) node for proposal.ready"

    # task_batch_commit node reachable from gate.resolved.
    commit_found = any(
        node.action and node.action.get("command") == "task_batch_commit"
        for node in pb.nodes.values()
    )
    assert commit_found, "No task_batch_commit node wired to gate.resolved"


async def test_spec_approved_creates_ingest_task(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p1", name="p1"))
    await db.upsert_profile(AgentProfile(id="spec-ingest", name="Spec Ingest"))

    spec_path = "/vault/projects/p1/specs/2026-08-21-a.md"
    await engine.dispatch(
        "spec.approved", {"project_id": "p1", "spec_path": spec_path}
    )

    all_tasks = await db.list_tasks(project_id="p1")
    ingest = [
        t for t in all_tasks if t.dedup_key == f"spec-ingest:{spec_path}"
    ]
    assert len(ingest) == 1, f"Expected 1 ingest task, got {len(ingest)}"
    assert ingest[0].profile_id == "spec-ingest"


async def test_spec_approved_is_idempotent(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p1", name="p1"))
    await db.upsert_profile(AgentProfile(id="spec-ingest", name="Spec Ingest"))

    spec_path = "/vault/projects/p1/specs/2026-08-21-b.md"
    payload = {"project_id": "p1", "spec_path": spec_path}
    for _ in range(3):
        await engine.dispatch("spec.approved", payload)

    all_tasks = await db.list_tasks(project_id="p1")
    ingest = [
        t for t in all_tasks if t.dedup_key == f"spec-ingest:{spec_path}"
    ]
    assert len(ingest) == 1, f"Expected exactly 1 ingest task, got {len(ingest)}"


async def test_proposal_ready_raises_human_gate(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p1", name="p1"))

    await engine.dispatch(
        "proposal.ready", {"project_id": "p1", "proposal_id": "prop-123"}
    )

    gates = await db.list_gates(project_id="p1", status="open")
    human_gates = [
        g for g in gates
        if g["gate_type"] == "human" and g.get("await_id") == "prop-123"
    ]
    assert len(human_gates) == 1, f"Expected 1 open human gate, got: {gates}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
