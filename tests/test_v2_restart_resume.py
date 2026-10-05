"""Restart recovery and idempotency boundaries — Package 4 child plan T-15.

PostgreSQL tests exercise automatic recovery of durable rows left by a prior
daemon, including replay safety, driver ownership, bounded scans and diagnosis.
Isolated repository and process-kill tests cover each executor boundary and
operator resolution. A fresh engine retains only the durable snapshot; its
driver registry describes ownership in the current process.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import multiprocessing
import os
import signal
from typing import Any
from uuid import uuid4

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.commands.principal import PrincipalKind
from src.playbooks.engine import (
    EVENT_RUN_FINISHED,
    ChildTaskCompleted,
    EventArrived,
    HumanDecision,
    InterruptedByRestart,
    OperatorResolution,
    PlaybookEngine,
    TimerFired,
    WaitScheduler,
)
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.executors.foreach import collection_digest
from src.playbooks.executors.wait import wait_id_for
from src.playbooks.run_state import LoopFrame, RunLifecycle, RunSnapshot
from src.playbooks.receipts import StepReceipt
from src.playbooks.waits import WaitSpec
from src.playbooks.recovery import RestartReconciler
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    LIST_TASKS,
    registry_with,
)
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    SQLiteRunRepository,
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


def _persist_then_sigkill(database_path: str, snapshot: RunSnapshot) -> None:
    """A real process dies after the named durable boundary has landed."""
    asyncio.run(SQLiteRunRepository(database_path).create_run(snapshot))
    os.kill(os.getpid(), signal.SIGKILL)


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


@pytest.fixture
async def restart_db():
    from src.database import Database
    from tests.db_fixtures import lease_dsn

    database = Database(lease_dsn("v2_restart_recovery"))
    await database.initialize()
    yield database
    await database.close()


async def recovery_engine(database, artifact_name="two-rules-one-event.artifact.json", adapter=None):
    from tests.test_child_task_reconciler import seed_artifact

    engine, adapter, ref = fresh_engine(artifact_name, runs=database, waits=database, adapter=adapter)
    await seed_artifact(database, ref)
    return engine, adapter, ref


def orphan_snapshot(ref, **overrides):
    fields = {
        "run_id": "orphan",
        "playbook_id": ref.playbook_id,
        "artifact_sha256": ref.artifact_sha256,
        "rule_id": "review",
        "current_step_id": "ensure-review-task",
        "event": event("task-completed-code"),
        "started_at": 1_000.0,
        "updated_at": 1_000.0,
    }
    fields.update(overrides)
    return RunSnapshot(**fields)


async def drain_recovery(recovery):
    # Join only already-scheduled work; tests never construct a resume cause.
    await asyncio.gather(*tuple(recovery._tasks.values()))
    await asyncio.sleep(0)


async def test_restart_sweep_replays_a_fenced_keyed_command_once(restart_db):
    engine, adapter, ref = await recovery_engine(restart_db)
    snapshot = await restart_db.create_run(orphan_snapshot(ref))
    fence = replace(
        interrupted_attempt(snapshot, step_id="ensure-review-task"),
        snapshot_version=snapshot.version + 1,
    )
    await restart_db.commit_boundary(snapshot, fence)
    adapter.queue.append(ok("review-1"))
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)

    assert await recovery.tick() == (snapshot.run_id,)
    await drain_recovery(recovery)
    stored = await restart_db.load_run(snapshot.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    assert stored.bindings["review"]["task_id"] == "review-1"
    assert stored.context["resume_cause"] == {
        "kind": "interrupted_by_restart", "process_started_at": 1_500,
    }
    receipts = await restart_db.list_receipts(snapshot.run_id)
    assert any(row.receipt_kind == "interrupted" for row in receipts)
    command = [row for row in receipts if row.step_id == "ensure-review-task"]
    assert {row.idempotency_key for row in command} == {"orphan:ensure-review-task:-:1"}
    assert await recovery.tick() == ()
    assert len(adapter.calls) == 1
    assert len(await restart_db.list_receipts(snapshot.run_id)) == len(receipts)
    await recovery.shutdown()


@pytest.mark.parametrize("kind", ["llm", "agent_task", "unsafe_command"])
async def test_restart_sweep_preserves_ambiguous_external_effects(restart_db, kind):
    from src.commands.contracts.models import IdempotencySpec
    from src.playbooks.definition import PlaybookDefinition
    from tests.test_agent_task_executor import agent_task_artifact
    from tests.test_child_task_reconciler import seed_artifact

    if kind == "agent_task":
        artifact = agent_task_artifact()
        ref = artifact_ref_for(artifact)
        await seed_artifact(restart_db, ref)
        engine, adapter, _ = fresh_engine("two-rules-one-event.artifact.json", runs=restart_db)
        engine.services.artifact_store.put(artifact)
        engine.activations = StubActivations([ref])
        step_id, rule_id = "delegate", "r"
    else:
        artifact_name = (
            "review-pipeline.artifact.json" if kind == "llm"
            else "two-rules-one-event.artifact.json"
        )
        engine, adapter, ref = await recovery_engine(restart_db, artifact_name)
        step_id = "classify-risk" if kind == "llm" else "ensure-review-task"
        rule_id = "review-on-task-completed" if kind == "llm" else "review"
        if kind == "unsafe_command":
            unsafe = ENSURE_TASK.model_copy(update={
                "execution": ENSURE_TASK.execution.model_copy(update={
                    "retry_safe": False, "idempotency": IdempotencySpec(mode="none"),
                }),
            })
            engine.services = replace(engine.services, contracts=registry_with(unsafe)[0])
    assert isinstance(engine.services.artifact_store.load(ref.artifact_sha256), PlaybookDefinition)
    snapshot = orphan_snapshot(ref, current_step_id=step_id, rule_id=rule_id, context={
        "_in_flight_attempt": {
            "step_id": step_id, "step_kind": "command" if kind == "unsafe_command" else kind,
            "iteration": -1, "attempt": 1, "started_at": 1_000,
            "idempotency_key": f"orphan:{step_id}:-:1",
        },
    })
    await restart_db.create_run(snapshot)
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
    await recovery.tick()
    await drain_recovery(recovery)

    stored = await restart_db.load_run(snapshot.run_id)
    assert stored.lifecycle is RunLifecycle.PAUSED
    assert stored.operator_decision is not None
    assert stored.context["resume_cause"]["kind"] == "interrupted_by_restart"
    assert stored.bindings == {}
    assert not adapter.calls
    assert await recovery.tick() == ()
    await recovery.shutdown()


@pytest.mark.parametrize("boundary", ["wait", "loop", "decision", "terminal"])
async def test_restart_sweep_recovers_internal_boundaries(restart_db, boundary):
    if boundary == "wait":
        engine, adapter, ref = await recovery_engine(restart_db, "wait-kinds.artifact.json")
        snapshot = orphan_snapshot(ref, rule_id="correlate", current_step_id="await-review",
                                   event=event("spec-approved"))
    elif boundary == "loop":
        engine, adapter, ref = await recovery_engine(restart_db, "sequential-loop.artifact.json")
        snapshot = crashed_mid_loop(ref, index=1, items=["d-1", "d-2", "d-3"])
        adapter.queue.extend([ok("t-2"), ok("t-3")])
    else:
        engine, adapter, ref = await recovery_engine(restart_db)
        snapshot = orphan_snapshot(
            ref, rule_id="sweep" if boundary == "decision" else "review",
            current_step_id="check-empty" if boundary == "decision" else "review-done",
            bindings={"downstream": {"tasks": [], "count": 0}} if boundary == "decision" else {},
        )
    await restart_db.create_run(snapshot)
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
    await recovery.tick()
    await drain_recovery(recovery)
    stored = await restart_db.load_run(snapshot.run_id)
    if boundary == "wait":
        assert stored.lifecycle is RunLifecycle.PAUSED
        assert [row.wait_id for row in await restart_db.list_active(snapshot.run_id)] == [
            wait_id_for(snapshot.run_id, "await-review", -1, 1)
        ]
    else:
        assert stored.lifecycle is RunLifecycle.COMPLETED
    if boundary == "loop":
        assert [args.title for args in adapter.args_for("ensure_task")] == ["Gate: d-2", "Gate: d-3"]
        assert [item["index"] for item in stored.bindings["sweep_result"]["items"]] == [0, 1, 2]
    assert await recovery.tick() == ()
    await recovery.shutdown()


async def test_restart_sweep_settles_cancel_intent_without_replaying(restart_db):
    engine, adapter, ref = await recovery_engine(restart_db)
    snapshot = orphan_snapshot(ref, lifecycle=RunLifecycle.CANCELLING, cancel_requested_at=1_010)
    await restart_db.create_run(snapshot)
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
    await recovery.tick()
    await drain_recovery(recovery)
    stored = await restart_db.load_run(snapshot.run_id)
    assert stored.lifecycle is RunLifecycle.CANCELLED
    assert stored.context["resume_cause"]["kind"] == "interrupted_by_restart"
    assert not adapter.calls
    assert (await restart_db.list_receipts(snapshot.run_id))[-1].cancelled_at is not None
    await recovery.shutdown()


@pytest.mark.parametrize("lifecycle", [RunLifecycle.RUNNING, RunLifecycle.CANCELLING])
async def test_restart_sweep_advances_past_bad_artifacts_and_retries(restart_db, lifecycle):
    engine, adapter, ref = await recovery_engine(restart_db)
    broken = orphan_snapshot(ref, run_id="a-broken", lifecycle=lifecycle)
    healthy = orphan_snapshot(ref, run_id="b-healthy")
    await restart_db.create_run(broken)
    await restart_db.create_run(healthy)
    original = engine._ref_for

    async def unavailable(snapshot):
        if snapshot.run_id == broken.run_id:
            raise FileNotFoundError("artifact unavailable")
        return await original(snapshot)

    engine._ref_for = unavailable
    adapter.queue.append(ok("review-1"))
    recovery = RestartReconciler(
        engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500, concurrency=1,
    )
    assert await recovery.tick() == (broken.run_id,)
    await drain_recovery(recovery)
    assert await restart_db.load_run(broken.run_id) == broken
    assert await recovery.tick() == (healthy.run_id,)
    await drain_recovery(recovery)
    assert (await restart_db.load_run(healthy.run_id)).lifecycle is RunLifecycle.COMPLETED
    assert await recovery.tick() == ()  # Finish the keyset pass.
    assert await recovery.tick() == (broken.run_id,)  # Retry on the next pass.
    await drain_recovery(recovery)
    assert await restart_db.load_run(broken.run_id) == broken
    engine._ref_for = original
    adapter.queue.append(ok("review-retry"))
    assert await recovery.tick() == ()
    assert await recovery.tick() == (broken.run_id,)
    await drain_recovery(recovery)
    stored = await restart_db.load_run(broken.run_id)
    assert stored.lifecycle is (
        RunLifecycle.CANCELLED if lifecycle is RunLifecycle.CANCELLING else RunLifecycle.COMPLETED
    )
    await recovery.shutdown()


async def test_restart_resume_rechecks_candidates_and_does_not_wake_paused_runs(restart_db):
    engine, adapter, ref = await recovery_engine(restart_db)
    for lifecycle, stamp in [(RunLifecycle.PAUSED, 1_000), (RunLifecycle.RUNNING, 1_500)]:
        snapshot = orphan_snapshot(ref, run_id=lifecycle.value, lifecycle=lifecycle, updated_at=stamp)
        await restart_db.create_run(snapshot)
        outcome = await engine.resume(snapshot.run_id, InterruptedByRestart(1_500), TRUSTED_LOCAL)
        assert outcome.outcome == "restart_recovery_not_needed"
        assert await restart_db.load_run(snapshot.run_id) == snapshot
    assert not adapter.calls


async def test_a_current_driver_is_reserved_before_its_first_read(restart_db, monkeypatch):
    engine, adapter, ref = await recovery_engine(restart_db)
    snapshot = await restart_db.create_run(orphan_snapshot(ref))
    started, release = asyncio.Event(), asyncio.Event()
    original = restart_db.load_run
    reads = 0

    async def delayed_read(run_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            await release.wait()
        return await original(run_id)

    monkeypatch.setattr(restart_db, "load_run", delayed_read)
    adapter.queue.append(ok("review-1"))
    driver = asyncio.create_task(engine.resume(snapshot.run_id, EventArrived("manual"), TRUSTED_LOCAL))
    await asyncio.wait_for(started.wait(), 5)
    try:
        duplicate = await engine.resume(snapshot.run_id, InterruptedByRestart(1_500), TRUSTED_LOCAL)
        assert duplicate.outcome == "already_driving"
        assert await restart_db.list_receipts(snapshot.run_id) == []
        recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
        assert await recovery.tick() == ()
    finally:
        release.set()
        await driver
    assert len(adapter.calls) == 1
    assert not engine.active_run_ids
    await recovery.shutdown()


async def test_timer_resume_is_not_lost_while_recovery_finishes_pausing(restart_db, monkeypatch):
    engine, adapter, ref = await recovery_engine(restart_db, "wait-kinds.artifact.json")
    snapshot = orphan_snapshot(
        ref, rule_id="sleep", current_step_id="await-timer", event=event("task-created"),
    )
    await restart_db.create_run(snapshot)
    finishing, release, paused_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    emit, load = engine._emit, restart_db.load_run

    async def hold_finished(event_type, current, **kwargs):
        await emit(event_type, current, **kwargs)
        if current.lifecycle is RunLifecycle.PAUSED and event_type == EVENT_RUN_FINISHED:
            finishing.set()
            await release.wait()

    async def observe_paused(run_id):
        current = await load(run_id)
        if current and current.lifecycle is RunLifecycle.PAUSED:
            paused_read.set()
        return current

    monkeypatch.setattr(engine, "_emit", hold_finished)
    monkeypatch.setattr(restart_db, "load_run", observe_paused)
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
    await recovery.tick()
    timer = None
    try:
        await asyncio.wait_for(finishing.wait(), 5)
        timer = asyncio.create_task(WaitScheduler(engine, restart_db, TRUSTED_LOCAL).tick(now=3_000))
        await asyncio.wait_for(paused_read.wait(), 5)
    finally:
        release.set()
        await drain_recovery(recovery)
        if timer is not None:
            await asyncio.wait_for(timer, 5)
        await recovery.shutdown()
    stored = await restart_db.load_run(snapshot.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    assert stored.current_step_id == "sleep-done"
    assert "fired_at" in stored.bindings["timer"]
    assert await restart_db.list_active(snapshot.run_id) == []
    assert not adapter.calls


async def test_slow_recovery_does_not_block_siblings_and_shutdown_is_recoverable(restart_db):
    from tests.fixtures.contracts.engine_contracts import ScriptedAdapter

    started, release = asyncio.Event(), asyncio.Event()

    class SlowAdapter(ScriptedAdapter):
        def invoke_for(self, name):
            invoke = super().invoke_for(name)

            async def delayed(args, principal):
                if args.title == "Review: Slow":
                    started.set()
                    await release.wait()
                return await invoke(args, principal)

            return delayed

    engine, adapter, ref = await recovery_engine(restart_db, adapter=SlowAdapter())
    slow = orphan_snapshot(ref, run_id="a-slow", event=dict(event("task-completed-code"), title="Slow"))
    fast = orphan_snapshot(ref, run_id="b-fast")
    await restart_db.create_run(slow)
    await restart_db.create_run(fast)
    adapter.queue.append(ok("fast-review"))
    recovery = RestartReconciler(
        engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500, concurrency=2,
    )
    assert await asyncio.wait_for(recovery.tick(), 5) == (slow.run_id, fast.run_id)
    await asyncio.wait_for(started.wait(), 5)
    await recovery._tasks[fast.run_id]
    assert (await restart_db.load_run(fast.run_id)).lifecycle is RunLifecycle.COMPLETED
    assert await recovery.tick() == ()
    assert slow.run_id in recovery.active_run_ids
    await recovery.shutdown()
    assert not engine.active_run_ids
    assert not recovery._tasks
    assert await recovery.tick() == ()
    stored = await restart_db.load_run(slow.run_id)
    assert stored.lifecycle is RunLifecycle.RUNNING
    assert stored.context["_in_flight_attempt"]["step_id"] == "ensure-review-task"

    restarted, replay_adapter, _ = fresh_engine("two-rules-one-event.artifact.json", runs=restart_db)
    replay_adapter.queue.append(ok("slow-review"))
    second = RestartReconciler(restarted, restart_db, TRUSTED_LOCAL, process_started_at=2_500)
    assert await second.tick() == (slow.run_id,)
    await drain_recovery(second)
    assert (await restart_db.load_run(slow.run_id)).lifecycle is RunLifecycle.COMPLETED
    assert replay_adapter.args_for("ensure_task")[0].dedup_key == "a-slow:ensure-review-task:-:1"
    await second.shutdown()


async def test_orphan_doctor_names_runs_without_a_process_driver(restart_db):
    from types import SimpleNamespace

    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import _check_orphaned_runs, playbook_v2_checks

    engine, adapter, ref = await recovery_engine(restart_db)
    await restart_db.create_run(orphan_snapshot(ref))
    await restart_db.create_run(orphan_snapshot(ref, run_id="owned"))
    await restart_db.create_run(orphan_snapshot(ref, run_id="waiting", lifecycle=RunLifecycle.PAUSED))
    await restart_db.create_run(orphan_snapshot(ref, run_id="new", updated_at=1_500))
    engine._driving.add("owned")
    recovery = RestartReconciler(engine, restart_db, TRUSTED_LOCAL, process_started_at=1_500)
    config = SimpleNamespace(playbooks=SimpleNamespace(enabled=True))
    ctx = DoctorContext(config, db=restart_db, handler=SimpleNamespace(orchestrator=SimpleNamespace(
        playbook_manager=SimpleNamespace(restart_reconciler=recovery),
    )))
    warning = await _check_orphaned_runs(ctx)
    assert warning.severity is Severity.WARN
    assert [row["run_id"] for row in warning.data["runs"]] == ["orphan"]
    assert warning.data["runs"][0]["current_step_id"] == "ensure-review-task"
    assert not warning.fixable
    assert any(check.id == warning.id for check in playbook_v2_checks())
    adapter.queue.append(ok("review-1"))
    await recovery.tick()
    await drain_recovery(recovery)
    assert (await _check_orphaned_runs(ctx)).severity is Severity.OK
    config.playbooks.enabled = False
    assert (await _check_orphaned_runs(ctx)).severity is Severity.INFO
    config.playbooks.enabled = True
    assert (await _check_orphaned_runs(DoctorContext(config, db=restart_db))).severity is Severity.INFO
    engine._driving.clear()
    await recovery.shutdown()


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


def interrupted_attempt(
    snapshot: RunSnapshot, *, step_id: str, step_kind: str = "command"
) -> StepReceipt:
    """The durable half of an executor call that died before its boundary:
    the ``attempt_start`` fence the engine commits before external work."""
    return StepReceipt(
        receipt_id=uuid4().hex,
        run_id=snapshot.run_id,
        artifact_sha256=snapshot.artifact_sha256,
        rule_id=snapshot.rule_id,
        step_id=step_id,
        step_kind=step_kind,
        receipt_kind="attempt_start",
        turn_index=0,
        outcome="started",
        started_at=1_000.0,
        snapshot_version=snapshot.version,
        attempt=1,
        idempotency_key=f"{snapshot.run_id}:{step_id}:-:1",
        # No completion is deliberately the only signal of ambiguity.
        completed_at=None,
    )


class TestAmbiguousInterruption:
    async def _ambiguous_llm(self):
        runs = RecordingRunRepository()
        engine, _adapter, ref = fresh_engine("review-pipeline.artifact.json", runs=runs)
        paused = await engine.run_rule(
            ref, "review-on-task-completed", event("task-completed-code"), TRUSTED_LOCAL,
            pause_before_start=True,
        )
        snapshot = replace(
            paused.snapshot, lifecycle=RunLifecycle.RUNNING, current_step_id="classify-risk"
        )
        runs.snapshots[snapshot.run_id] = snapshot
        runs.receipts.append(
            interrupted_attempt(snapshot, step_id="classify-risk", step_kind="llm")
        )
        ambiguous = await engine.resume(
            snapshot.run_id, EventArrived(event_id="restart"), TRUSTED_LOCAL
        )
        return engine, runs, ambiguous

    @pytest.mark.asyncio
    async def test_retry_safe_command_replays_with_the_same_attempt_key(self):
        runs = RecordingRunRepository()
        engine, adapter, ref = fresh_engine("review-pipeline.artifact.json", runs=runs)
        paused = await engine.run_rule(
            ref, "review-on-task-completed", event("task-completed-code"), TRUSTED_LOCAL,
            pause_before_start=True,
        )
        snapshot = replace(paused.snapshot, lifecycle=RunLifecycle.RUNNING)
        runs.snapshots[snapshot.run_id] = snapshot
        runs.receipts.append(interrupted_attempt(snapshot, step_id="ensure-review-task"))
        restarted, replay_adapter, _ref = fresh_engine(
            "review-pipeline.artifact.json", runs=runs
        )
        replay_adapter.queue.append(ok("review-1"))
        outcome = await restarted.resume(
            snapshot.run_id, EventArrived(event_id="restart"), TRUSTED_LOCAL
        )

        replay = next(
            receipt
            for receipt in runs.receipts
            if receipt.step_id == "ensure-review-task" and receipt.completed_at is not None
        )
        assert replay.idempotency_key == f"{snapshot.run_id}:ensure-review-task:-:1"
        assert any(receipt.error_code == "interrupted" for receipt in runs.receipts)
        assert outcome.snapshot.bindings["review"]["task_id"] == "review-1"

    @pytest.mark.asyncio
    async def test_durable_in_flight_marker_requires_an_operator_for_llm(self):
        runs = RecordingRunRepository()
        engine, _adapter, ref = fresh_engine(
            "review-pipeline.artifact.json", runs=runs
        )
        paused = await engine.run_rule(
            ref,
            "review-on-task-completed",
            event("task-completed-code"),
            TRUSTED_LOCAL,
            pause_before_start=True,
        )
        snapshot = replace(
            paused.snapshot,
            lifecycle=RunLifecycle.RUNNING,
            current_step_id="classify-risk",
            context=dict(paused.snapshot.context)
            | {
                "_in_flight_attempt": {
                    "step_id": "classify-risk",
                    "step_kind": "llm",
                    "iteration": -1,
                    "attempt": 1,
                    "started_at": 1_000.0,
                    "idempotency_key": (
                        f"{paused.run_id}:classify-risk:-:1"
                    ),
                }
            },
        )
        runs.snapshots[snapshot.run_id] = snapshot

        recovered = await engine.resume(
            snapshot.run_id, EventArrived(event_id="restart"), TRUSTED_LOCAL
        )

        assert recovered.outcome == "operator_decision_required"
        assert recovered.snapshot.operator_decision is not None
        assert "_in_flight_attempt" not in recovered.snapshot.context

    @pytest.mark.asyncio
    async def test_non_retry_safe_llm_pauses_for_an_operator(self):
        _engine, _runs, outcome = await self._ambiguous_llm()

        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert outcome.outcome == "operator_decision_required"
        assert outcome.snapshot.operator_decision is not None
        assert outcome.snapshot.bindings == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            ("accept", {"outcome": "low", "value": {"risk": "low"}}),
            ("accept_outcome", {"outcome": "low", "value": {"risk": "low"}}),
            ("retry", {}),
            ("fail", {}),
            ("cancel", {}),
        ],
    )
    async def test_each_operator_resolution_is_receipted(self, kind, payload):
        engine, runs, ambiguous = await self._ambiguous_llm()

        resolved = await engine.resume(
            ambiguous.run_id, OperatorResolution(kind=kind, payload=payload), TRUSTED_LOCAL
        )

        assert resolved.snapshot.operator_decision is None
        assert any(
            receipt.result.get("operator_resolution") == kind for receipt in runs.receipts
        )

    @pytest.mark.asyncio
    async def test_invalid_operator_resolution_preserves_the_pending_decision(self):
        engine, runs, ambiguous = await self._ambiguous_llm()
        receipt_count = len(runs.receipts)

        rejected = await engine.resume(
            ambiguous.run_id, OperatorResolution(kind="skip"), TRUSTED_LOCAL
        )

        assert rejected.outcome == "contract_violation"
        assert rejected.snapshot.operator_decision is not None
        assert runs.snapshots[ambiguous.run_id].operator_decision is not None
        assert len(runs.receipts) == receipt_count

    @pytest.mark.asyncio
    async def test_a_playbook_principal_cannot_resolve_its_own_run(self):
        engine, runs, ambiguous = await self._ambiguous_llm()
        playbook = replace(TRUSTED_LOCAL, kind=PrincipalKind.PLAYBOOK)

        denied = await engine.resume(
            ambiguous.run_id, OperatorResolution(kind="fail"), playbook
        )

        assert denied.outcome == "unauthorized"
        assert denied.snapshot.operator_decision is not None
        assert runs.snapshots[ambiguous.run_id].operator_decision is not None


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
        keys = [
            (r.step_id, r.iteration, r.attempt, r.turn_index, r.receipt_kind)
            for r in runs.receipts
        ]
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
        trail = [
            (r.step_id, r.iteration, r.attempt)
            for r in runs.receipts
            if r.receipt_kind == "step"
        ]
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


class TestRestartProcessBoundaries:
    @staticmethod
    def _kill_after_persist(database_path: str, snapshot: RunSnapshot) -> None:
        child = multiprocessing.get_context("spawn").Process(
            target=_persist_then_sigkill, args=(database_path, snapshot)
        )
        child.start()
        child.join(timeout=15)
        assert child.exitcode == -signal.SIGKILL

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_restart_mid_command_after_sigkill_replays_the_same_attempt(
        self, tmp_path
    ):
        database_path = str(tmp_path / "restart-command.sqlite")
        template = RecordingRunRepository()
        engine, _adapter, ref = fresh_engine(
            "review-pipeline.artifact.json", runs=template
        )
        paused = await engine.run_rule(
            ref,
            "review-on-task-completed",
            event("task-completed-code"),
            TRUSTED_LOCAL,
            pause_before_start=True,
        )
        snapshot = replace(
            paused.snapshot,
            lifecycle=RunLifecycle.RUNNING,
            context=dict(paused.snapshot.context)
            | {
                "_in_flight_attempt": {
                    "step_id": "ensure-review-task",
                    "step_kind": "command",
                    "iteration": -1,
                    "attempt": 1,
                    "started_at": 1_000.0,
                    "idempotency_key": (
                        f"{paused.run_id}:ensure-review-task:-:1"
                    ),
                }
            },
        )
        self._kill_after_persist(database_path, snapshot)

        runs = SQLiteRunRepository(database_path)
        restarted, adapter, _ref = fresh_engine(
            "review-pipeline.artifact.json", runs=runs
        )
        adapter.queue.append(ok("review-after-restart"))
        resumed = await restarted.resume(
            snapshot.run_id, EventArrived(event_id="restart"), TRUSTED_LOCAL
        )

        replay = next(
            receipt
            for receipt in await runs.list_receipts(snapshot.run_id)
            if receipt.step_id == "ensure-review-task"
            and receipt.receipt_kind == "step"
        )
        assert replay.idempotency_key == (
            f"{snapshot.run_id}:ensure-review-task:-:1"
        )
        assert resumed.snapshot.bindings["review"]["task_id"] == (
            "review-after-restart"
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_restart_mid_llm_after_sigkill_requires_operator(self, tmp_path):
        database_path = str(tmp_path / "restart-llm.sqlite")
        template = RecordingRunRepository()
        engine, _adapter, ref = fresh_engine(
            "review-pipeline.artifact.json", runs=template
        )
        paused = await engine.run_rule(
            ref,
            "review-on-task-completed",
            event("task-completed-code"),
            TRUSTED_LOCAL,
            pause_before_start=True,
        )
        snapshot = replace(
            paused.snapshot,
            lifecycle=RunLifecycle.RUNNING,
            current_step_id="classify-risk",
            context=dict(paused.snapshot.context)
            | {
                "_in_flight_attempt": {
                    "step_id": "classify-risk",
                    "step_kind": "llm",
                    "iteration": -1,
                    "attempt": 1,
                    "started_at": 1_000.0,
                    "idempotency_key": f"{paused.run_id}:classify-risk:-:1",
                }
            },
        )
        self._kill_after_persist(database_path, snapshot)

        runs = SQLiteRunRepository(database_path)
        restarted, _adapter, _ref = fresh_engine(
            "review-pipeline.artifact.json", runs=runs
        )
        resumed = await restarted.resume(
            snapshot.run_id, EventArrived(event_id="restart"), TRUSTED_LOCAL
        )

        assert resumed.outcome == "operator_decision_required"
        assert resumed.snapshot.operator_decision is not None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_restart_at_wait_deadline_after_sigkill_resumes_once(self, tmp_path):
        database_path = str(tmp_path / "restart-wait.sqlite")
        template = RecordingRunRepository()
        engine, _adapter, ref = fresh_engine(
            "wait-kinds.artifact.json", runs=template, waits=RecordingWaitRepository()
        )
        paused = await engine.run_rule(
            ref, "sleep", event("task-created"), TRUSTED_LOCAL
        )
        assert paused.snapshot.wait is not None
        self._kill_after_persist(database_path, paused.snapshot)

        runs = SQLiteRunRepository(database_path)
        restarted, _adapter, _ref = fresh_engine(
            "wait-kinds.artifact.json", runs=runs
        )
        resumed = await restarted.resume(
            paused.run_id,
            TimerFired(paused.snapshot.wait.wait_id),
            TRUSTED_LOCAL,
        )

        assert resumed.lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_restart_mid_loop_after_sigkill_uses_the_same_iteration(self, tmp_path):
        database_path = str(tmp_path / "restart-loop.sqlite")
        template_runs = RecordingRunRepository()
        _engine, _adapter, ref = fresh_engine("sequential-loop.artifact.json", runs=template_runs)
        snapshot = crashed_mid_loop(ref, index=1, items=["d-1", "d-2", "d-3"])
        child = multiprocessing.get_context("spawn").Process(
            target=_persist_then_sigkill, args=(database_path, snapshot)
        )
        child.start()
        child.join(timeout=15)
        assert child.exitcode == -signal.SIGKILL

        runs = SQLiteRunRepository(database_path)
        restarted, adapter, _ref = fresh_engine("sequential-loop.artifact.json", runs=runs)
        adapter.queue.extend([ok("t-2"), ok("t-3")])
        resumed = await restarted.resume(
            snapshot.run_id, EventArrived(event_id="restart", payload={}), TRUSTED_LOCAL
        )

        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert [args.title for args in adapter.args_for("ensure_task")] == [
            "Gate: d-2",
            "Gate: d-3",
        ]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_restart_after_agent_task_creation_does_not_create_a_second_child(self, tmp_path):
        database_path = str(tmp_path / "restart-agent-task.sqlite")
        template_runs = RecordingRunRepository()
        _engine, _adapter, ref = fresh_engine("review-pipeline.artifact.json", runs=template_runs)
        snapshot = RunSnapshot(
            run_id="run-agent-task-killed",
            playbook_id="default-pipeline",
            artifact_sha256=ref.artifact_sha256,
            rule_id="review-on-task-completed",
            lifecycle=RunLifecycle.PAUSED,
            current_step_id="escalate",
            event=event("task-completed-code"),
            context={"dispatch_id": "d-agent", "playbook_id": "default-pipeline", "rule_id": "review-on-task-completed"},
            bindings={"review": {"task_id": "review-1", "created": True}},
            agent_task_ids=("child-created-before-kill",),
            wait=WaitSpec(
                wait_id="wait-child-created-before-kill",
                run_id="run-agent-task-killed",
                step_id="escalate",
                kind="agent_task",
                match={"task_id": "child-created-before-kill"},
                created_at=1_000.0,
            ),
            event_type="task.completed",
            event_id="evt-agent",
            dispatch_id="d-agent",
            started_at=1_000.0,
            updated_at=1_000.0,
        )
        child = multiprocessing.get_context("spawn").Process(
            target=_persist_then_sigkill, args=(database_path, snapshot)
        )
        child.start()
        child.join(timeout=15)
        assert child.exitcode == -signal.SIGKILL

        runs = SQLiteRunRepository(database_path)
        restarted, adapter, _ref = fresh_engine("review-pipeline.artifact.json", runs=runs)
        resumed = await restarted.resume(
            snapshot.run_id,
            ChildTaskCompleted(task_id="child-created-before-kill", status="completed"),
            TRUSTED_LOCAL,
        )

        assert resumed.lifecycle is RunLifecycle.PAUSED
        assert resumed.snapshot.agent_task_ids == ("child-created-before-kill",)
        assert "create_task" not in adapter.names
