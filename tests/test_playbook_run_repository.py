"""Playbook V2 run state — snapshots, boundaries, receipts, cancellation.

Package 3 child plan §8, §9 and §4.4.  Every concurrency case is parametrised
over both backends: on SQLite ``immediate()``'s per-adapter ``asyncio.Lock``
serialises callers, so a green SQLite run proves the *result* is correct but
not that the compare-and-set is what enforced it.  Only PostgreSQL proves the
fence, which is why ``POSTGRES_TEST_DSN`` matters for this file.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from src.database import Database
from src.database.tables import playbook_artifacts
from src.playbooks.receipts import StepReceipt, transition_id
from src.playbooks.run_state import (
    DuplicateAttempt,
    IllegalLifecycleTransition,
    LoopFrame,
    RunBudget,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    StateLimitExceeded,
    StateLimits,
    UndeclaredBinding,
    bind_step_output,
    check_result_size,
    deserialize_snapshot,
    serialize_snapshot,
)
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

ARTIFACT = "sha256:" + "1c" * 32
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
    await seed_artifact(database, ARTIFACT)
    yield database
    await database.close()


async def seed_artifact(database, digest: str) -> None:
    """A run's ``artifact_sha256`` is a real FK on PostgreSQL."""
    from sqlalchemy import insert

    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=digest,
                playbook_id="task-review",
                scope="system",
                scope_identifier="",
                schema_generation=2,
                version=3,
                source_digest="sha256:" + "2a" * 32,
                contract_fingerprint="sha256:" + "3b" * 32,
                profile_fingerprint="",
                compiler_build="test-build",
                path=f"artifacts/{digest.split(':', 1)[1]}.json",
                size_bytes=10,
                validation="{}",
                compiled_at=None,
                created_at=NOW,
            )
        )


def make_snapshot(**overrides) -> RunSnapshot:
    base = {
        "run_id": "run-1",
        "playbook_id": "task-review",
        "artifact_sha256": ARTIFACT,
        "rule_id": "on-task-completed",
        "event_type": "task.completed",
        "started_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return RunSnapshot(**base)


def make_receipt(snapshot: RunSnapshot, **overrides) -> StepReceipt:
    base = {
        "receipt_id": f"receipt-{overrides.get('step_id', 'ensure-task')}-"
        f"{overrides.get('iteration', -1)}-{overrides.get('attempt', 1)}",
        "run_id": snapshot.run_id,
        "artifact_sha256": snapshot.artifact_sha256,
        "rule_id": snapshot.rule_id,
        "step_id": "ensure-task",
        "step_kind": "command",
        "outcome": "success",
        "started_at": NOW,
        "snapshot_version": snapshot.version + 1,
    }
    base.update(overrides)
    return StepReceipt(**base)


# -- B-1: the snapshot value ------------------------------------------------


def test_snapshot_round_trips_through_canonical_json():
    snapshot = make_snapshot(
        version=4,
        current_step_id="review",
        bindings={"ensure-task": {"task_id": "t-1"}},
        loop=LoopFrame(
            step_id="each-file",
            item_binding="file",
            collection_digest="sha256:" + "9f" * 32,
            index=2,
            total=5,
            partial=("a", "b"),
        ),
        budget=RunBudget(llm_calls=2, total_tokens=1200, max_total_tokens=4000),
        agent_task_ids=("t-1", "t-2"),
    )
    payload = serialize_snapshot(snapshot)
    restored = deserialize_snapshot(payload)
    assert restored == snapshot
    assert serialize_snapshot(restored) == payload


def test_snapshot_redacted_drops_sensitive_values():
    snapshot = make_snapshot(sensitive={"sensitive:abc": "hunter2"})
    assert snapshot.redacted().sensitive == {}
    assert "hunter2" not in json.dumps(snapshot.redacted().to_body())


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunLifecycle.COMPLETED, RunLifecycle.RUNNING),
        (RunLifecycle.FAILED, RunLifecycle.RUNNING),
        (RunLifecycle.CANCELLED, RunLifecycle.RUNNING),
        (RunLifecycle.TIMED_OUT, RunLifecycle.PAUSED),
        (RunLifecycle.RUNNING, RunLifecycle.CANCELLED),
        (RunLifecycle.CANCELLING, RunLifecycle.RUNNING),
        (RunLifecycle.CANCELLING, RunLifecycle.PAUSED),
    ],
)
def test_lifecycle_rejects_an_illegal_transition(current, target):
    from src.playbooks.run_state import validate_transition

    with pytest.raises(IllegalLifecycleTransition):
        validate_transition("run-1", current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunLifecycle.RUNNING, RunLifecycle.PAUSED),
        (RunLifecycle.RUNNING, RunLifecycle.CANCELLING),
        (RunLifecycle.PAUSED, RunLifecycle.CANCELLED),
        (RunLifecycle.PAUSED, RunLifecycle.RUNNING),
        (RunLifecycle.CANCELLING, RunLifecycle.CANCELLED),
    ],
)
def test_lifecycle_allows_the_design_spec_transitions(current, target):
    from src.playbooks.run_state import validate_transition

    validate_transition("run-1", current, target)


