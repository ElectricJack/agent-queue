"""Restart and idempotency boundaries — Package 4 child plan T-15.

This file is created by the wait + loop slice and covers **two** of T-15's
five parameterisations: the wait boundary and the loop boundary.  The command,
LLM and agent-task boundaries, the ``multiprocessing`` process-kill
integration cases and T-10's operator-resolution suite land with the dry-run
and restart task that follows this one; the two here are in-process, because
the property they assert is about *durable state*, not about process death:

    build the snapshot a crash leaves behind, construct a **fresh**
    ``PlaybookEngine`` against the same repository, resume, and assert that
    (a) no acknowledged attempt is duplicated, (b) bindings are intact, and
    (c) the run still reaches its terminal.

A fresh engine is the right stand-in for a fresh process here precisely
because the engine holds no per-run state: everything a restart needs is on
the snapshot, and a test that could only be written with a real fork would be
saying the opposite.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import (
    EventArrived,
    HumanDecision,
    PlaybookEngine,
)
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.executors.foreach import collection_digest
from src.playbooks.executors.wait import wait_id_for
from src.playbooks.run_state import LoopFrame, RunLifecycle, RunSnapshot
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    LIST_TASKS,
    registry_with,
)
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
    event,
    load_artifact,
)
from tests.test_v2_engine import (
    RecordingWaitRepository,
    WaitAwareRepository,
    downstream,
    ok,
)


def fresh_engine(
    artifact_name: str,
    *,
    runs: Any,
    waits: Any = None,
    adapter: Any = None,
) -> tuple[PlaybookEngine, Any, Any]:
    """A brand-new engine over an existing repository — the "restart".

    Nothing is carried across but the repository, so anything the resumed run
    knows it knows from its snapshot.
    """
    artifact = load_artifact(artifact_name)
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS, adapter=adapter)
    store = InMemoryArtifactStore()
    store.put(artifact)
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: 2_000.0,
            artifact_store=store,
            bus=RecordingBus(),
        ),
        runs=runs,
        waits=waits,
        activations=StubActivations([ref]),
    )
    return engine, adapter, ref


class TestRestartAtTheWaitBoundary:
    @pytest.mark.asyncio
    async def test_restart_before_the_wait_boundary_re_registers_the_wait_once(self):
        """A crash *before* the suspension commits loses the attempt, not the run.

        The boundary is the only durable write, so nothing was registered and
        nothing was receipted.  The replay is therefore the same attempt of
        the same step, and it computes the same wait id — which means a
        registration that *had* landed would collide on the primary key
        rather than open a second wait for one suspension.
        """
        waits = RecordingWaitRepository()
        runs = WaitAwareRepository(waits)
        engine, _adapter, ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        outcome = await engine.run_rule(
            ref, "correlate", event("spec-approved"), TRUSTED_LOCAL, pause_before_start=True
        )
        # The crash: the run row exists at its entry step and nothing else.
        assert runs.receipts == []
        assert waits.registered == []

        restarted, _adapter, _ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        resumed = await restarted.resume(
            outcome.run_id, EventArrived(event_id="e", payload={}), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.PAUSED
        assert len(waits.registered) == 1
        assert waits.registered[0].wait_id == wait_id_for(
            outcome.run_id, "await-review", -1, 1
        )

        # Replaying the *same* pre-boundary state a second time computes the
        # same id: the wait is a function of the attempt that opens it.
        again = wait_id_for(outcome.run_id, "await-review", -1, 1)
        assert again == waits.registered[0].wait_id

    @pytest.mark.asyncio
    async def test_a_restarted_engine_resumes_a_paused_wait_from_the_snapshot(self):
        waits = RecordingWaitRepository()
        runs = WaitAwareRepository(waits)
        engine, _adapter, ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        outcome = await engine.run_rule(
            ref, "gate", event("task-completed-code"), TRUSTED_LOCAL
        )
        assert outcome.lifecycle is RunLifecycle.PAUSED
        before = len(runs.receipts)

        restarted, _adapter, _ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        resumed = await restarted.resume(
            outcome.run_id, HumanDecision(decision="approve"), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert resumed.snapshot.current_step_id == "gate-done"
        assert resumed.snapshot.bindings["approval"]["resolution"] == "approve"
        # Exactly one resume receipt, and it is a *new* attempt of the same
        # step — the suspension's own receipt is not rewritten.
        gate = [r for r in runs.receipts if r.step_id == "await-approval"]
        assert [r.attempt for r in gate] == [1, 2]
        assert len(runs.receipts) > before

    @pytest.mark.asyncio
    async def test_a_restarted_engine_does_not_resume_a_wait_twice(self):
        waits = RecordingWaitRepository()
        runs = WaitAwareRepository(waits)
        engine, _adapter, ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        outcome = await engine.run_rule(
            ref, "gate", event("task-completed-code"), TRUSTED_LOCAL
        )
        await engine.resume(
            outcome.run_id, HumanDecision(decision="approve"), TRUSTED_LOCAL
        )
        settled = len(runs.receipts)

        restarted, _adapter, _ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs, waits=waits
        )
        again = await restarted.resume(
            outcome.run_id, HumanDecision(decision="approve"), TRUSTED_LOCAL
        )
        assert again.outcome == "already_terminal"
        assert len(runs.receipts) == settled


def crashed_mid_loop(ref, *, index: int, items: list[str]) -> RunSnapshot:
    """The snapshot a crash *inside* iteration ``index`` leaves behind.

    Hand-written rather than captured (child plan §6.4), so the test asserts
    against a *stated* expectation of what a crash looks like instead of
    against whatever the implementation happened to write: entered iteration
    ``index``, left it un-left, and an aggregate holding exactly the
    iterations that finished.
    """
    collection = [{"id": task_id} for task_id in items]
    attempts = {"list-downstream:-1": 1, "for-each-task:-1": 1}
    partial = []
    for done in range(index):
        attempts[f"open-gate:{done}"] = 1
        attempts[f"for-each-task:{done}"] = 1
        partial.append(
            {"index": done, "outcome": "created", "value": None, "error": None}
        )
    return RunSnapshot(
        run_id="run-mid-loop",
        playbook_id="sequential-loop",
        artifact_sha256=ref.artifact_sha256,
        rule_id="sweep",
        lifecycle=RunLifecycle.RUNNING,
        version=1 + 2 * index,
        current_step_id="open-gate",
        event=event("spec-approved"),
        context={"dispatch_id": "d-1", "playbook_id": "sequential-loop", "rule_id": "sweep"},
        bindings={
            "downstream": {"tasks": collection, "count": len(collection)},
            "gate": {"task_id": f"t-{index}", "created": True},
        },
        attempts=attempts,
        loop=LoopFrame(
            step_id="for-each-task",
            item_binding="task",
            collection_digest=collection_digest(collection),
            index=index,
            total=len(collection),
            partial=tuple(partial),
        ),
        event_type="spec.approved",
        event_id="evt-spec-approved",
        dispatch_id="d-1",
        started_at=1_000.0,
        updated_at=1_000.0,
    )


class TestRestartMidLoop:
    @pytest.mark.asyncio
    async def test_restart_mid_loop_resumes_the_same_iteration(self):
        """A crash inside iteration *n* restarts iteration *n*, never *n+1*.

        The frame is committed on both sides of every body transition, so the
        durable state names an iteration that has been entered and not yet
        left.  Skipping to *n+1* would silently drop one item's work and still
        report a complete aggregate.
        """
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        snapshot = crashed_mid_loop(ref, index=1, items=["d-1", "d-2", "d-3"])
        await runs.create_run(snapshot)

        adapter.queue.extend([ok("t-2"), ok("t-3")])
        resumed = await engine.resume(
            snapshot.run_id, EventArrived(event_id="restart", payload={}), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        # Iteration 1 ran again, and iteration 0 did not.
        assert [args.title for args in adapter.args_for("ensure_task")] == [
            "Gate: d-2",
            "Gate: d-3",
        ]
        result = resumed.snapshot.bindings["sweep_result"]
        assert result["total"] == 3
        assert [item["index"] for item in result["items"]] == [0, 1, 2]
        assert result["succeeded"] == 3

    @pytest.mark.asyncio
    async def test_the_interrupted_attempt_is_lost_and_the_run_is_not(self):
        """Nothing between the executor call and the boundary is durable.

        So the replay is the *same* attempt number of the same step: the
        crashed attempt left no receipt to collide with, and the four-part
        idempotency key a keyed command receives is therefore unchanged.
        """
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        snapshot = crashed_mid_loop(ref, index=1, items=["d-1", "d-2"])
        await runs.create_run(snapshot)
        adapter.queue.append(ok("t-2"))
        await engine.resume(
            snapshot.run_id, EventArrived(event_id="restart", payload={}), TRUSTED_LOCAL
        )
        replayed = next(
            r for r in runs.receipts if r.step_id == "open-gate" and r.iteration == 1
        )
        assert replayed.attempt == 1
        assert replayed.idempotency_key == f"{snapshot.run_id}:open-gate:1:1"
        keys = [(r.step_id, r.iteration, r.attempt) for r in runs.receipts]
        assert len(set(keys)) == len(keys)

    @pytest.mark.asyncio
    async def test_the_loop_item_is_re_resolved_from_the_pinned_collection(self):
        """A restarted engine holds no per-run state; the item comes back from
        the snapshot's own binding, pinned by the frame's digest."""
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        snapshot = crashed_mid_loop(ref, index=2, items=["d-1", "d-2", "d-3"])
        await runs.create_run(snapshot)
        adapter.queue.append(ok("t-3"))
        resumed = await engine.resume(
            snapshot.run_id, EventArrived(event_id="restart", payload={}), TRUSTED_LOCAL
        )
        assert [args.title for args in adapter.args_for("ensure_task")] == ["Gate: d-3"]
        assert resumed.lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_bindings_survive_the_restart(self):
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        adapter.queue.extend([downstream("d-1", "d-2"), ok("t-1"), ok("t-2")])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.bindings["downstream"]["count"] == 2

        restarted, _adapter, _ref = fresh_engine(
            "sequential-loop.artifact.json", runs=runs
        )
        reloaded = await restarted.runs.load_run(outcome.run_id)
        assert reloaded.bindings["downstream"] == outcome.snapshot.bindings["downstream"]
        assert reloaded.bindings["sweep_result"]["succeeded"] == 2


class TestReceiptTrail:
    @pytest.mark.asyncio
    async def test_receipts_identify_every_traversed_node_iteration_and_artifact(self):
        """What Package 5's overlay depends on: the trail reconstructs the path."""
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        adapter.queue.extend([downstream("d-1", "d-2"), ok("t-1"), ok("t-2")])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        trail = [(r.step_id, r.iteration, r.attempt) for r in runs.receipts]
        assert trail == [
            ("list-downstream", -1, 1),
            ("for-each-task", -1, 1),
            ("open-gate", 0, 1),
            ("for-each-task", 0, 1),
            ("open-gate", 1, 1),
            ("for-each-task", 1, 1),
            ("sweep-done", -1, 1),
        ]
        assert {r.artifact_sha256 for r in runs.receipts} == {ref.artifact_sha256}
        assert all(r.idempotency_key.startswith(outcome.run_id) for r in runs.receipts)


class TestRestartIsModePreserving:
    @pytest.mark.asyncio
    async def test_a_resumed_run_keeps_the_mode_it_started_in(self):
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        adapter.queue.extend([downstream("d-1"), ok()])
        outcome = await engine.run_rule(
            ref, "sweep", event("spec-approved"), TRUSTED_LOCAL, pause_before_start=True
        )
        assert ExecutionMode(runs.snapshots[outcome.run_id].mode) is ExecutionMode.LIVE
