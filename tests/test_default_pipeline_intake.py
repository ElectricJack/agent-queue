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
from unittest.mock import AsyncMock

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal
from src.database.queries.proposal_queries import get_proposal
from src.models import AgentProfile, TaskStatus
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle
from src.reviews.service import ReviewHooks, ReviewService
from tests._private_values_helper import private_value_service
from tests.test_review_feedback import accept, prepared
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
            # The daemon wires protected result storage; ``release-on-receipt``
            # saves its result to route on the refusal's ``error_code``.
            private_values=private_value_service(handler.db),
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


async def test_a_refused_ensure_spec_ingest_ends_the_spec_ingest_run_failed(handler, pipeline):
    """No project row: ``ensure_spec_ingest`` is refused, and the run must say so."""
    engine, runs, principal = pipeline

    result = await engine.dispatch_event(
        {"event_type": "spec.approved", "event_id": "spec-1", "project_id": PROJECT,
         "spec_path": "projects/p1/specs/x.md", "decision_id": "missing",
         "packet_sha256": "0" * 64, "review_id": None, "revision": None, "spec_kind": "design"},
        principal,
    )

    assert result.rules_selected == ("spec-ingest-on-approve",)
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]


@pytest.mark.parametrize("kind", ["spec", "plan"])
@pytest.mark.parametrize("lifecycle", [
    "active", "completed", "failed", "obsolete", "archived-completed", "archived-obsolete",
])
async def test_review_approval_ingests_once_at_deep_high(handler, pipeline, kind, lifecycle):
    from src.vault import ensure_default_intelligence_classes

    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    engine, runs, principal = pipeline
    await handler.execute("create_project", {"id": PROJECT, "name": PROJECT})
    await handler.db.create_profile(AgentProfile(
        id="spec-ingest", name="Spec Ingest", harness="codex", default_class="deep-high",
        needs_workspace=False,
    ))

    async def resolve_gate(gate_id, resolved_by, resolution):
        return await handler.db.resolve_gate(
            gate_id, resolved_by=resolved_by, resolution=resolution,
        )

    svc = ReviewService(handler.db, handler.config.vault_root, ReviewHooks(
        emit=handler.orchestrator.bus.emit,
        resolve_gate=resolve_gate,
        changes_requested=AsyncMock(),
    ))
    submitted = await svc.submit(
        project_id=PROJECT, author_task_id=None, kind=kind, title="Approved specification",
        content="---\nspec_kind: implementation\n---\n# Specification\n",
        submitted_by="human:local-operator",
    )
    args, preview = await prepared(
        handler, submitted["review_id"], note="Approved",
        dispositions=[{"feedback_key": "overall_note", "disposition": "no_change",
                       "reason": "Approval acknowledgment; no implementation change requested."}],
    )
    approved = await accept(handler, args, preview)
    assert approved["success"], approved
    assert approved["application_state"] == "applied", approved
    payload = next(call.args[1] for call in handler.orchestrator.bus.emit.call_args_list
                   if call.args[0] == "spec.approved")
    path = Path(payload["spec_path"])
    assert path.is_absolute()
    assert "status: approved" in path.read_text()
    result = await engine.dispatch_event(
        {"event_type": "spec.approved", "event_id": "review-approval-1", **payload}, principal,
    )
    assert _lifecycles(runs, result) == [RunLifecycle.COMPLETED]
    [task] = await handler.db.list_tasks(project_id=PROJECT)
    assert task.profile_id == "spec-ingest"
    assert task.intelligence_class == "deep-high"
    assert task.dedup_key == f"spec-ingest:decision:{payload['decision_id']}"
    review = await handler.db.get_review(submitted["review_id"])
    assert f"spec_path: {review['vault_path']}" in task.description
    assert "spec_kind" in task.description
    # The executable recording must deliver the policy, not just its source Markdown.
    assert "docs/guides/reviews.md" in task.description
    assert "every predecessor goal as kept, changed or dropped" in task.description
    assert "Jack's explicit sign-off for the goal and replacement revision" in task.description
    assert "do not commit affected work" in task.description

    # A supervisor settling an unnecessary ingest must not commission another
    # task when approval is delivered again under a fresh event identity.
    if "obsolete" in lifecycle:
        closed = await handler.execute("task_close", {
            "task_id": task.id, "obsolete": True, "reason": "Spec already ingested manually",
        })
        assert closed["success"], closed
        assert (await handler.db.get_task(task.id)).status == TaskStatus.COMPLETED
    elif lifecycle != "active":
        status = TaskStatus.FAILED if lifecycle == "failed" else TaskStatus.COMPLETED
        await handler.db.update_task(task.id, status=status)
    archived = lifecycle.startswith("archived-")
    if archived:
        assert await handler.db.archive_task(task.id)
    replay = await engine.dispatch_event(
        {"event_type": "spec.approved", "event_id": "review-approval-replay", **payload},
        principal,
    )
    assert _lifecycles(runs, replay) == [RunLifecycle.COMPLETED]
    assert {t.id for t in await handler.db.list_tasks(project_id=PROJECT)} == (
        set() if archived else {task.id}
    )
    ingest_args = {
        key: payload[key] for key in ("project_id", "decision_id", "packet_sha256")
    }
    # Receipt reuse still requires the exact approved identity, including
    # after the task row has left the active table.
    for override in ({"packet_sha256": "0" * 64}, {"decision_id": "other"},
                     {"project_id": "other"}):
        refused = await handler.execute("ensure_spec_ingest", {**ingest_args, **override})
        assert not refused["success"], refused
        assert refused["code"] == "ask.not_found", refused
    reused = await handler.execute("ensure_spec_ingest", ingest_args)
    assert reused == {"success": True, "task_id": task.id, "created": False}


