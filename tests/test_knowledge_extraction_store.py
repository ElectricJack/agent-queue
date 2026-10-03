"""Real PostgreSQL race, fenced lease, crash replay and independent budget checks."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import metadata
from src.knowledge.extraction_schema import EXTRACTION_TABLE_NAMES
from src.knowledge.extraction_store import ExtractionStore, utc_day
from src.records.models import RecordError
from tests.record_helpers import seed_project


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database()
    async with db.immediate() as conn:
        await conn.run_sync(
            lambda sync: metadata.create_all(
                sync, tables=[metadata.tables[name] for name in EXTRACTION_TABLE_NAMES]
            )
        )
    for scope in ("p", "q", "r"):
        await seed_project(db, scope)
        async with db.immediate() as conn:
            await db.ensure_record_scope_on(project_id=scope, conn=conn)
    return db


@pytest.fixture
def now():
    return [datetime(2026, 10, 3, 12, tzinfo=UTC)]


@pytest.fixture
def store(now):
    return ExtractionStore(clock=lambda: now[0])


def receipt(event_id=1, *, scope="project:p", actor="worker:a", artifact="artifact:input"):
    return dict(
        event_id=event_id,
        attempt_id="attempt:a",
        artifact_id=artifact,
        source_scope=scope,
        actor_id=actor,
    )


async def enqueue(db, store, *, source="source:a", scope="project:p", inputs=None, **changes):
    values = dict(
        scope_key=scope,
        source_identity=source,
        source_sha256="a" * 64,
        extractor_version="extractor:1",
        policy_version="policy:1",
        inputs=[receipt(scope=scope)] if inputs is None else inputs,
    )
    values.update(changes)
    async with db.immediate() as conn:
        return await store.enqueue_on(**values, conn=conn)


async def claim(db, store, *scopes, **kwargs):
    async with db.immediate() as conn:
        return await store.claim_due_on(scope_keys=scopes or ["project:p"], conn=conn, **kwargs)


async def configure(db, store, *, scope="project:p", feature="extraction", money=100, tokens=1000):
    async with db.immediate() as conn:
        await store.configure_budget_on(
            scope_key=scope, feature=feature, limit_microusd=money, token_limit=tokens, conn=conn
        )


async def reserve(db, store, job, *, feature="extraction", money=50, tokens=500):
    async with db.immediate() as conn:
        return await store.reserve_on(
            job["job_id"],
            job["lease_token"],
            feature=feature,
            estimated_microusd=money,
            estimated_tokens=tokens,
            conn=conn,
        )


async def begin(db, store, job, *, operation="operation:a"):
    async with db.immediate() as conn:
        return await store.begin_operation_on(
            job["job_id"], job["lease_token"], provider_operation_id=operation, conn=conn
        )


async def budget(db, store, scope="project:p", feature="extraction", period=None):
    async with db.immediate() as conn:
        query = select(store.budgets).where(
            store.budgets.c.scope_key == scope, store.budgets.c.feature == feature
        )
        if period:
            query = query.where(store.budgets.c.period_start == period)
        return dict((await conn.execute(query)).mappings().one())


async def test_concurrent_capture_replay_keeps_exact_inputs_and_versions(db, store):
    left, right = await asyncio.gather(enqueue(db, store), enqueue(db, store))
    assert left["job_id"] == right["job_id"]
    variants = [
        await enqueue(db, store, **change)
        for change in (
            {"scope": "project:q"},
            {"source_sha256": "b" * 64},
            {"extractor_version": "extractor:2"},
            {"policy_version": "policy:2"},
        )
    ]
    assert len({left["job_id"], *(row["job_id"] for row in variants)}) == 5
    async with db.immediate() as conn:
        saved = await store.inputs_on(left["job_id"], conn=conn)
        assert saved == [{"job_id": left["job_id"], "input_ordinal": 0, **receipt()}]
    with pytest.raises(RecordError, match="extraction.input_conflict"):
        await enqueue(db, store, inputs=[receipt(actor="different")])


async def test_capture_receipts_and_monotonic_checkpoint_share_rollback(db, store):
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await store.enqueue_on(
                scope_key="project:p",
                source_identity="source:a",
                source_sha256="a" * 64,
                extractor_version="v1",
                policy_version="p1",
                inputs=[receipt()],
                conn=conn,
            )
            await store.advance_checkpoint_on(
                consumer_id="capture", scope_key="project:p", last_event_id=10, conn=conn
            )
            raise RuntimeError("capture crash")
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(store.jobs)) == 0
        assert await conn.scalar(select(func.count()).select_from(store.checkpoints)) == 0

    async def advance(value):
        async with db.immediate() as conn:
            await store.advance_checkpoint_on(
                consumer_id="capture", scope_key="project:p", last_event_id=value, conn=conn
            )

    await asyncio.gather(advance(12), advance(7), advance(10))
    async with db.immediate() as conn:
        assert await conn.scalar(select(store.checkpoints.c.last_event_id)) == 12


@pytest.mark.parametrize("inputs", [[], [receipt()] * 9, [receipt(scope="project:q")]])
async def test_bounded_same_scope_input_receipts(db, store, inputs):
    with pytest.raises(RecordError):
        await enqueue(db, store, inputs=inputs)


async def test_claims_serialize_one_in_flight_and_bound_page(db, store):
    for scope in ("project:p", "project:q", "project:r"):
        await enqueue(db, store, scope=scope)
        await enqueue(db, store, scope=scope, source="second")
    left, right = await asyncio.gather(
        claim(db, store, "project:p", "project:q", "project:r", limit=50),
        claim(db, store, "project:p", "project:q", "project:r", limit=50),
    )
    assert 1 <= len(left) <= 2
    assert len(right) <= 2
    assert len({j["scope_key"] for j in left + right}) == len(left + right)
    remaining = await claim(db, store, "project:p", "project:q", "project:r")
    assert len(left + right + remaining) == 3
    assert await claim(db, store, "project:p", "project:q", "project:r") == []


async def test_claim_skips_a_scope_locked_on_another_connection(db, store):
    await enqueue(db, store)
    async with db.immediate() as conn:
        assert len(await store.claim_due_on(scope_keys=["project:p"], conn=conn)) == 1
        assert await asyncio.wait_for(claim(db, store), timeout=5) == []


async def test_crash_before_payment_releases_lease_and_fences_old_worker(db, store, now):
    await enqueue(db, store)
    (first,) = await claim(db, store)
    assert first["lease_until"] == now[0] + timedelta(seconds=30)
    now[0] += timedelta(seconds=30)
    (replay,) = await claim(db, store)
    assert replay["job_id"] == first["job_id"]
    assert replay["attempts"] == 2
    assert replay["lease_token"] != first["lease_token"]
    with pytest.raises(RecordError, match="extraction.stale_lease"):
        async with db.immediate() as conn:
            await store.finish_on(
                first["job_id"], first["lease_token"], state="cancelled", conn=conn
            )
    async with db.immediate() as conn:
        await store.renew_on(replay["job_id"], replay["lease_token"], conn=conn)
        assert (await store.get_job_on(first["job_id"], conn=conn))["state"] == "leased"


async def test_retry_delay_is_bounded_and_missing_provider_keeps_inputs(db, store, now):
    await enqueue(db, store)
    (job,) = await claim(db, store)
    async with db.immediate() as conn:
        await conn.execute(
            update(store.jobs).where(store.jobs.c.job_id == job["job_id"]).values(attempts=10000)
        )
        assert (
            await store.finish_on(
                job["job_id"],
                job["lease_token"],
                state="retry",
                error_code="provider_unavailable",
                conn=conn,
            )
            == "retry"
        )
        saved = await store.get_job_on(job["job_id"], conn=conn)
        assert saved["available_at"] == now[0] + timedelta(seconds=300)
        assert len(await store.inputs_on(job["job_id"], conn=conn)) == 1
    assert await claim(db, store) == []
    now[0] += timedelta(seconds=300)
    assert len(await claim(db, store)) == 1


async def test_defaults_disable_even_zero_estimate_calls(db, store):
    await enqueue(db, store)
    (job,) = await claim(db, store)
    with pytest.raises(RecordError, match="extraction.budget_disabled"):
        await reserve(db, store, job, money=0, tokens=0)
    async with db.immediate() as conn:
        await store.configure_budget_on(scope_key="project:p", feature="extraction", conn=conn)
    row = await budget(db, store)
    assert row["limit_microusd"] == row["reserved_microusd"] == row["spent_tokens"] == 0


@pytest.mark.parametrize("money,tokens", [(101, 1), (1, 1001), (1, 8001)])
async def test_money_and_token_limits_each_block_admission(db, store, money, tokens):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    with pytest.raises(RecordError):
        await reserve(db, store, job, money=money, tokens=tokens)
    assert (await budget(db, store))["reserved_microusd"] == 0


async def test_concurrent_reservation_is_exactly_once_and_replay_checks_estimate(db, store):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    left, right = await asyncio.gather(reserve(db, store, job), reserve(db, store, job))
    assert left["reservation_id"] == right["reservation_id"]
    assert (await budget(db, store))["reserved_microusd"] == 50
    with pytest.raises(RecordError, match="extraction.reservation_conflict"):
        await reserve(db, store, job, money=51)


async def test_pending_reservation_counts_against_second_job(db, store):
    await configure(db, store, money=75)
    await enqueue(db, store)
    await enqueue(db, store, source="second")
    (first,) = await claim(db, store)
    await reserve(db, store, first)
    async with db.immediate() as conn:
        await store.finish_on(first["job_id"], first["lease_token"], state="retry", conn=conn)
    (second,) = await claim(db, store)
    with pytest.raises(RecordError, match="extraction.budget_exceeded"):
        await reserve(db, store, second)


async def test_paid_operation_started_once_and_unknown_retains_both_reservations(db, store, now):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    started = await asyncio.gather(begin(db, store, job), begin(db, store, job))
    assert sorted(started) == [False, True]
    async with db.immediate() as conn:
        await store.unknown_on(job["job_id"], job["lease_token"], conn=conn)
        assert (await store.get_job_on(job["job_id"], conn=conn))["state"] == "quarantined"
        assert (await store.reservation_on(job["job_id"], conn=conn))["state"] == "unknown"
    now[0] += timedelta(days=1)
    assert await claim(db, store) == []
    row = await budget(db, store)
    assert (row["reserved_microusd"], row["reserved_tokens"], row["spent_microusd"]) == (50, 500, 0)


async def test_expired_paid_operation_is_quarantined_instead_of_recalled(db, store, now):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    await begin(db, store, job)
    now[0] += timedelta(seconds=31)
    assert await claim(db, store) == []
    async with db.immediate() as conn:
        row = await store.get_job_on(job["job_id"], conn=conn)
        assert row["state"] == "quarantined"
        assert row["error_code"] == "provider_outcome_unknown"
        assert (await store.reservation_on(job["job_id"], conn=conn))["state"] == "unknown"
    assert (await budget(db, store))["reserved_microusd"] == 50


async def test_cancel_after_paid_call_does_not_release_unknown_cost(db, store):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    await begin(db, store, job)
    async with db.immediate() as conn:
        assert (
            await store.finish_on(job["job_id"], job["lease_token"], state="cancelled", conn=conn)
            == "quarantined"
        )
    assert (await budget(db, store))["reserved_tokens"] == 500


async def test_cancel_before_call_releases_once_and_preserves_receipts(db, store):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    async with db.immediate() as conn:
        await store.finish_on(job["job_id"], job["lease_token"], state="cancelled", conn=conn)
        assert (await store.reservation_on(job["job_id"], conn=conn))["state"] == "released"
        assert len(await store.inputs_on(job["job_id"], conn=conn)) == 1
    assert (await budget(db, store))["reserved_tokens"] == 0
    with pytest.raises(RecordError, match="extraction.stale_lease"):
        async with db.immediate() as conn:
            await store.finish_on(job["job_id"], job["lease_token"], state="cancelled", conn=conn)


async def test_saved_output_replays_after_crash_and_settlement_is_idempotent(db, store, now):
    await enqueue(db, store)
    await configure(db, store)
    (original,) = await claim(db, store)
    await reserve(db, store, original)
    await begin(db, store, original)
    async with db.immediate() as conn:
        await store.save_output_on(
            original["job_id"], original["lease_token"], artifact_id="artifact:output", conn=conn
        )
    now[0] += timedelta(seconds=31)
    (job,) = await claim(db, store)
    assert job["result_artifact_id"] == "artifact:output"
    assert not await begin(db, store, job)

    async def settle():
        async with db.immediate() as conn:
            return await store.settle_on(
                job["job_id"], job["lease_token"], actual_microusd=40, actual_tokens=450, conn=conn
            )

    left, right = await asyncio.gather(settle(), settle())
    assert left == right
    with pytest.raises(RecordError, match="extraction.settlement_conflict"):
        async with db.immediate() as conn:
            await store.settle_on(
                job["job_id"], job["lease_token"], actual_microusd=41, actual_tokens=450, conn=conn
            )
    async with db.immediate() as conn:
        assert (
            await store.finish_on(job["job_id"], job["lease_token"], state="succeeded", conn=conn)
            == "succeeded"
        )
    now[0] += timedelta(seconds=60)
    async with db.immediate() as conn:
        replay = await ExtractionStore(clock=lambda: now[0]).settle_on(
            job["job_id"],
            job["lease_token"],
            actual_microusd=40,
            actual_tokens=450,
            conn=conn,
        )
        assert replay == left
    row = await budget(db, store)
    assert (row["spent_microusd"], row["spent_tokens"], row["reserved_tokens"]) == (40, 450, 0)
    assert await claim(db, store) == []


@pytest.mark.parametrize("actual_money,actual_tokens", [(150, 500), (50, 1500), (60, 500)])
async def test_overage_is_recorded_in_full_and_opens_durable_circuit(
    db,
    store,
    now,
    actual_money,
    actual_tokens,
):
    await configure(db, store)
    await enqueue(db, store)
    await enqueue(db, store, source="second")
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    await begin(db, store, job)
    async with db.immediate() as conn:
        await store.save_output_on(job["job_id"], job["lease_token"], artifact_id="out", conn=conn)
        await store.settle_on(
            job["job_id"],
            job["lease_token"],
            actual_microusd=actual_money,
            actual_tokens=actual_tokens,
            conn=conn,
        )
        await store.finish_on(job["job_id"], job["lease_token"], state="succeeded", conn=conn)
    row = await budget(db, store)
    assert (row["spent_microusd"], row["spent_tokens"]) == (actual_money, actual_tokens)
    assert row["circuit_open"]
    # Increasing limits never silently erases an opened circuit.
    await configure(db, store, money=1000, tokens=10000)
    (second,) = await claim(db, store)
    with pytest.raises(RecordError, match="extraction.budget_circuit_open"):
        await reserve(db, store, second)
    now[0] += timedelta(days=1)
    await configure(db, store, money=1000, tokens=10000)
    (second,) = await claim(db, store)
    with pytest.raises(RecordError, match="extraction.budget_circuit_open"):
        await reserve(db, store, second)


async def test_scope_feature_and_utc_day_budgets_are_independent(db, store, now):
    for scope, feature in (
        ("project:p", "extraction"),
        ("project:q", "extraction"),
        ("project:p", "consolidation"),
    ):
        await configure(db, store, scope=scope, feature=feature)
    await enqueue(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    assert (await budget(db, store, "project:q"))["reserved_microusd"] == 0
    assert (await budget(db, store, feature="consolidation"))["reserved_microusd"] == 0
    yesterday = utc_day(now[0])
    now[0] += timedelta(days=1)
    await configure(db, store)
    assert (await budget(db, store, period=utc_day(now[0])))["reserved_microusd"] == 0
    assert (await budget(db, store, period=yesterday))["reserved_microusd"] == 50
    # An unstarted reservation cannot authorize tomorrow's request yesterday.
    # Re-reserving moves only the unstarted allowance, retaining its identity.
    now[0] -= timedelta(days=1)
    async with db.immediate() as conn:
        await store.renew_on(job["job_id"], job["lease_token"], conn=conn)
    now[0] = yesterday + timedelta(days=1)
    async with db.immediate() as conn:
        await conn.execute(
            update(store.jobs)
            .where(store.jobs.c.job_id == job["job_id"])
            .values(lease_until=now[0] + timedelta(seconds=30))
        )
    with pytest.raises(RecordError, match="extraction.reservation_expired"):
        await begin(db, store, job)
    original = await reserve(db, store, job)
    assert original["period_start"] == utc_day(now[0])
    assert (await budget(db, store, period=yesterday))["reserved_microusd"] == 0
    assert (await budget(db, store, period=utc_day(now[0])))["reserved_microusd"] == 50
    assert await begin(db, store, job)


async def test_usage_crossing_midnight_settles_original_reservation_day(db, store, now):
    now[0] = utc_day(now[0]) + timedelta(hours=23, minutes=59, seconds=50)
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    await begin(db, store, job)
    original_day = utc_day(now[0])
    now[0] += timedelta(seconds=15)
    await configure(db, store)
    async with db.immediate() as conn:
        await store.settle_on(
            job["job_id"], job["lease_token"], actual_microusd=40, actual_tokens=450, conn=conn
        )
    assert (await budget(db, store, period=original_day))["spent_microusd"] == 40
    assert (await budget(db, store, period=utc_day(now[0])))["spent_microusd"] == 0


async def test_settlement_and_success_rollback_with_caller_proposals(db, store):
    await enqueue(db, store)
    await configure(db, store)
    (job,) = await claim(db, store)
    await reserve(db, store, job)
    await begin(db, store, job)
    async with db.immediate() as conn:
        await store.save_output_on(job["job_id"], job["lease_token"], artifact_id="out", conn=conn)
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await store.settle_on(
                job["job_id"], job["lease_token"], actual_microusd=40, actual_tokens=450, conn=conn
            )
            await store.finish_on(job["job_id"], job["lease_token"], state="succeeded", conn=conn)
            raise RuntimeError("proposal transaction crashed")
    async with db.immediate() as conn:
        assert (await store.get_job_on(job["job_id"], conn=conn))["state"] == "leased"
        assert (await store.reservation_on(job["job_id"], conn=conn))["state"] == "reserved"
    assert (await budget(db, store))["reserved_microusd"] == 50
    assert (await budget(db, store))["spent_microusd"] == 0


async def test_direct_database_checks_reject_wrong_scope_and_invalid_counters(db, store):
    job = await enqueue(db, store)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                insert(store.inputs).values(
                    job_id=job["job_id"], input_ordinal=1, **receipt(scope="project:q")
                )
            )
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                insert(store.budgets).values(
                    scope_key="project:p", feature="x", period_start=store.clock(), spent_tokens=-1
                )
            )
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                update(store.jobs)
                .where(store.jobs.c.job_id == job["job_id"])
                .values(state="leased", lease_token=uuid4())
            )


@pytest.mark.migration
async def test_migration_baseline_replay_empty_rollback_and_data_guard(db, store):
    migration = importlib.import_module("migrations.versions.a00000000063_knowledge_extraction")

    def run(sync, action):
        with Operations.context(MigrationContext.configure(sync)):
            getattr(migration, action)()

    async with db.immediate() as conn:
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "downgrade"))
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert not set(EXTRACTION_TABLE_NAMES) & set(tables)
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(EXTRACTION_TABLE_NAMES) <= set(tables)
    await enqueue(db, store)
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with db.immediate() as conn:
            await conn.run_sync(lambda sync: run(sync, "downgrade"))
    async with db.immediate() as conn:
        assert set(EXTRACTION_TABLE_NAMES) <= set(
            await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        )