def test_bindings_reject_undeclared_keys():
    snapshot = make_snapshot()
    with pytest.raises(UndeclaredBinding) as excinfo:
        bind_step_output(
            snapshot,
            step_id="ensure-task",
            value={"task_id": "t-1", "raw_handler_dict": {"anything": True}},
            declared=("task_id",),
        )
    assert excinfo.value.keys == ("raw_handler_dict",)
    assert snapshot.bindings == {}


def test_bindings_accept_declared_keys():
    snapshot = bind_step_output(
        make_snapshot(),
        step_id="ensure-task",
        value={"task_id": "t-1"},
        declared=("task_id", "status"),
    )
    assert snapshot.bindings == {"ensure-task": {"task_id": "t-1"}}


# -- B-2: the two size limits ----------------------------------------------


def test_check_result_size_rejects_an_oversize_result():
    limits = StateLimits(max_result_bytes=128, max_snapshot_bytes=4096)
    with pytest.raises(StateLimitExceeded) as excinfo:
        check_result_size("run-1", "ensure-task", {"blob": "x" * 500}, limits=limits)
    assert excinfo.value.kind == "result"
    assert excinfo.value.limit == 128
    assert excinfo.value.size > 128
    assert excinfo.value.code == "state_limit_exceeded"


def test_oversize_snapshot_raises_before_the_transaction():
    limits = StateLimits(max_result_bytes=64, max_snapshot_bytes=256)
    snapshot = make_snapshot(context={"blob": "x" * 4000})
    with pytest.raises(StateLimitExceeded) as excinfo:
        serialize_snapshot(snapshot, limits=limits)
    assert excinfo.value.kind == "snapshot"


async def test_oversize_result_fails_the_run_without_storing_it(db):
    """§8.2: reject, do not externalize — and never store the payload."""
    db.set_playbook_state_limits(StateLimits(max_result_bytes=256, max_snapshot_bytes=4_194_304))
    snapshot = await db.create_run(make_snapshot())
    receipt = make_receipt(snapshot, result={"blob": "x" * 4000})

    with pytest.raises(StateLimitExceeded) as excinfo:
        await db.commit_boundary(replace(snapshot, current_step_id="ensure-task"), receipt)

    assert excinfo.value.kind == "result"
    reloaded = await db.load_run(snapshot.run_id)
    assert reloaded.version == 0
    assert await db.list_receipts(snapshot.run_id) == []

    # The failure is legible: the run fails with the size, not the payload.
    failure = make_receipt(
        snapshot,
        receipt_id="receipt-oversize",
        outcome="failure",
        error_code="state_limit_exceeded",
        error=str(excinfo.value),
    )
    failed = await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.FAILED, error_code="state_limit_exceeded"),
        failure,
    )
    assert failed.lifecycle is RunLifecycle.FAILED
    stored = await db.list_receipts(snapshot.run_id)
    assert "x" * 100 not in json.dumps([r.result for r in stored])


# -- B-4: create and load --------------------------------------------------


async def test_create_and_load_a_run(db):
    snapshot = make_snapshot(
        current_step_id="ensure-task",
        context={"task_id": "t-1"},
        budget=RunBudget(llm_calls=1, total_tokens=42),
    )
    created = await db.create_run(snapshot)
    assert created == snapshot

    loaded = await db.load_run("run-1")
    assert loaded == snapshot
    assert loaded.version == 0
    assert await db.load_run("missing") is None


async def test_list_runs_filters_by_playbook_lifecycle_and_artifact(db):
    await db.create_run(make_snapshot(run_id="run-a"))
    await db.create_run(
        make_snapshot(run_id="run-b", lifecycle=RunLifecycle.PAUSED, started_at=NOW + 1)
    )
    assert {r.run_id for r in await db.list_runs(playbook_id="task-review")} == {"run-a", "run-b"}
    assert [r.run_id for r in await db.list_runs(lifecycle="paused")] == ["run-b"]
    assert len(await db.list_runs(artifact_sha256=ARTIFACT)) == 2
    assert await db.list_runs(playbook_id="nope") == []


