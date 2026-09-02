"""Playbook V2 durable waits and retained pending events.

Package 3 child plan §10, and the wait half of §4.5.  The property under
test is that a wait is never visible in a state its run is not: registration
travels into ``commit_boundary`` as a ``WaitChangeSet`` and is applied on the
boundary's own connection, so there is no interval in which a run is
suspended and its wait is missing — nor one in which a wait outlives the
boundary that failed to write it.

Every concurrency case is parametrised over both backends.  On SQLite
``immediate()``'s per-adapter ``asyncio.Lock`` serialises callers, so a green
SQLite run proves the *result* is correct but not that the compare-and-set is
what enforced it; only PostgreSQL proves the fence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import playbook_artifacts, playbook_pending_events, playbook_waits
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import (
    DuplicateWait,
    PendingEventIntegrityError,
    PendingEventQuotaExceeded,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    WaitOwnershipViolation,
    WaitVersionMismatch,
)
from src.playbooks.waits import WaitChangeSet, WaitSpec, matches
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

ARTIFACT = "sha256:" + "1c" * 32
NOW = 1_000_000.0
HOUR = 3600.0
DAY = 86_400.0


# -- fixtures ---------------------------------------------------------------


@dataclass
class Event:
    """The minimum ``MatchableEvent`` — Package 4 supplies the real one."""

    event_type: str
    event_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(params=["sqlite", "postgres"])
async def db_factory(request, tmp_path):
    """Open adapters against one durable target, for the restart case.

    The factory is what makes ``test_wait_survives_a_process_restart`` a
    restart rather than a reset: the first adapter is closed outright and a
    second is opened against the same file/DSN, so nothing in memory carries
    across.
    """
    opened: list[Any] = []

    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        async def factory():
            database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
            await database.initialize()
            if not opened:
                await database.reset_for_tests()
            opened.append(database)
            return database

    else:
        path = str(tmp_path / "test.db")

        async def factory():
            database = Database(path)
            await database.initialize()
            opened.append(database)
            return database

    yield factory
    for database in opened:
        await database.close()


@pytest.fixture
async def db(db_factory):
    database = await db_factory()
    await seed_artifact(database, ARTIFACT)
    return database


async def seed_artifact(database, digest: str) -> None:
    """A run's ``artifact_sha256`` is a real FK on PostgreSQL."""
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
    step_id = overrides.get("step_id", "await-merge")
    iteration = overrides.get("iteration", -1)
    attempt = overrides.get("attempt", 1)
    base = {
        "receipt_id": f"receipt-{step_id}-{iteration}-{attempt}",
        "run_id": snapshot.run_id,
        "artifact_sha256": snapshot.artifact_sha256,
        "rule_id": snapshot.rule_id,
        "step_id": step_id,
        "step_kind": "wait",
        "outcome": "success",
        "started_at": NOW,
        "snapshot_version": snapshot.version + 1,
    }
    base.update(overrides)
    return StepReceipt(**base)


def make_wait(**overrides) -> WaitSpec:
    base = {
        "wait_id": "wait-1",
        "run_id": "run-1",
        "step_id": "await-merge",
        "kind": "event",
        "event_type": "pr.merged",
        "match": {"pr.number": 41},
        "deadline_at": NOW + DAY,
    }
    base.update(overrides)
    return WaitSpec(**base)


async def count_rows(database, table) -> int:
    async with database._engine.connect() as conn:
        return len((await conn.execute(select(table))).fetchall())


# -- the inert predicate ----------------------------------------------------


def test_match_requires_every_declared_path():
    spec = make_wait(match={"pr.number": 41, "pr.repo": "aq"})
    assert matches(spec, Event("pr.merged", "e1", {"pr": {"number": 41, "repo": "aq"}}))
    assert not matches(spec, Event("pr.merged", "e1", {"pr": {"number": 41}}))
    assert not matches(spec, Event("pr.merged", "e1", {"pr": {"number": 9, "repo": "aq"}}))


def test_match_distinguishes_absent_from_null():
    """An absent path never matches, even a required ``None``."""
    spec = make_wait(match={"pr.merged_by": None})
    assert matches(spec, Event("pr.merged", "e1", {"pr": {"merged_by": None}}))
    assert not matches(spec, Event("pr.merged", "e1", {"pr": {}}))