async def test_a_refused_gate_create_ends_the_proposal_run_failed(handler, pipeline):
    engine, runs, principal = pipeline

    result = await engine.dispatch_event(
        {"event_type": "proposal.ready", "event_id": "ready-1", "project_id": PROJECT,
         "proposal_id": "prop-orphan"},
        principal,
    )

    assert result.rules_selected == ("proposal-ready-gate",)
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]


async def test_dispatch_retries_failed_run_until_transactional_task_receipt(handler, pipeline):
    import asyncio
    from sqlalchemy import update
    from src.asks.operations import get_operation
    from src.database.tables import escalation_actions
    from src.vault import ensure_default_intelligence_classes

    engine, runs, principal = pipeline
    await handler.execute("create_project", {"id": PROJECT, "name": PROJECT})
    path = Path(handler.config.vault_root) / f"projects/{PROJECT}/specs/retry.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nstatus: draft\nspec_kind: implementation\n---\n# Exact approved body")
    approved = await handler.execute("spec_approve", {"project_id": PROJECT, "spec_path": str(path)})
    assert approved["success"], approved
    payload = next(c.args[1] for c in handler.orchestrator.bus.emit.call_args_list
                   if c.args[0] == "spec.approved")
    # The role profile is unavailable: a delivered event and a terminal failed
    # pipeline run cannot be mistaken for its task receipt.
    first = await engine.dispatch_event({"event_type": "spec.approved",
                                         "event_id": str(payload["seq"]), **payload}, principal)
    assert _lifecycles(runs, first) == [RunLifecycle.FAILED]
    async with handler.db.immediate() as conn:
        op = await get_operation(conn, approved["decision_id"], "ingestion")
        assert op["status"] == "pending" and "task_id" not in op["receipt"]
        await conn.execute(update(escalation_actions).where(escalation_actions.c.id == op["id"])
                           .values(retry_at=0))
    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    await handler.db.create_profile(AgentProfile(
        id="spec-ingest", name="Ingest", harness="codex", default_class="deep-high",
    ))
    path.unlink()  # The sweep must not reread the mirror.
    retry = await handler.execute("spec_ingest_reconcile", {})
    assert retry["success"] and retry["errors"] == [], retry
    payload2 = [c.args[1] for c in handler.orchestrator.bus.emit.call_args_list
                if c.args[0] == "spec.approved"][-1]
    assert payload2["seq"] != payload["seq"]
    second = await engine.dispatch_event({"event_type": "spec.approved",
                                          "event_id": str(payload2["seq"]), **payload2}, principal)
    assert _lifecycles(runs, second) == [RunLifecycle.COMPLETED]
    args = {key: payload[key] for key in ("project_id", "decision_id", "packet_sha256")}
    results = await asyncio.gather(*(handler.execute("ensure_spec_ingest", args) for _ in range(2)))
    assert all(r["success"] and not r["created"] for r in results), results
    assert len({r["task_id"] for r in results}) == 1
    assert len(await handler.db.list_tasks(project_id=PROJECT)) == 1
    async with handler.db._engine.connect() as conn:
        receipt = await get_operation(conn, approved["decision_id"], "ingestion")
        assert receipt["status"] == "succeeded" and receipt["receipt"]["task_id"] == results[0]["task_id"]