# -- B-5: the commit boundary ----------------------------------------------


async def test_commit_boundary_advances_the_version_and_writes_one_receipt(db):
    snapshot = await db.create_run(make_snapshot())
    receipt = make_receipt(
        snapshot, selected_transition=transition_id("on-task-completed", "ensure-task", "success")
    )

    advanced = await db.commit_boundary(
        replace(snapshot, current_step_id="review", bindings={"ensure-task": {"task_id": "t-1"}}),
        receipt,
    )

    assert advanced.version == 1
    assert snapshot.version == 0, "the argument is frozen and must not be mutated"
    reloaded = await db.load_run("run-1")
    assert reloaded == advanced
    receipts = await db.list_receipts("run-1")
    assert len(receipts) == 1
    assert receipts[0].idempotency_key == "run-1:ensure-task:-:1"
    assert receipts[0].selected_transition == "on-task-completed::ensure-task::success"


async def test_stale_version_raises_snapshot_version_conflict(db):
    snapshot = await db.create_run(make_snapshot())
    await db.commit_boundary(replace(snapshot, current_step_id="a"), make_receipt(snapshot))

    with pytest.raises(SnapshotVersionConflict) as excinfo:
        await db.commit_boundary(
            replace(snapshot, current_step_id="b"),
            make_receipt(snapshot, receipt_id="receipt-2", step_id="review"),
        )
    assert excinfo.value.expected == 0
    assert excinfo.value.actual == 1


async def test_commit_boundary_rolls_back_the_receipt_when_the_cas_fails(db):
    snapshot = await db.create_run(make_snapshot())
    await db.commit_boundary(replace(snapshot, current_step_id="a"), make_receipt(snapshot))

    with pytest.raises(SnapshotVersionConflict):
        await db.commit_boundary(
            replace(snapshot, current_step_id="b"),
            make_receipt(snapshot, receipt_id="receipt-loser", step_id="review"),
        )

    receipts = await db.list_receipts("run-1")
    assert [r.receipt_id for r in receipts] == ["receipt-ensure-task--1-1"]


async def test_commit_boundary_rejects_an_illegal_lifecycle_move(db):
    snapshot = await db.create_run(make_snapshot())
    done = await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.COMPLETED, completed_at=NOW + 5),
        make_receipt(snapshot),
    )
    with pytest.raises(IllegalLifecycleTransition):
        await db.commit_boundary(
            replace(done, lifecycle=RunLifecycle.RUNNING),
            make_receipt(done, receipt_id="receipt-late", step_id="late"),
        )


async def test_commit_boundary_refuses_a_receipt_from_another_run(db):
    snapshot = await db.create_run(make_snapshot())
    other = make_snapshot(run_id="run-other")
    with pytest.raises(ValueError, match="run-other"):
        await db.commit_boundary(snapshot, make_receipt(other))


# -- B-6: attempt idempotency ----------------------------------------------


async def test_duplicate_attempt_is_rejected_by_the_database(db):
    snapshot = await db.create_run(make_snapshot())
    first = await db.commit_boundary(replace(snapshot, current_step_id="a"), make_receipt(snapshot))

    with pytest.raises(DuplicateAttempt) as excinfo:
        await db.commit_boundary(
            replace(first, current_step_id="b"),
            make_receipt(first, receipt_id="receipt-replay"),
        )
    assert excinfo.value.step_id == "ensure-task"
    assert excinfo.value.attempt == 1

    # The whole boundary rolled back: no receipt, and no version advance.
    assert len(await db.list_receipts("run-1")) == 1
    assert (await db.load_run("run-1")).version == 1


async def test_a_second_iteration_of_the_same_step_is_not_a_duplicate(db):
    snapshot = await db.create_run(make_snapshot())
    current = snapshot
    for iteration in (0, 1):
        current = await db.commit_boundary(
            replace(current, current_step_id="each-file"),
            make_receipt(current, step_id="each-file", iteration=iteration),
        )
    keys = {r.idempotency_key for r in await db.list_receipts("run-1")}
    assert keys == {"run-1:each-file:0:1", "run-1:each-file:1:1"}


async def test_a_retry_of_the_same_step_is_not_a_duplicate(db):
    snapshot = await db.create_run(make_snapshot())
    current = await db.commit_boundary(
        snapshot, make_receipt(snapshot, outcome="failure", attempt=1)
    )
    current = await db.commit_boundary(current, make_receipt(current, attempt=2))
    assert len(await db.list_receipts("run-1")) == 2


# -- B-7: the fence, under a real race -------------------------------------