def test_match_requires_the_event_type():
    spec = make_wait(match={})
    assert matches(spec, Event("pr.merged", "e1", {}))
    assert not matches(spec, Event("pr.closed", "e1", {}))


def test_correlation_key_is_stable_across_key_order():
    first = make_wait(match={"a": 1, "b": 2})
    second = make_wait(wait_id="wait-2", match={"b": 2, "a": 1})
    assert first.correlation_key == second.correlation_key
    assert make_wait(match={"a": 2}).correlation_key != first.correlation_key


# -- B-9: register and claim ------------------------------------------------


async def test_register_and_claim(db):
    snapshot = await db.create_run(make_snapshot())
    advanced = await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.PAUSED, current_step_id="await-merge"),
        make_receipt(snapshot),
        WaitChangeSet(register=(make_wait(),)),
    )

    active = await db.list_active("run-1")
    assert [w.wait_id for w in active] == ["wait-1"]
    assert active[0].match == {"pr.number": 41}
    assert active[0].deadline_at == NOW + DAY

    claims = await db.claim_for_event(
        Event("pr.merged", "evt-9", {"pr": {"number": 41}}), now=NOW + HOUR
    )
    assert [c.wait_id for c in claims] == ["wait-1"]
    claim = claims[0]
    assert claim.run_id == "run-1"
    assert claim.step_id == "await-merge"
    assert claim.claimed_event_id == "evt-9"
    assert claim.claimed_at == NOW + HOUR
    assert claim.expired is False
    # The wait records the snapshot the run is suspended *on*, not the one it
    # was loaded from — a resume that disagrees is a wait_version_mismatch.
    assert claim.snapshot_version == advanced.version
    assert await db.list_active("run-1") == []


async def test_a_non_matching_event_claims_nothing(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(), 1)
    assert await db.claim_for_event(Event("pr.merged", "e", {"pr": {"number": 7}}), now=NOW) == []
    assert await db.claim_for_event(Event("pr.closed", "e", {"pr": {"number": 41}}), now=NOW) == []
    assert len(await db.list_active("run-1")) == 1


async def test_stale_snapshot_version_is_rejected_before_registration(db):
    await db.create_run(make_snapshot())

    with pytest.raises(WaitVersionMismatch) as caught:
        await db.register(make_wait(), 999)

    assert caught.value.code == "wait_version_mismatch"
    assert caught.value.wait_id == "wait-1"
    assert caught.value.run_id == "run-1"
    assert caught.value.expected == 1
    assert caught.value.actual == 999
    assert await db.list_active("run-1") == []
    assert (
        await db.claim_for_event(
            Event("pr.merged", "evt-stale", {"pr": {"number": 41}}), now=NOW
        )
        == []
    )


async def test_claim_limit_counts_successes_not_nonmatching_candidates(db):
    await db.create_run(make_snapshot())
    waits = (
        make_wait(wait_id="wait-1", step_id="step-1", match={"pr.number": 7}),
        make_wait(wait_id="wait-2", step_id="step-2", match={"pr.number": 8}),
        make_wait(wait_id="wait-3", step_id="step-3"),
        make_wait(wait_id="wait-4", step_id="step-4"),
        make_wait(wait_id="wait-5", step_id="step-5"),
    )
    for wait in waits:
        await db.register(wait, 1)

    # Force pagination across the wait_id tie-break so the first SQL-sized
    # page contains only same-type, nonmatching waits.
    async with db.immediate() as conn:
        await conn.execute(update(playbook_waits).values(created_at=NOW))

    claims = await db.claim_for_event(
        Event("pr.merged", "evt-9", {"pr": {"number": 41}}), now=NOW + HOUR, limit=2
    )

    assert [claim.wait_id for claim in claims] == ["wait-3", "wait-4"]
    assert [wait.wait_id for wait in await db.list_active("run-1")] == [
        "wait-1",
        "wait-2",
        "wait-5",
    ]