# Spec-ingestion held batches, L5.2: the pipeline assigns the one fidelity
# reviewer and releases on a passing receipt. Batch events are dispatched as
# the integration outbox delivers them (``src/playbooks/runtime.py``).
OUTBOX = ExecutionPrincipal.service("integration-outbox")


async def _outbox_events(handler, event_type: str) -> list[dict]:
    from sqlalchemy import select

    from src.database.tables import integration_outbox

    async with handler.db.immediate() as conn:
        rows = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == event_type,
        ))).mappings().all()
    return [{"event_type": event_type, "event_id": row["id"], **dict(row["payload"])}
            for row in rows]


def _receipt_event(batch: dict, event_id: str) -> dict:
    return {"event_type": "batch.receipt_recorded", "event_id": event_id, "project_id": PROJECT,
            "batch_id": batch["batch_id"], "structure_revision": batch["structure_revision"],
            "structure_digest": batch["structure_digest"], "reviewer_task_id": None}


async def test_held_batch_gets_exactly_one_fidelity_reviewer(handler, pipeline):
    from tests.test_fidelity_review import _batch_row, _held_batch, _holds

    engine, runs, _ = pipeline
    batch, _ = await _held_batch(handler)
    [held] = await _outbox_events(handler, "batch.held")
    assert held["batch_id"] == batch["batch_id"]

    first = await engine.dispatch_event(held, OUTBOX)
    assert first.rules_selected == ("fidelity-review-on-held",)
    assert _lifecycles(runs, first) == [RunLifecycle.COMPLETED]
    reviewer_id = (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"]
    reviewer = await handler.db.get_task(reviewer_id)
    assert reviewer.profile_id == "fidelity-reviewer"

    replay = await engine.dispatch_event({**held, "event_id": "held-replay"}, OUTBOX)
    assert _lifecycles(runs, replay) == [RunLifecycle.COMPLETED]
    assert (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"] == reviewer_id
    assert [t.id for t in await handler.db.list_tasks(project_id=PROJECT)
            if t.profile_id == "fidelity-reviewer"] == [reviewer_id]
    # A reviewer never releases anything.
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}


async def test_passing_receipt_releases_the_held_batch_once(handler, pipeline):
    from tests.test_task_batch_commands import _live_batch, _release_ready

    engine, runs, _ = pipeline
    batch, _, _ = await _release_ready(handler)
    [recorded] = await _outbox_events(handler, "batch.receipt_recorded")

    first = await engine.dispatch_event(recorded, OUTBOX)
    assert first.rules_selected == ("release-on-receipt",)
    assert _lifecycles(runs, first) == [RunLifecycle.COMPLETED]
    row = await _live_batch(handler, batch)
    assert row["state"] == "released" and row["release_receipt"]
    assert not await handler.db.ingest_held_task_ids(set(batch["task_ids"]))

    replay = await engine.dispatch_event({**recorded, "event_id": "receipt-replay"}, OUTBOX)
    assert _lifecycles(runs, replay) == [RunLifecycle.COMPLETED]
    assert (await _live_batch(handler, batch))["release_receipt"] == row["release_receipt"]


async def test_a_stale_receipt_fails_the_release_run_and_keeps_the_hold(handler, pipeline):
    from tests.test_task_batch_commands import _release_ready

    engine, runs, _ = pipeline
    batch, _, _ = await _release_ready(handler)
    result = await engine.dispatch_event(
        {**_receipt_event(batch, "stale-receipt"), "structure_digest": "stale"}, OUTBOX,
    )
    assert _lifecycles(runs, result) == [RunLifecycle.FAILED]
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])


