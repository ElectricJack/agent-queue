"""Archive-before-delete and exact anti-replay after dedupe compaction."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, event, func, inspect, select, update

from src.database import Database
from tests.db_fixtures import lease_dsn
from src.database.tables import sessions, token_ledger, transcript_usage_calls, transcript_usage_fences
from src.models import Project, SessionRecord
from src.sessions.transcripts import retention

DAY = 86400


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("transcript-retention")
    await database.create_project(Project(id="p", name="P"))
    return database


async def session(db, name="old", *, state="stopped", age=31, desired="stopped"):
    await db.create_session(SessionRecord(
        id=name, project_id="p", profile_id="worker", harness="claude", provider="fake",
        name=name, lifecycle="task", work_dir="/tmp", epoch="e", instance_token=name,
        started_at=time.time() - 40 * DAY, state=state, desired_state=desired,
        ended_at=time.time() - age * DAY if age is not None else None,
    ))


async def record(db, key="call", *, output=2, owner="old"):
    await db.record_transcript_usage(
        usage_key=key, project_id="p", agent_id="agent", task_id="task", session_id=owner,
        attempt_id=None, model="claude-x", model_source="assistant_response",
        counters={"input_tokens": 10, "output_tokens": output}, legacy_call_ids=[],
    )


async def age_calls(db):
    async with db._engine.begin() as conn:
        await conn.execute(update(transcript_usage_calls).values(updated_at=time.time() - 31 * DAY))


async def count(db, table):
    async with db._engine.connect() as conn:
        return await conn.scalar(select(func.count()).select_from(table))


async def test_fence_survives_deleting_calls_ledger_and_session(db, tmp_path):
    await session(db)
    for i in range(5):
        await record(db, f"call-{i}")
    await age_calls(db)
    result = await retention.archive_transcript_usage_calls(
        db._engine, backup_dir=tmp_path, batch_size=2, max_batches=2,
    )
    assert result["deleted"] == result["archived"] == 4
    assert result["batches"] == 2
    assert await count(db, transcript_usage_calls) == 1
    assert await count(db, transcript_usage_fences) == 2
    result = await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    assert result["deleted"] == 1
    with gzip.open(result["archive_path"], "rt") as archive:
        rows = [json.loads(line) for line in archive]
    assert {row["usage_key"] for row in rows} == {f"call-{i}" for i in range(5)}
    assert all(row["input_tokens"] == 10 and row["session_id"] == "old" for row in rows)
    async with db._engine.begin() as conn:
        await conn.execute(delete(token_ledger))
        await conn.execute(delete(sessions))
    # Permanent fence is independent of all original attribution and checkpoints.
    await asyncio.gather(*(record(db, f"call-{i}", output=99, owner="new") for i in range(5)))
    assert await count(db, token_ledger) == 0
    assert await count(db, transcript_usage_calls) == 0
    await record(db, "new-call", owner="new")
    assert await db.get_project_token_usage("p") == 12


@pytest.mark.parametrize("state,age,desired", [
    ("running", 31, "running"), ("sleeping", 31, "sleeping"),
    ("quarantined", 31, "stopped"), ("stopped", 29, "stopped"),
    ("stopped", None, "stopped"), ("stopped", 31, "running"),
])
async def test_nonterminal_or_recent_sessions_keep_exact_maxima(db, tmp_path, state, age, desired):
    await session(db, state=state, age=age, desired=desired)
    await record(db)
    await age_calls(db)
    result = await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    assert result["deleted"] == 0
    await asyncio.gather(*(record(db, output=n) for n in [1, 8, 3, 8]))
    assert await db.get_project_token_usage("p") == 18
    assert await count(db, transcript_usage_calls) == 1
    assert await count(db, transcript_usage_fences) == 0


async def test_recent_counter_and_missing_attribution_are_retained(db, tmp_path):
    await session(db)
    await record(db, "recent")
    await record(db, "missing-session", owner="missing")
    async with db._engine.begin() as conn:
        await conn.execute(update(transcript_usage_calls).where(
            transcript_usage_calls.c.usage_key == "missing-session"
        ).values(updated_at=time.time() - 31 * DAY))
    assert (await retention.archive_transcript_usage_calls(
        db._engine, backup_dir=tmp_path,
    ))["deleted"] == 0


async def test_archive_failure_never_creates_fence_or_deletes(db, tmp_path, monkeypatch):
    await session(db)
    await record(db)
    await age_calls(db)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(retention, "_archive", fail)
    with pytest.raises(OSError, match="disk full"):
        await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    assert await count(db, transcript_usage_calls) == 1
    assert await count(db, transcript_usage_fences) == 0


async def test_archive_is_off_loop_before_deletion_and_rechecks_changed_row(db, tmp_path, monkeypatch):
    await session(db)
    await record(db)
    await age_calls(db)
    original = retention._archive
    entered, resume = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def pause(path, rows):
        assert threading.get_ident() != loop_thread
        original(path, rows)
        entered.set()
        assert resume.wait(10)
    monkeypatch.setattr(retention, "_archive", pause)
    task = asyncio.create_task(retention.archive_transcript_usage_calls(
        db._engine, backup_dir=tmp_path,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert await count(db, transcript_usage_calls) == 1
        assert await count(db, transcript_usage_fences) == 0
        await record(db, output=8)
    finally:
        resume.set()
    assert (await task)["deleted"] == 0
    assert await db.get_project_token_usage("p") == 18


async def test_reactivated_session_is_rechecked_after_archive(db, tmp_path, monkeypatch):
    await session(db)
    await record(db)
    await age_calls(db)
    original = retention._archive
    original_to_thread = asyncio.to_thread
    async def reactivate(function, *args):
        await original_to_thread(function, *args)
        async with db._engine.begin() as conn:
            await conn.execute(update(sessions).values(state="running", desired_state="running"))
    monkeypatch.setattr(retention.asyncio, "to_thread", reactivate)
    result = await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    assert original is retention._archive
    assert result["archived"] == 1 and result["deleted"] == 0


async def test_delete_failure_rolls_back_fence_and_retry_is_safe(db, tmp_path):
    await session(db)
    await record(db)
    await age_calls(db)
    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM transcript_usage_calls"):
            raise RuntimeError("simulated delete failure")
    event.listen(db._engine.sync_engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="delete failure"):
            await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_delete)
    assert await count(db, transcript_usage_calls) == 1
    assert await count(db, transcript_usage_fences) == 0
    result = await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
    assert result["deleted"] == 1
    with gzip.open(result["archive_path"], "rt") as archive:
        assert len(list(archive)) == 2  # duplicate evidence is safe after a rollback


@pytest.fixture
async def migration_db():
    database = Database(lease_dsn("transcript-fence-migration"))
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def test_migration_idempotence_and_populated_downgrade_refusal(migration_db):
    db = migration_db
    migration = import_module("migrations.versions.a00000000122_transcript_usage_fences")
    async with db._engine.begin() as conn:
        def upgrade(sync):
            transcript_usage_fences.drop(sync)
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
                migration.upgrade()
            assert "idx_transcript_usage_fences_keys" in {
                index["name"] for index in inspect(sync).get_indexes("transcript_usage_fences")
            }
        await conn.run_sync(upgrade)
    # No archive required to exercise the downgrade safety guard.
    async with db._engine.begin() as conn:
        await conn.execute(transcript_usage_fences.insert().values(
            id="f", usage_keys={"call": True}, archived_at=time.time(), archive_path="archive",
        ))
        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                with pytest.raises(RuntimeError, match="Cannot discard"):
                    migration.downgrade()
        await conn.run_sync(downgrade)


@pytest.mark.parametrize("pause_before", ["INSERT INTO", "SELECT"])
async def test_ingestion_racing_with_fence_cannot_recreate_or_charge(db, tmp_path, monkeypatch,
                                                                  pause_before):
    from sqlalchemy.ext.asyncio import AsyncConnection

    await session(db)
    await record(db)
    await age_calls(db)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute
    async def pause(conn, statement, *args, **kwargs):
        sql = str(statement)
        if (asyncio.current_task().get_name() == "racing-ingest"
                and sql.startswith(pause_before) and "transcript_usage_calls" in sql):
            entered.set()
            await resume.wait()
        return await original(conn, statement, *args, **kwargs)
    monkeypatch.setattr(AsyncConnection, "execute", pause)
    task = asyncio.create_task(record(db, output=8, owner="replay"), name="racing-ingest")
    try:
        await asyncio.wait_for(entered.wait(), 10)
        result = await retention.archive_transcript_usage_calls(db._engine, backup_dir=tmp_path)
        assert result["deleted"] == 1
    finally:
        resume.set()
    await task
    assert await count(db, transcript_usage_calls) == 0
    assert await db.get_project_token_usage("p") == 12


def test_failed_archive_append_preserves_previous_complete_gzip(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "archive.jsonl.gz"
    retention._archive(path, [{"usage_key": "first"}])
    original = path.read_bytes()
    def fail(*args):
        raise OSError("interrupted publication")
    monkeypatch.setattr(retention.os, "replace", fail)
    with pytest.raises(OSError, match="interrupted publication"):
        retention._archive(path, [{"usage_key": "second"}])
    assert path.read_bytes() == original
    with gzip.open(path, "rt") as stream:
        assert [json.loads(line) for line in stream] == [{"usage_key": "first"}]
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name, path.name + ".lock"]
