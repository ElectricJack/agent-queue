"""Exact retained capture, lost-wake reconciliation, rollback and role separation."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.database.tables import events, record_source_artifacts, task_completion_records
from src.knowledge.capture import CaptureInput, CONSUMER, partition_inputs
from src.records.models import RecordError
from tests.record_helpers import generation_setup, seed_task


@pytest.fixture
async def setup(reuse_database, tmp_path):
    return await generation_setup(reuse_database, tmp_path)


def source(**changes):
    return CaptureInput(
        **{
            "source_identity": "event:1",
            "scope_key": "project:p",
            "actor_id": "worker:a",
            "permission_key": "worker:read",
            "source_policy": "1",
            "provider_id": "fake",
            "content": "Synthetic observed error log\nsecond line\n",
            "evidence_type": "observed",
            "event_id": 1,
            "attempt_id": "attempt:a",
            "line_end": 2,
            **changes,
        }
    )


async def test_capture_dedup_preserves_exact_bytes_attempt_range_and_event_after_retention(setup):
    db, _, _, store, capture = setup
    item = source()
    first = (await capture.capture([item]))[0]
    assert (await capture.capture([item]))[0]["job_id"] == first["job_id"]
    async with db.immediate() as conn:
        await conn.execute(delete(events))
        receipt = (await store.inputs_on(first["job_id"], conn=conn))[0]
        raw, artifact = await capture.artifacts().read_on(
            receipt["artifact_id"], scope_key="project:p", conn=conn
        )
        import json

        retained = json.loads(raw)
        assert retained["content"] == item.content
        assert retained["attempt_id"] == item.attempt_id
        assert retained["evidence_type"] == "observed"
        assert retained["line_start"] == 1 and retained["line_end"] == 2
        assert retained["event_id"] == 1
        assert artifact["scope_key"] == "project:p"
        assert await conn.scalar(select(func.count()).select_from(store.jobs)) == 1


async def test_capture_crash_rolls_back_receipts_and_cursor_then_replays(setup):
    db, _, _, store, capture = setup
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await capture.capture_on([source()], conn=conn)
            await store.advance_checkpoint_on(
                consumer_id=CONSUMER, scope_key="project:p", last_event_id=9, conn=conn
            )
            raise RuntimeError("crash after cursor update before commit")
    async with db.immediate() as conn:
        for table in (store.jobs, store.inputs, store.checkpoints, record_source_artifacts):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
    assert len(await capture.capture([source()])) == 1


async def test_mixed_scope_role_permissions_policy_provider_are_separate_jobs(setup):
    db, _, _, store, capture = setup
    base = source()
    variants = [
        base,
        replace(base, source_identity="event:2", scope_key="project:q"),
        replace(base, source_identity="event:3", actor_id="supervisor:a"),
        replace(base, source_identity="event:4", permission_key="private-log"),
        replace(base, source_identity="event:5", source_policy="restricted"),
        replace(base, source_identity="event:6", provider_id="other"),
    ]
    assert len(partition_inputs(variants)) == 6
    jobs = await capture.capture(variants)
    async with db.immediate() as conn:
        for job in jobs:
            receipts = await store.inputs_on(job["job_id"], conn=conn)
            assert len(receipts) == 1
            assert receipts[0]["source_scope"] == job["scope_key"]


async def test_missing_source_is_quarantined_with_a_retained_unavailability_receipt(setup):
    db, _, _, store, capture = setup
    job = (await capture.capture([source(content=None)]))[0]
    assert job["state"] == "quarantined" and job["error_code"] == "source_unavailable"
    async with db.immediate() as conn:
        receipt = (await store.inputs_on(job["job_id"], conn=conn))[0]
        raw, _ = await capture.artifacts().read_on(
            receipt["artifact_id"], scope_key=job["scope_key"], conn=conn
        )
        assert b'"content":null' in raw
        assert await store.claim_due_on(scope_keys=[job["scope_key"]], conn=conn) == []


async def completion(db, task_id="t", body="Completion claim"):
    await seed_task(db, task_id)
    row = dict(id=str(uuid4()), task_id=task_id, outcome="pass", completed_at=10, summary=body)
    async with db.immediate() as conn:
        await conn.execute(insert(task_completion_records).values(**row))
    return row


async def test_reconciliation_captures_without_any_bus_wake_and_repeated_late_events_dedup(setup):
    db, _, _, store, capture = setup
    row = await completion(db)
    assert await capture.reconcile("p") == 1
    assert await capture.reconcile("p") == 0
    await db.log_event("task.completed", project_id="p", task_id=row["task_id"])
    await db.log_event("task.completed", project_id="p", task_id=row["task_id"])
    await capture.reconcile("p")
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(store.jobs)) == 1
        assert await conn.scalar(select(store.checkpoints.c.last_event_id)) == 2
    assert await capture.reconcile("p") == 0


async def test_event_without_durable_result_quarantines_instead_of_treating_payload_as_evidence(
    setup,
):
    db, _, _, store, capture = setup
    await db.log_event(
        "task.completed",
        project_id="p",
        task_id="expired",
        payload='{"summary":"invented event payload"}',
    )
    assert await capture.reconcile("p") == 1
    async with db.immediate() as conn:
        job = (await conn.execute(select(store.jobs))).mappings().one()
        assert job["state"] == "quarantined" and job["error_code"] == "source_unavailable"
        assert await conn.scalar(select(store.checkpoints.c.last_event_id)) == 1


async def test_capture_error_does_not_advance_durable_event_cursor(setup, monkeypatch):
    db, _, _, store, capture = setup
    await completion(db)
    await db.log_event("task.completed", project_id="p", task_id="t")

    async def crash(*args, **kwargs):
        raise RuntimeError("artifact persistence failure")

    monkeypatch.setattr(capture, "capture_on", crash)
    with pytest.raises(RuntimeError):
        await capture.reconcile("p")
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(store.checkpoints)) == 0


@pytest.mark.parametrize(
    "changes",
    [{"evidence_type": "verified"}, {"scope_key": "global"}, {"line_end": 0}, {"event_id": -1}],
)
def test_invalid_provenance_is_rejected(changes):
    with pytest.raises(RecordError):
        partition_inputs([source(**changes)])


async def test_retained_tombstone_cannot_be_recaptured_or_read(setup):
    db, _, _, store, capture = setup
    job = (await capture.capture([source()]))[0]
    async with db.immediate() as conn:
        receipt = (await store.inputs_on(job["job_id"], conn=conn))[0]
        await conn.execute(update(record_source_artifacts).values(redacted_at=store.clock()))
    with pytest.raises(RecordError, match="record.revision_redacted"):
        await capture.capture([source()])
    async with db.immediate() as conn:
        with pytest.raises(RecordError, match="extraction.source_unavailable"):
            await capture.artifacts().read_on(
                receipt["artifact_id"], scope_key=job["scope_key"], conn=conn
            )


async def test_disabled_capture_does_no_database_work(setup, monkeypatch):
    db, config, _, _, capture = setup
    config.memory.enabled = False
    monkeypatch.setattr(db, "immediate", lambda: pytest.fail("disabled database access"))
    assert await capture.reconcile("p") == 0
    assert await capture.reconcile_consolidation("p") == 0