async def test_a_second_active_wait_for_a_step_is_rejected(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(), 1)
    with pytest.raises(DuplicateWait) as caught:
        await db.register(make_wait(wait_id="wait-2"), 1)
    assert caught.value.code == "duplicate_wait"
    assert caught.value.step_id == "await-merge"
    assert len(await db.list_active("run-1")) == 1


async def test_a_second_iteration_of_the_same_step_may_wait(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(iteration=0), 1)
    await db.register(make_wait(wait_id="wait-2", iteration=1), 1)
    assert len(await db.list_active("run-1")) == 2


async def test_a_cleared_wait_frees_the_step_for_a_new_one(db):
    snapshot = await db.create_run(make_snapshot())
    await db.register(make_wait(), 1)
    assert await db.clear_for_run("run-1") == 1
    await db.commit_boundary(snapshot, make_receipt(snapshot))
    await db.register(make_wait(wait_id="wait-2"), 2)
    assert [w.wait_id for w in await db.list_active("run-1")] == ["wait-2"]


async def test_claim_is_exactly_once_under_concurrency(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(), 1)
    event = Event("pr.merged", "evt-9", {"pr": {"number": 41}})

    results = await asyncio.gather(
        *(db.claim_for_event(event, now=NOW + HOUR) for _ in range(20))
    )
    assert sum(len(batch) for batch in results) == 1


# -- B-10: the boundary is atomic -------------------------------------------


async def test_wait_registration_and_snapshot_commit_are_atomic(db, monkeypatch):
    """Inject a failure *after* the wait insert; nothing may survive it."""
    snapshot = await db.create_run(make_snapshot())
    original = type(db).register

    async def exploding_register(self, wait, snapshot_version, *, conn=None):
        await original(self, wait, snapshot_version, conn=conn)
        raise RuntimeError("boom, after the insert")

    monkeypatch.setattr(type(db), "register", exploding_register)
    with pytest.raises(RuntimeError, match="boom"):
        await db.commit_boundary(
            replace(snapshot, lifecycle=RunLifecycle.PAUSED),
            make_receipt(snapshot),
            WaitChangeSet(register=(make_wait(),)),
        )
    monkeypatch.undo()

    reloaded = await db.load_run("run-1")
    assert reloaded.version == snapshot.version
    assert reloaded.lifecycle is RunLifecycle.RUNNING
    assert await db.list_receipts("run-1") == []
    assert await count_rows(db, playbook_waits) == 0


async def test_a_duplicate_registration_rolls_the_whole_boundary_back(db):
    snapshot = await db.create_run(make_snapshot())
    with pytest.raises(DuplicateWait):
        await db.commit_boundary(
            replace(snapshot, lifecycle=RunLifecycle.PAUSED),
            make_receipt(snapshot),
            WaitChangeSet(register=(make_wait(), make_wait(wait_id="wait-2"))),
        )
    assert (await db.load_run("run-1")).version == snapshot.version
    assert await db.list_receipts("run-1") == []
    assert await count_rows(db, playbook_waits) == 0


async def test_a_failed_cas_writes_no_wait(db):
    snapshot = await db.create_run(make_snapshot())
    stale = replace(snapshot, version=snapshot.version + 5)
    with pytest.raises(SnapshotVersionConflict):
        await db.commit_boundary(
            replace(stale, lifecycle=RunLifecycle.PAUSED),
            make_receipt(stale),
            WaitChangeSet(register=(make_wait(),)),
        )
    assert await count_rows(db, playbook_waits) == 0


async def test_a_boundary_cannot_register_a_wait_for_another_run(db):
    """A change set is scoped to the run whose CAS this boundary holds.

    Otherwise run-a's boundary opens a suspension on run-b while run-b stays
    at its own version — a wait outside the fence that is supposed to guard
    it, which a resume of run-b would then trip over.
    """
    run_a = await db.create_run(make_snapshot())
    await db.create_run(make_snapshot(run_id="run-b"))

    with pytest.raises(WaitOwnershipViolation) as caught:
        await db.commit_boundary(
            replace(run_a, lifecycle=RunLifecycle.PAUSED),
            make_receipt(run_a),
            WaitChangeSet(register=(make_wait(wait_id="wait-b", run_id="run-b"),)),
        )

    assert caught.value.code == "wait_ownership_violation"
    assert caught.value.wait_id == "wait-b"
    assert caught.value.owner_run_id == "run-b"
    assert await count_rows(db, playbook_waits) == 0
    assert (await db.load_run("run-1")).version == run_a.version
    assert await db.list_receipts("run-1") == []
    assert (await db.load_run("run-b")).version == 0