async def test_concurrent_boundaries_produce_one_winner(db):
    """Twenty writers from one loaded snapshot; exactly one may commit."""
    snapshot = await db.create_run(make_snapshot())

    async def attempt(index: int):
        return await db.commit_boundary(
            replace(snapshot, current_step_id=f"step-{index}"),
            make_receipt(snapshot, receipt_id=f"receipt-{index}", attempt=index + 1),
        )

    results = await asyncio.gather(*(attempt(i) for i in range(20)), return_exceptions=True)
    winners = [r for r in results if isinstance(r, RunSnapshot)]
    losers = [r for r in results if isinstance(r, SnapshotVersionConflict)]

    assert len(winners) == 1
    assert len(losers) == 19
    assert winners[0].version == 1
    assert len(await db.list_receipts("run-1")) == 1
    assert (await db.load_run("run-1")).version == 1


# -- B-8: cancellation -----------------------------------------------------


async def test_running_run_enters_cancelling(db):
    await db.create_run(make_snapshot())
    cancelled = await db.request_cancel(
        "run-1", expected_version=0, reason="operator asked", requested_by="user:jack"
    )
    assert cancelled.lifecycle is RunLifecycle.CANCELLING
    assert cancelled.version == 1
    assert cancelled.cancel_requested_at is not None
    reloaded = await db.load_run("run-1")
    assert reloaded.lifecycle is RunLifecycle.CANCELLING
    # Cancellation writes no receipt — the acknowledgement is the engine's.
    assert await db.list_receipts("run-1") == []


async def test_paused_run_cancels_immediately(db):
    await db.create_run(make_snapshot(lifecycle=RunLifecycle.PAUSED))
    cancelled = await db.request_cancel(
        "run-1", expected_version=0, reason="stale", requested_by="user:jack"
    )
    assert cancelled.lifecycle is RunLifecycle.CANCELLED
    assert cancelled.completed_at is not None


async def test_cancel_requires_the_current_version(db):
    snapshot = await db.create_run(make_snapshot())
    await db.commit_boundary(replace(snapshot, current_step_id="a"), make_receipt(snapshot))
    with pytest.raises(SnapshotVersionConflict):
        await db.request_cancel(
            "run-1", expected_version=0, reason="late", requested_by="user:jack"
        )


async def test_cancel_refuses_a_terminal_run(db):
    snapshot = await db.create_run(make_snapshot())
    done = await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.COMPLETED, completed_at=NOW + 1),
        make_receipt(snapshot),
    )
    with pytest.raises(IllegalLifecycleTransition):
        await db.request_cancel(
            "run-1", expected_version=done.version, reason="too late", requested_by="user:jack"
        )


async def test_cancel_of_an_unknown_run_conflicts(db):
    with pytest.raises(SnapshotVersionConflict):
        await db.request_cancel("nope", expected_version=0, reason="x", requested_by="y")


# -- §8.3: the redaction boundary ------------------------------------------


async def test_sensitive_value_never_lands_in_a_receipt(db):
    from src.playbooks.receipts import project_receipt

    snapshot = await db.create_run(make_snapshot())
    handle_inputs, handle_result = project_receipt(
        {"token": "hunter2"},
        {"api_key": "s3cr3t"},
        receipt_projection=("api_key",),
        sensitive_result_fields=("api_key",),
        run_id=snapshot.run_id,
    )
    receipt = make_receipt(snapshot, inputs=handle_inputs, result=handle_result)
    await db.commit_boundary(
        replace(snapshot, sensitive={handle_result["api_key"]: "s3cr3t"}), receipt
    )

    stored = (await db.list_receipts("run-1"))[0]
    serialized = json.dumps([stored.inputs, stored.result, stored.principal])
    assert "s3cr3t" not in serialized
    assert "hunter2" not in serialized
    assert stored.result["api_key"].startswith("sensitive:")


# -- retention -------------------------------------------------------------


async def test_purge_receipts_spares_a_live_run(db):
    snapshot = await db.create_run(make_snapshot())
    await db.commit_boundary(replace(snapshot, current_step_id="a"), make_receipt(snapshot))
    assert await db.purge_receipts(NOW + 10_000) == 0
    assert len(await db.list_receipts("run-1")) == 1


async def test_purge_receipts_collects_an_old_terminal_run(db):
    snapshot = await db.create_run(make_snapshot())
    await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.COMPLETED, completed_at=NOW),
        make_receipt(snapshot),
    )
    assert await db.purge_receipts(NOW - 1) == 0
    assert await db.purge_receipts(NOW + 1) == 1
    assert await db.list_receipts("run-1") == []
