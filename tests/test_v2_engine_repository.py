"""The engine against the *real* repository, on both backends.

``tests/test_v2_engine.py`` proves the engine's contract with a counting
double.  A double cannot prove the two things that only a database can: that
the snapshot the engine builds actually serializes and round-trips, and that
a replay of one event really does collide on
``uq_playbook_v2_runs_dispatch_rule`` rather than on a pre-read a concurrent
dispatch could race.  Those are the assertions here.

On SQLite ``immediate()``'s per-adapter lock serialises callers, so a green
SQLite run proves the *result*; only PostgreSQL proves the fence.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import insert

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.database import Database
from src.database.tables import playbook_artifacts
from src.playbooks.engine import HumanDecision, PlaybookEngine, WaitScheduler
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    LIST_TASKS,
    EnsureTaskResult,
    ListTasksResult,
    registry_with,
)
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    StubActivations,
    artifact_ref_for,
    event,
    load_artifact,
)

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()
NOW = 1_000_000.0


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
    yield database
    await database.close()


async def seed_artifact(database, ref) -> None:
    """``playbook_v2_runs.artifact_sha256`` is a real FK on PostgreSQL."""
    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ref.artifact_sha256,
                playbook_id=ref.playbook_id,
                scope="system",
                scope_identifier="",
                schema_generation=ref.schema_generation,
                version=ref.version,
                source_digest=ref.source_digest,
                contract_fingerprint=ref.contract_fingerprint,
                profile_fingerprint="",
                compiler_build=ref.compiler_build,
                path=f"artifacts/{ref.digest}.json",
                size_bytes=10,
                validation="{}",
                compiled_at=None,
                created_at=NOW,
            )
        )


async def build(database):
    artifact = load_artifact("two-rules-one-event.artifact.json")
    ref = artifact_ref_for(artifact)
    await seed_artifact(database, ref)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
    store = InMemoryArtifactStore()
    store.put(artifact)
    bus = RecordingBus()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, clock=lambda: NOW, artifact_store=store, bus=bus
        ),
        runs=database,
        waits=None,
        activations=StubActivations([ref]),
    )
    return engine, adapter, ref, bus


def ok() -> CommandResult:
    return CommandResult(
        outcome="created", value=EnsureTaskResult(task_id="t-1", created=True), summary="ok"
    )


def listed(count: int = 1) -> CommandResult:
    return CommandResult(
        outcome="listed", value=ListTasksResult(tasks=[], count=count), summary="ok"
    )


@pytest.mark.asyncio
async def test_a_dispatch_persists_one_run_and_its_receipts_per_rule(db):
    engine, adapter, _ref, _bus = await build(db)
    adapter.queue.extend([ok(), listed()])
    result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
    assert len(result.run_ids) == 2
    for run_id in result.run_ids:
        snapshot = await db.load_run(run_id)
        assert snapshot is not None
        assert snapshot.lifecycle is RunLifecycle.COMPLETED
        assert snapshot.dispatch_id == result.dispatch_id
        receipts = await db.list_receipts(run_id)
        assert receipts
        # Every attempt of the run is receipted, each with its own identity.
        keys = [
            (r.step_id, r.iteration, r.attempt, r.turn_index, r.receipt_kind)
            for r in receipts
        ]
        assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_the_bound_result_round_trips_through_the_snapshot(db):
    engine, adapter, ref, _bus = await build(db)
    adapter.queue.append(ok())
    outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
    stored = await db.load_run(outcome.run_id)
    assert stored.bindings["review"] == {"task_id": "t-1", "created": True}


@pytest.mark.asyncio
async def test_a_replayed_event_is_refused_by_the_database_index(db):
    """§2.5 item 9 — the deterministic ``dispatch_id`` turns a replay into a
    unique-index collision, which is stronger than a pre-read."""
    engine, adapter, _ref, _bus = await build(db)
    adapter.queue.extend([ok(), listed()])
    first = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
    second = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
    assert second.run_ids == first.run_ids
    assert set(second.deduplicated) == {"review", "sweep"}
    rows = await db.list_runs(playbook_id="two-rules-one-event", limit=50)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_two_concurrent_dispatches_of_one_event_still_create_two_runs(db):
    """The fence, not the pre-read: both dispatches race the same index."""
    engine, adapter, _ref, _bus = await build(db)
    adapter.queue.extend([ok(), listed(), ok(), listed()])
    await asyncio.gather(
        engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL),
        engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL),
    )
    rows = await db.list_runs(playbook_id="two-rules-one-event", limit=50)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_failed_run_records_its_reserved_outcome_as_an_error_code(db):
    engine, adapter, ref, _bus = await build(db)
    adapter.queue.append(RuntimeError("boom"))
    outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
    stored = await db.load_run(outcome.run_id)
    assert stored.lifecycle is RunLifecycle.FAILED
    receipts = {r.step_id: r for r in await db.list_receipts(outcome.run_id)}
    # The command's own receipt names the reserved outcome; the terminal it
    # routed to is a separate attempt and carries none.
    assert receipts["ensure-review-task"].error_code == "runtime_error"
    assert receipts["ensure-review-task"].outcome == "failure"
    assert receipts["review-failed"].error_code is None


# --------------------------------------------------------------------------
# Waits and loops against the real repository (T-6, T-7)
#
# The doubles in ``test_v2_engine.py`` prove the engine's ordering.  Only the
# database proves that the change set the engine hands to ``commit_boundary``
# actually lands: that a suspension writes the wait row inside the same
# transaction as the snapshot and the receipt, that the inbox is scanned in
# that transaction, and that a loop's four-part attempt identities survive
# ``uq_playbook_step_receipts_attempt``.
# --------------------------------------------------------------------------


async def build_for(database, artifact_name: str):
    artifact = load_artifact(artifact_name)
    ref = artifact_ref_for(artifact)
    await seed_artifact(database, ref)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
    store = InMemoryArtifactStore()
    store.put(artifact)
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, clock=lambda: NOW, artifact_store=store, bus=RecordingBus()
        ),
        runs=database,
        waits=database,
        activations=StubActivations([ref]),
    )
    return engine, adapter, ref


@pytest.mark.asyncio
async def test_a_suspension_writes_the_wait_row_in_the_same_boundary(db):
    engine, _adapter, ref = await build_for(db, "wait-kinds.artifact.json")
    outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
    assert outcome.lifecycle is RunLifecycle.PAUSED
    active = await db.list_active(outcome.run_id)
    assert [w.step_id for w in active] == ["await-approval"]
    assert active[0].match == {"task_id": "task-1"}
    stored = await db.load_run(outcome.run_id)
    assert stored.wait is not None
    # The registration is fenced to the version the boundary wrote, so a
    # resume that found the two disagreeing would refuse rather than resume
    # into a moved-on state.
    receipts = await db.list_receipts(outcome.run_id)
    assert receipts[-1].wait_id == active[0].wait_id
    assert receipts[-1].snapshot_version == stored.version


@pytest.mark.asyncio
async def test_resuming_a_wait_clears_its_row_and_advances_the_run(db):
    engine, _adapter, ref = await build_for(db, "wait-kinds.artifact.json")
    outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
    resumed = await engine.resume(
        outcome.run_id, HumanDecision(decision="approve"), TRUSTED_LOCAL
    )
    assert resumed.lifecycle is RunLifecycle.COMPLETED
    assert await db.list_active(outcome.run_id) == []
    stored = await db.load_run(outcome.run_id)
    assert stored.wait is None
    assert stored.bindings["approval"]["resolution"] == "approve"
    gate = [r for r in await db.list_receipts(outcome.run_id) if r.step_id == "await-approval"]
    # Two receipts for one step instance: the suspension and the resume.  The
    # database's own uq_playbook_step_receipts_attempt is what would reject a
    # second attempt=1, so a green run here proves the counter, not the code.
    assert sorted(r.attempt for r in gate) == [1, 2]


@pytest.mark.asyncio
async def test_an_expired_wait_is_claimed_once_and_resumes_the_run(db):
    engine, _adapter, ref = await build_for(db, "wait-kinds.artifact.json")
    outcome = await engine.run_rule(ref, "sleep", event("task-created"), TRUSTED_LOCAL)
    scheduler = WaitScheduler(engine, db, TRUSTED_LOCAL)
    assert await scheduler.tick(NOW + 31) == (outcome.run_id,)
    stored = await db.load_run(outcome.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    assert stored.current_step_id == "sleep-done"
    # The claim is a compare-and-set, so a second sweep finds nothing.
    assert await scheduler.tick(NOW + 60) == ()


@pytest.mark.asyncio
async def test_a_loop_persists_one_receipt_per_iteration_boundary(db):
    engine, adapter, ref = await build_for(db, "sequential-loop.artifact.json")
    adapter.queue.extend(
        [
            CommandResult(
                outcome="listed",
                value=ListTasksResult(tasks=[{"id": "d-1"}, {"id": "d-2"}], count=2),
                summary="ok",
            ),
            ok(),
            ok(),
        ]
    )
    outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
    assert outcome.lifecycle is RunLifecycle.COMPLETED
    receipts = await db.list_receipts(outcome.run_id)
    keys = [(r.step_id, r.iteration, r.attempt) for r in receipts]
    assert len(keys) == len(set(keys))
    assert ("open-gate", 0, 1) in keys
    assert ("open-gate", 1, 1) in keys
    stored = await db.load_run(outcome.run_id)
    assert stored.loop is None
    assert stored.bindings["sweep_result"]["succeeded"] == 2


@pytest.mark.asyncio
async def test_cancelling_a_paused_run_retires_its_wait_row(db):
    """T-9 §4.9's paused row, against the database that has to enforce it.

    The double can show that the engine *asks* for ``clear_run_waits``; only
    the database shows that the wait row and the terminal snapshot land in
    one transaction.  A cancelled run whose ``playbook_waits`` row is still
    ``active`` is claimable by a later event, and after a restart nothing
    remembers that the run it points at is over.
    """
    engine, _adapter, ref = await build_for(db, "wait-kinds.artifact.json")
    outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
    assert outcome.lifecycle is RunLifecycle.PAUSED
    assert await db.list_active(outcome.run_id) != []

    cancelled = await engine.cancel(outcome.run_id, TRUSTED_LOCAL, reason="operator")

    assert cancelled.lifecycle is RunLifecycle.CANCELLED
    assert await db.list_active(outcome.run_id) == []
    stored = await db.load_run(outcome.run_id)
    assert stored.lifecycle is RunLifecycle.CANCELLED
    assert stored.wait is None
    assert stored.cancel_requested_at is not None
    assert stored.completed_at is not None

    gate = [r for r in await db.list_receipts(outcome.run_id) if r.step_id == "await-approval"]
    # The suspension's receipt and the cancellation's, on the same step
    # instance — so the four-part attempt identity is what keeps the second
    # one off ``uq_playbook_step_receipts_attempt``.
    assert sorted(r.attempt for r in gate) == [1, 2]
    cancellation = max(gate, key=lambda r: r.attempt)
    assert cancellation.outcome == "cancelled"
    assert cancellation.cancelled_at is not None
    assert cancellation.result["cancellation"] == "acknowledged"
    assert cancellation.snapshot_version == stored.version


@pytest.mark.asyncio
async def test_cancelling_a_terminal_run_is_refused_by_the_engine(db):
    """The refusal never reaches ``request_cancel``'s CAS (§4.9)."""
    engine, adapter, ref = await build_for(db, "two-rules-one-event.artifact.json")
    adapter.queue.append(
        CommandResult(
            outcome="created",
            value=EnsureTaskResult(task_id="t-1", created=True),
            summary="ensured",
        )
    )
    outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
    assert outcome.lifecycle is RunLifecycle.COMPLETED
    before = await db.list_receipts(outcome.run_id)

    refused = await engine.cancel(outcome.run_id, TRUSTED_LOCAL)

    assert refused.outcome == "already_terminal"
    assert refused.error == f"Run '{outcome.run_id}' already completed"
    stored = await db.load_run(outcome.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    assert stored.cancel_requested_at is None
    assert len(await db.list_receipts(outcome.run_id)) == len(before)