async def test_a_boundary_cannot_clear_another_runs_wait_by_id(db):
    """``clear_wait_ids`` is constrained to waits the boundary run owns."""
    run_a = await db.create_run(make_snapshot())
    run_b = await db.create_run(make_snapshot(run_id="run-b"))
    await db.commit_boundary(
        replace(run_b, lifecycle=RunLifecycle.PAUSED),
        make_receipt(run_b, receipt_id="receipt-b"),
        WaitChangeSet(register=(make_wait(wait_id="wait-b", run_id="run-b"),)),
    )

    with pytest.raises(WaitOwnershipViolation) as caught:
        await db.commit_boundary(
            replace(run_a, current_step_id="a"),
            make_receipt(run_a),
            WaitChangeSet(clear_wait_ids=("wait-b",)),
        )

    assert caught.value.wait_id == "wait-b"
    assert caught.value.owner_run_id == "run-b"
    assert [w.wait_id for w in await db.list_active("run-b")] == ["wait-b"]
    assert (await db.load_run("run-1")).version == run_a.version
    assert await db.list_receipts("run-1") == []


async def test_one_boundary_clears_a_wait_and_opens_the_next(db):
    """clear → register in one boundary must not trip the partial index."""
    snapshot = await db.create_run(make_snapshot())
    first = await db.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.PAUSED),
        make_receipt(snapshot),
        WaitChangeSet(register=(make_wait(),)),
    )
    second = await db.commit_boundary(
        replace(first, lifecycle=RunLifecycle.RUNNING),
        make_receipt(first, attempt=2),
        WaitChangeSet(
            clear_wait_ids=("wait-1",), register=(make_wait(wait_id="wait-2"),)
        ),
    )
    assert [w.wait_id for w in await db.list_active("run-1")] == ["wait-2"]
    assert second.version == first.version + 1


# -- B-11: the exit gate in miniature ---------------------------------------


async def test_wait_survives_a_process_restart(db_factory):
    database = await db_factory()
    await seed_artifact(database, ARTIFACT)
    snapshot = await database.create_run(
        make_snapshot(bindings={"ensure-task": {"task_id": "t-1"}})
    )
    committed = await database.commit_boundary(
        replace(snapshot, lifecycle=RunLifecycle.PAUSED, current_step_id="await-merge"),
        make_receipt(snapshot),
        WaitChangeSet(register=(make_wait(),)),
    )
    await database.close()

    restarted = await db_factory()
    claims = await restarted.claim_for_event(
        Event("pr.merged", "evt-9", {"pr": {"number": 41}}), now=NOW + HOUR
    )
    assert [c.wait_id for c in claims] == ["wait-1"]
    assert claims[0].snapshot_version == committed.version

    reloaded = await restarted.load_run("run-1")
    assert reloaded.version == committed.version
    assert reloaded.bindings == {"ensure-task": {"task_id": "t-1"}}
    assert reloaded.loop == committed.loop
    assert reloaded == committed


# -- B-12: expiry and clearing ----------------------------------------------


async def test_expire_due_claims_only_past_deadlines(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(wait_id="due", step_id="a", deadline_at=NOW - 1), 1)
    await db.register(make_wait(wait_id="later", step_id="b", deadline_at=NOW + DAY), 1)
    await db.register(make_wait(wait_id="never", step_id="c", deadline_at=None), 1)

    expired = await db.expire_due(NOW)
    assert [c.wait_id for c in expired] == ["due"]
    assert expired[0].expired is True
    assert expired[0].claimed_event_id is None
    assert expired[0].claimed_at == NOW
    assert sorted(w.wait_id for w in await db.list_active("run-1")) == ["later", "never"]
    assert await db.expire_due(NOW) == []


async def test_expire_is_exactly_once_under_concurrency(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(deadline_at=NOW - 1), 1)
    results = await asyncio.gather(*(db.expire_due(NOW) for _ in range(20)))
    assert sum(len(batch) for batch in results) == 1


