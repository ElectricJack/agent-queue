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
from src.playbooks.engine import PlaybookEngine
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
        keys = [(r.step_id, r.iteration, r.attempt) for r in receipts]
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
