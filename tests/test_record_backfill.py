"""Operator-only mapping repair has independent durable source cursors."""

import asyncio

import pytest
from sqlalchemy import func, insert, select

from src.commands.principal import principal_context
from src.database.tables import archived_tasks, record_backfill_state, records, tasks
from src.models import Task, TaskStatus
from src.records.backfill import TaskRecordBackfill, task_mapping_inventory
from src.records.identity import RecordIntegrityError, task_record_id
from tests.record_helpers import knowledge_config, seed_project, worker_principal


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


async def add_task(db, name):
    await seed_project(db)
    await db.create_task(
        Task(
            id=name,
            project_id="p",
            title=name,
            description="Untouched",
            status=TaskStatus.COMPLETED,
        )
    )


async def rows(db, table):
    async with db.immediate() as conn:
        return list(
            (await conn.execute(select(table).order_by(list(table.primary_key)[0]))).mappings()
        )


async def test_dry_run_reports_independent_counts_without_writes(db):
    await add_task(db, "a")
    await add_task(db, "b")
    await db.archive_task("b")
    before = await rows(db, tasks)
    result = await TaskRecordBackfill(db).run()
    assert result["dry_run"]
    assert result["inventory"]["tasks"]["missing"] == 1
    assert result["inventory"]["archived_tasks"]["missing"] == 1
    assert await rows(db, tasks) == before
    assert await rows(db, records) == await rows(db, record_backfill_state) == []


async def test_independent_cursors_replay_determinism_and_execution_unchanged(db):
    for name in ("a", "b", "c", "d", "e"):
        await add_task(db, name)
    await db.archive_task("b")
    await db.archive_task("d")
    live, archive = await rows(db, tasks), await rows(db, archived_tasks)
    backfill = TaskRecordBackfill(db, batch_size=2)
    result = await backfill.run(dry_run=False, max_batches=2)
    assert not result["done"]
    assert {r["source"]: r["cursor"] for r in await rows(db, record_backfill_state)} == {
        "tasks": "c",
        "archived_tasks": "d",
    }
    assert (await backfill.run(dry_run=False, max_batches=8))["done"]
    mappings = await rows(db, records)
    async with db.immediate() as conn:
        installation = await db.get_record_installation_on(conn=conn)
    assert all(r["record_id"] == task_record_id(installation, r["task_id"]) for r in mappings)
    assert (await backfill.run(dry_run=False, max_batches=4))["done"]
    assert await rows(db, records) == mappings
    assert sum(r["inserted"] for r in await rows(db, record_backfill_state)) == 5
    assert await rows(db, tasks) == live
    assert await rows(db, archived_tasks) == archive


async def test_interrupted_batch_rolls_back_mapping_and_cursor(db, monkeypatch):
    for name in ("a", "b", "c"):
        await add_task(db, name)
    original = db.ensure_task_record_on
    count = 0

    async def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("interrupted")
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "ensure_task_record_on", interrupted)
    with pytest.raises(RuntimeError):
        await TaskRecordBackfill(db).batch("tasks")
    assert await rows(db, records) == await rows(db, record_backfill_state) == []
    monkeypatch.setattr(db, "ensure_task_record_on", original)
    assert (await TaskRecordBackfill(db).batch("tasks"))["inserted"] == 3


async def test_reconciliation_finds_new_ids_below_cursor_and_archived_moves(db):
    await add_task(db, "m")
    await TaskRecordBackfill(db).run(dry_run=False, max_batches=4)
    await add_task(db, "a")  # lexically before the saved live cursor
    await add_task(db, "b")
    await db.archive_task("b")
    await TaskRecordBackfill(db).run(dry_run=False, max_batches=8)
    report = await task_mapping_inventory(db)
    assert report["tasks"]["missing"] == report["archived_tasks"]["missing"] == 0
    assert {r["task_id"] for r in await rows(db, records)} == {"a", "b", "m"}


async def test_concurrent_backfills_and_lazy_ensure_have_exact_inserted_count(db):
    for name in ("a", "b", "c"):
        await add_task(db, name)

    async def lazy():
        async with db.immediate() as conn:
            return await db.ensure_task_record_on(
                "a", actor_id="lazy", conn=conn, return_inserted=True
            )

    left, right, (_, lazy_inserted) = await asyncio.gather(
        TaskRecordBackfill(db).batch("tasks"),
        TaskRecordBackfill(db).batch("tasks"),
        lazy(),
    )
    assert left["inserted"] + right["inserted"] + lazy_inserted == 3
    assert len(await rows(db, records)) == 3


async def test_one_batch_requests_do_not_starve_archive_after_restart(db):
    await add_task(db, "live")
    await add_task(db, "archive")
    await db.archive_task("archive")
    for _ in range(4):
        await TaskRecordBackfill(db).run(dry_run=False, max_batches=1)
    inventory = await task_mapping_inventory(db)
    assert inventory["tasks"]["missing"] == inventory["archived_tasks"]["missing"] == 0


async def test_duplicate_alias_inventory_refuses_apply(db):
    await add_task(db, "dup")
    async with db.immediate() as conn:
        await conn.execute(
            insert(archived_tasks).values(
                id="dup",
                project_id="p",
                title="Duplicate",
                description="Original retained",
                status="COMPLETED",
                created_at=1,
                updated_at=1,
                archived_at=1,
            )
        )
    assert (await TaskRecordBackfill(db).run())["inventory"]["duplicate_aliases"] == 1
    with pytest.raises(RecordIntegrityError):
        await TaskRecordBackfill(db).run(dry_run=False)
    assert await rows(db, records) == []


async def test_unavailable_project_is_reported_and_does_not_loop_or_rewrite(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(archived_tasks).values(
                id="orphan",
                project_id="missing",
                title="Orphan archive",
                description="Retained",
                status="COMPLETED",
                created_at=1,
                updated_at=1,
                archived_at=1,
            )
        )
    result = await TaskRecordBackfill(db).run(dry_run=False, max_batches=6)
    assert result["done"]
    assert result["inventory"]["archived_tasks"]["unavailable_projects"] == 1
    assert result["inventory"]["archived_tasks"]["missing"] == 1
    assert await rows(db, records) == []


async def test_worker_cannot_apply_or_inventory_operator_repair(command_handler_factory):
    handler = await command_handler_factory()
    worker = await worker_principal(handler.db, "repair-worker", grants=["record_repair"])
    handler.config.knowledge = knowledge_config()
    with principal_context(worker):
        result = await handler._cmd_record_repair({"operation": "backfill-task-mappings"})
    assert result["error_code"] == "record.forbidden"
    async with handler.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_backfill_state)) == 0


def test_batches_cannot_bypass_limits():
    for size in (0, 501):
        with pytest.raises(ValueError):
            TaskRecordBackfill(None, batch_size=size)
