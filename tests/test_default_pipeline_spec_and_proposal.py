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

The ``gate.resolved`` -> ``task_batch_commit`` fan-in rule is wired
end-to-end: ``_resolve_gate_and_emit`` now propagates the gate's
``await_id`` onto the bus payload, and the rule pipes it into
``task_batch_commit(proposal_id=…)``.  Test 5 exercises the full leg.
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


async def test_gate_resolved_commits_proposal_end_to_end(
    command_handler_factory, pipeline_engine_factory
):
    """gate.resolved carrying await_id -> task_batch_commit fires with
    proposal_id resolved by Jinja. Full leg wired now that
    _resolve_gate_and_emit propagates await_id."""
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p1", name="p1"))

    # Propose a batch so we have a real proposal to commit.
    prop = await h.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    assert prop["success"]
    proposal_id = prop["proposal_id"]

    # Simulate the human-approve leg: gate.resolved with await_id=proposal_id.
    await engine.dispatch(
        "gate.resolved",
        {
            "project_id": "p1",
            "gate_id": "g-1",
            "gate_type": "human",
            "resolved_by": "test",
            "await_id": proposal_id,
        },
    )

    # Proposal should now be committed and the task materialised.
    from src.database.queries.proposal_queries import get_proposal
    row = await get_proposal(db, proposal_id)
    assert row is not None and row["status"] == "committed"
    tasks = await db.list_tasks(project_id="p1")
    assert any(t.title == "A" for t in tasks)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