async def test_needs_scope_does_not_release(handler, pipeline):
    """Scenario 5: a scope question holds the tree on its one ask; never a release."""
    from sqlalchemy import update

    from src.database.tables import ingest_batch_receipts
    from tests.test_fidelity_review import _batch_row, _holds, _record, _review_request

    engine, runs, _ = pipeline
    batch, scope, args = await _review_request(handler)
    args["walkthrough_verdict"] = "scope_question"
    args["structural_findings"] = [{"question": "Keep the source constraint?", "section": "core"}]
    recorded = await _record(handler, scope, args)
    assert recorded["success"] and recorded["state"] == "needs_scope", recorded
    assert await _outbox_events(handler, "batch.receipt_recorded") == []
    [needs_scope] = await _outbox_events(handler, "batch.needs_scope")

    # The scope notification dispatches no rule at all.
    result = await engine.dispatch_event(needs_scope, OUTBOX)
    assert result.rules_selected == ()

    # A receipt wakeup racing the open question (a replay, or a receipt the
    # question did not replace) reaches release, which refuses `needs_scope`:
    # the run ends completed and the batch stays held on its ask.
    async with handler.db.immediate() as conn:
        await conn.execute(update(ingest_batch_receipts).values(
            passing=True, walkthrough_verdict="faithful",
        ))
    wakeup = await engine.dispatch_event(_receipt_event(batch, "receipt-during-scope"), OUTBOX)
    assert wakeup.rules_selected == ("release-on-receipt",)
    assert _lifecycles(runs, wakeup) == [RunLifecycle.COMPLETED]
    row = await _batch_row(handler, batch["batch_id"])
    assert row["state"] == "needs_scope" and row["release_receipt"] is None
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}


async def test_design_approval_no_reviewer(handler, pipeline):
    """Scenario 10: a design batch is released with the authoring leaf and no reviewer."""
    from tests.test_fidelity_review import _batch_row
    from tests.test_task_batch_commands import _ingest_assignment, _ingest_propose

    engine, _runs, _ = pipeline
    path, scope, decision = await _ingest_assignment(handler, "design")
    result = await _ingest_propose(handler, path, scope, decision)
    assert result["status"] == "released"
    assert result["release_receipt"] == {"kind": "design_authoring_shape"}
    assert (await _batch_row(handler, result["batch_id"]))["reviewer_task_id"] is None
    for event_type in ("batch.held", "batch.receipt_recorded", "batch.needs_scope"):
        assert await _outbox_events(handler, event_type) == []
    # Whatever batch notification the design batch did enqueue selects no rule.
    for event_type in ("batch.released", "task.change_set_committed"):
        for event in await _outbox_events(handler, event_type):
            assert (await engine.dispatch_event(event, OUTBOX)).rules_selected == ()
    tasks = await handler.db.list_tasks(project_id=PROJECT)
    assert len(tasks) == 3  # the ingest task, the epic and its one authoring child
    assert not [t for t in tasks if t.profile_id == "fidelity-reviewer"]
    assert not await handler.db.ingest_held_task_ids({t.id for t in tasks})
