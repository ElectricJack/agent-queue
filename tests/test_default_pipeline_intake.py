"""The shipped default-pipeline's intake rules, end to end on the real handler.

Policy simplification (task agile-glacier.2, audit F1 and §5's regression
table): the reviewed artifact is dispatched against ``gate.resolved``,
``spec.approved`` and ``proposal.ready`` events.  Rejected, expired and
unrelated decisions create no task graph; a re-delivered approval creates
exactly one; and a refused command ends its run ``failed``, never
``completed``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal
from src.database.queries.proposal_queries import get_proposal
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

ARTIFACT = Path("tests/fixtures/playbooks/v2/default-pipeline/artifact.json")
PROJECT = "p1"


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if getattr(h, "_db", None) is not None:
        await h._db.close()


@pytest.fixture
def pipeline(handler):
    artifact = load_definition_json(ARTIFACT.read_text(encoding="utf-8"))
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 4.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=handler._db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([artifact_ref_for(artifact)]),
    )
    # The principal the daemon dispatches bus events under
    # (``src/playbooks/runtime.py``), so the commands run exactly as live.
    principal = ExecutionPrincipal.service("playbook-dispatch")
    set_handler_provider(lambda: handler)
    try:
        yield engine, runs, principal
    finally:
        set_handler_provider(None)


async def _propose(handler) -> str:
    await handler.execute("create_project", {"id": PROJECT, "name": PROJECT})
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": PROJECT,
            "source": "spec:intake",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    assert proposal["success"] is True, proposal
    return proposal["proposal_id"]


async def _resolved_gate(handler, await_id: str, resolution: str) -> dict:
    gate = await handler.execute(
        "gate_create",
        {
            "project_id": PROJECT,
            "gate_type": "human",
            "title": "Approve task batch?",
            "await_id": await_id,
        },
    )
    assert gate["success"] is True, gate
    await handler._db.resolve_gate(gate["gate_id"], resolved_by="human:test", resolution=resolution)
    return await handler._db.get_gate(gate["gate_id"])


def _resolved_event(gate: dict, event_id: str, **overrides) -> dict:
    """The ``gate.resolved`` payload ``Orchestrator._resolve_gate_and_emit`` emits."""
    event = {
        "event_type": "gate.resolved",
        "event_id": event_id,
        "gate_id": gate["id"],
        "project_id": gate["project_id"],
        "resolved_by": gate["resolved_by"],
        "resolution": gate["resolution"],
        "unblocked_task_ids": [],
        "gate_type": gate["gate_type"],
        "await_id": gate["await_id"],
    }
    event.update(overrides)
    return event


def _lifecycles(runs, result) -> list[RunLifecycle]:
    return [runs.snapshots[run_id].lifecycle for run_id in result.run_ids]


@pytest.mark.parametrize("resolution", ["rejected", "reject", "Yes, approve it", ""])
async def test_a_decision_that_is_not_an_approval_dispatches_nothing(
    handler, pipeline, resolution
):
    engine, _runs, principal = pipeline
    proposal_id = await _propose(handler)
    gate = await _resolved_gate(handler, proposal_id, resolution)

    result = await engine.dispatch_event(_resolved_event(gate, "rejected-1"), principal)

    assert result.rules_selected == ()
    assert await handler._db.list_tasks(project_id=PROJECT) == []
    assert (await get_proposal(handler._db, proposal_id))["status"] == "ready"


async def test_an_expired_gate_dispatches_nothing(handler, pipeline):
    engine, _runs, principal = pipeline
    proposal_id = await _propose(handler)

    result = await engine.dispatch_event(
        {"event_type": "gate.expired", "event_id": "expired-1", "gate_id": "gate-x",
         "project_id": PROJECT, "gate_type": "human"},
        principal,
    )

    assert result.rules_selected == ()
    assert (await get_proposal(handler._db, proposal_id))["status"] == "ready"


async def test_a_redelivered_approval_commits_exactly_one_graph(handler, pipeline):
    engine, runs, principal = pipeline
    proposal_id = await _propose(handler)
    gate = await _resolved_gate(handler, proposal_id, "approved")

    first = await engine.dispatch_event(_resolved_event(gate, "approved-1"), principal)
    tasks_after_first = {t.id for t in await handler._db.list_tasks(project_id=PROJECT)}
    replay = await engine.dispatch_event(_resolved_event(gate, "approved-2"), principal)

    assert first.rules_selected == ("commit-on-gate-resolve",)
    assert _lifecycles(runs, first) == [RunLifecycle.COMPLETED]
    assert _lifecycles(runs, replay) == [RunLifecycle.COMPLETED]
    assert len(tasks_after_first) == 1
    assert {t.id for t in await handler._db.list_tasks(project_id=PROJECT)} == tasks_after_first
    assert (await get_proposal(handler._db, proposal_id))["status"] == "committed"


async def test_an_event_that_misreports_its_gate_fails_at_the_command(handler, pipeline):
    """The filter trusts the event; the command trusts only the gate row."""
    engine, runs, principal = pipeline
    proposal_id = await _propose(handler)
    gate = await _resolved_gate(handler, proposal_id, "rejected")

    result = await engine.dispatch_event(
        _resolved_event(gate, "forged-1", resolution="approved"), principal
    )

    assert result.rules_selected == ("commit-on-gate-resolve",)
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]
    assert await handler._db.list_tasks(project_id=PROJECT) == []
    assert (await get_proposal(handler._db, proposal_id))["status"] == "ready"


async def test_approving_an_unrelated_human_gate_fails_visibly(handler, pipeline):
    engine, runs, principal = pipeline
    await _propose(handler)
    gate = await _resolved_gate(handler, "prop-not-a-proposal", "approve")

    result = await engine.dispatch_event(_resolved_event(gate, "unrelated-1"), principal)

    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]
    assert await handler._db.list_tasks(project_id=PROJECT) == []


async def test_an_approval_without_proposal_identity_never_invokes_the_commit(handler, pipeline):
    engine, _runs, principal = pipeline
    await _propose(handler)
    gate = await _resolved_gate(handler, "prop-anything", "approved")

    result = await engine.dispatch_event(
        _resolved_event(gate, "no-await-1", await_id=None), principal
    )

    assert result.rules_selected == ()
    assert await handler._db.list_tasks(project_id=PROJECT) == []


async def test_a_refused_ensure_task_ends_the_spec_ingest_run_failed(handler, pipeline):
    """No project row: ``ensure_task`` is refused, and the run must say so."""
    engine, runs, principal = pipeline

    result = await engine.dispatch_event(
        {"event_type": "spec.approved", "event_id": "spec-1", "project_id": PROJECT,
         "spec_path": "projects/p1/specs/x.md"},
        principal,
    )

    assert result.rules_selected == ("spec-ingest-on-approve",)
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]


async def test_a_refused_gate_create_ends_the_proposal_run_failed(handler, pipeline):
    engine, runs, principal = pipeline

    result = await engine.dispatch_event(
        {"event_type": "proposal.ready", "event_id": "ready-1", "project_id": PROJECT,
         "proposal_id": "prop-orphan"},
        principal,
    )

    assert result.rules_selected == ("proposal-ready-gate",)
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]