async def test_clear_for_run_deactivates_every_wait(db):
    await db.create_run(make_snapshot())
    await db.create_run(make_snapshot(run_id="run-2"))
    await db.register(make_wait(step_id="a"), 1)
    await db.register(make_wait(wait_id="wait-2", step_id="b"), 1)
    await db.register(make_wait(wait_id="wait-3", run_id="run-2"), 1)

    assert await db.clear_for_run("run-1") == 2
    assert await db.list_active("run-1") == []
    assert [w.wait_id for w in await db.list_active("run-2")] == ["wait-3"]
    # Already cleared — a second call is a no-op, not a second clear.
    assert await db.clear_for_run("run-1") == 0


async def test_a_cleared_wait_is_not_claimable(db):
    await db.create_run(make_snapshot())
    await db.register(make_wait(), 1)
    await db.clear_for_run("run-1")
    event = Event("pr.merged", "evt-9", {"pr": {"number": 41}})
    assert await db.claim_for_event(event, now=NOW) == []


# -- B-13: pending events ---------------------------------------------------


async def retain(db, **overrides) -> str | None:
    base = {
        "playbook_id": "task-review",
        "scope": "system",
        "scope_identifier": "",
        "event_type": "task.completed",
        "event": {"task": {"id": "t-1"}},
        "event_id": "evt-1",
        "dedup_key": "task-review:t-1",
        "reason": "stale_contract",
        "now": NOW,
        "ttl_seconds": 7 * DAY,
    }
    base.update(overrides)
    return await db.retain_pending_event(**base)


async def test_pending_event_is_retained_with_its_ttl(db):
    pending_id = await retain(db)
    assert pending_id
    [row] = await db.list_pending_events(playbook_id="task-review")
    assert row["pending_event_id"] == pending_id
    assert row["event_type"] == "task.completed"
    assert row["event"] == {"task": {"id": "t-1"}}
    assert row["reason"] == "stale_contract"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert row["received_at"] == NOW
    assert row["expires_at"] == NOW + 7 * DAY


async def test_pending_event_is_deduplicated_by_the_index(db):
    first = await retain(db)
    assert await retain(db, event_id="evt-2", now=NOW + 1) is None
    assert len(await db.list_pending_events(playbook_id="task-review")) == 1
    # Resolving the first frees the dedup key for the next occurrence.
    await db.resolve_pending_event(first, resolution="discarded", resolved_by="op", now=NOW + 2)
    assert await retain(db, event_id="evt-2", now=NOW + 3) is not None


async def test_pending_event_integrity_failure_is_not_reported_as_deduplication(db):
    with pytest.raises(PendingEventIntegrityError) as caught:
        await retain(db, dedup_key="unique", reason="not-a-retention-reason")

    assert caught.value.code == "pending_event_integrity_error"
    assert caught.value.playbook_id == "task-review"
    assert isinstance(caught.value.__cause__, IntegrityError)
    assert await db.list_pending_events(playbook_id="task-review") == []


async def test_an_empty_dedup_key_never_deduplicates(db):
    assert await retain(db, dedup_key="") is not None
    assert await retain(db, dedup_key="", event_id="evt-2") is not None
    assert len(await db.list_pending_events(playbook_id="task-review")) == 2


async def test_pending_events_replay_in_arrival_order(db):
    ids = [
        await retain(db, dedup_key=f"k{i}", event_id=f"evt-{i}", now=NOW + i)
        for i in range(5)
    ]
    rows = await db.list_pending_events(playbook_id="task-review")
    assert [row["pending_event_id"] for row in rows] == ids


async def test_list_pending_events_hides_resolved_by_default(db):
    pending_id = await retain(db)
    await db.resolve_pending_event(
        pending_id, resolution="dispatched", resolved_by="op", now=NOW + 1
    )
    assert await db.list_pending_events(playbook_id="task-review") == []
    [row] = await db.list_pending_events(playbook_id="task-review", include_resolved=True)
    assert row["resolution"] == "dispatched"
    assert row["resolved_by"] == "op"
    assert row["resolved_at"] == NOW + 1


async def test_resolve_is_exactly_once(db):
    pending_id = await retain(db)
    outcomes = await asyncio.gather(
        *(
            db.resolve_pending_event(
                pending_id, resolution="dispatched", resolved_by=f"op-{i}", now=NOW + 1
            )
            for i in range(10)
        )
    )
    assert sum(1 for ok in outcomes if ok) == 1


async def test_pending_event_quota_is_enforced(db):
    db.set_playbook_pending_event_quota(3)
    for i in range(3):
        assert await retain(db, dedup_key=f"k{i}", event_id=f"evt-{i}") is not None
    with pytest.raises(PendingEventQuotaExceeded) as caught:
        await retain(db, dedup_key="k9", event_id="evt-9")
    assert caught.value.code == "pending_event_quota_exceeded"
    assert caught.value.playbook_id == "task-review"
    assert caught.value.limit == 3
    assert await count_rows(db, playbook_pending_events) == 3
    # The quota counts unresolved events only — resolving one makes room.
    rows = await db.list_pending_events(playbook_id="task-review")
    await db.resolve_pending_event(
        rows[0]["pending_event_id"], resolution="discarded", resolved_by="op", now=NOW + 1
    )
    assert await retain(db, dedup_key="k9", event_id="evt-9") is not None


async def test_the_quota_is_per_playbook(db):
    db.set_playbook_pending_event_quota(1)
    assert await retain(db) is not None
    assert await retain(db, playbook_id="other", dedup_key="other:1") is not None


async def test_purge_pending_events_marks_expired_then_collects_after_retention(db):
    resolved = await retain(db, dedup_key="k1", event_id="e1")
    await db.resolve_pending_event(
        resolved, resolution="discarded", resolved_by="op", now=NOW + 1
    )
    expired = await retain(db, dedup_key="k2", event_id="e2", ttl_seconds=1)
    live = await retain(db, dedup_key="k3", event_id="e3", ttl_seconds=7 * DAY)

    first = await db.purge_pending_events(NOW + HOUR, resolved_before=NOW)
    assert first.expired == 1
    assert first.purged == 0
    retained = await db.list_pending_events(playbook_id="task-review", include_resolved=True)
    assert {row["pending_event_id"] for row in retained} == {resolved, expired, live}
    expired_row = next(row for row in retained if row["pending_event_id"] == expired)
    assert expired_row["resolution"] == "expired"
    assert expired_row["resolved_by"] == "retention_sweep"
    assert expired_row["resolved_at"] == NOW + HOUR

    second = await db.purge_pending_events(
        NOW + HOUR + DAY + 1, resolved_before=NOW + HOUR + 1
    )
    assert second.expired == 0
    assert second.purged == 2
    remaining = await db.list_pending_events(playbook_id="task-review", include_resolved=True)
    assert [row["pending_event_id"] for row in remaining] == [live]


# -- the column defaults are usable -----------------------------------------


async def test_an_insert_may_rely_on_the_wait_column_defaults(db):
    """A pre-quoted ``server_default`` stores the quotes and fails the CHECK.

    ``state`` is constrained by ``ck_playbook_waits_state``, so a default of
    ``'''active'''`` does not merely store junk — it makes every insert that
    omits the column fail outright.  This is the shape
    ``tests/test_migration_string_defaults.py`` guards statically.
    """
    await db.create_run(make_snapshot())
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_waits).values(
                wait_id="bare",
                run_id="run-1",
                step_id="await-merge",
                kind="event",
                snapshot_version=1,
                created_at=NOW,
            )
        )
    [wait] = await db.list_active("run-1")
    assert wait.wait_id == "bare"
    assert wait.event_type == ""
    assert wait.match == {}
    assert wait.iteration == -1


async def test_an_insert_may_rely_on_the_pending_event_column_defaults(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_pending_events).values(
                pending_event_id="bare",
                playbook_id="task-review",
                event_type="task.completed",
                reason="disabled",
                received_at=NOW,
                expires_at=NOW + DAY,
            )
        )
    [row] = await db.list_pending_events(playbook_id="task-review")
    assert row["scope"] == "system"
    assert row["scope_identifier"] == ""
    assert row["event"] == {}
    assert row["dedup_key"] == ""
    assert row["attempts"] == 0
