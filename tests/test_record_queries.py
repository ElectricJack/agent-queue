"""Deterministic aliases and exact revision reads survive execution lifecycle."""

import asyncio
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import insert, select

from src.database.queries.record_queries import RecordDomainUnavailable
from src.database.tables import archived_tasks, records, tasks
from src.records.identity import (
    RecordIntegrityError,
    knowledge_identity,
    revision_id,
    task_record_id,
)
from tests.record_helpers import (
    append_revision,
    create_knowledge,
    seed_project,
    seed_task,
    snapshot,
)


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


def test_identity_is_installation_scoped_and_title_independent():
    namespace = UUID("bc89a03a-f45a-46e6-a4f9-2f2f73f4673d")
    assert task_record_id(namespace, "calm-grove-25.1") == uuid5(namespace, "task:calm-grove-25.1")
    assert task_record_id(namespace, "t") != task_record_id(uuid4(), "t")
    assert task_record_id(namespace, "t") != task_record_id(namespace, "T")
    record, alias = knowledge_identity()
    assert record.version == revision_id().version == 4
    assert alias == f"kn-{record.hex}"
    with pytest.raises(ValueError):
        task_record_id(namespace, "")


async def test_mapping_replay_and_two_connection_race(db):
    from src.models import Task

    await seed_project(db)
    await db.create_task(Task(id="t", project_id="p", title="Task", description="Work"))
    barrier = asyncio.Barrier(2)

    async def ensure():
        await barrier.wait()
        async with db.immediate() as conn:
            return await db.ensure_task_record_on("t", actor_id="retry", conn=conn)

    rows = await asyncio.gather(ensure(), ensure())
    assert rows[0] == rows[1]
    async with db.immediate() as conn:
        assert len((await conn.execute(select(records))).all()) == 1


async def test_task_archive_and_delete_preserve_mapping_and_exact_projection(db):
    mapping = await seed_task(db)
    await db.archive_task("t")
    async with db.immediate() as conn:
        projection = await db.get_task_record_domain_on("t", conn=conn)
        assert projection["archived"] is True
        assert projection["title"] == "Task"
        assert await db.ensure_task_record_on("t", actor_id="test", conn=conn) == mapping
    await db.delete_archived_task("t")
    async with db.immediate() as conn:
        assert await db.get_record_on(task_id="t", conn=conn) == mapping
        assert await db.get_task_record_domain_on("t", conn=conn) is None
        with pytest.raises(RecordDomainUnavailable):
            await db.ensure_task_record_on("t", actor_id="test", conn=conn)
        with pytest.raises(LookupError, match="not_found"):
            await db.ensure_task_record_on("never-existed", actor_id="test", conn=conn)


async def test_live_task_delete_is_not_pinned_by_record(db):
    mapping = await seed_task(db)
    await db.delete_task("t")
    async with db.immediate() as conn:
        assert await db.get_record_on(task_id="t", conn=conn) == mapping
        assert await db.get_task_record_domain_on("t", conn=conn) is None


async def test_missing_project_and_changed_project_cannot_rebind_identity(db):
    await seed_task(db)
    await seed_project(db, "elsewhere")
    from sqlalchemy import update

    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t").values(project_id="elsewhere"))
    with pytest.raises(RecordIntegrityError):
        async with db.immediate() as conn:
            await db.ensure_task_record_on("t", actor_id="test", conn=conn)
    async with db.immediate() as conn:
        with pytest.raises(RecordDomainUnavailable):
            await db.ensure_record_scope_on(project_id="missing", conn=conn)


async def test_uuid_collision_and_live_archive_collision_are_hard_errors(db):
    await seed_task(db, "other")
    from src.models import Task

    await db.create_task(Task(id="t", project_id="p", title="T", description="D"))
    async with db.immediate() as conn:
        deterministic = task_record_id(await db.get_record_installation_on(conn=conn), "t")
        await db.insert_record_on(
            dict(
                record_id=deterministic,
                kind="task",
                task_id="collision",
                scope_key="project:p",
                created_by="test",
            ),
            conn=conn,
        )
    with pytest.raises(RecordIntegrityError):
        async with db.immediate() as conn:
            await db.ensure_task_record_on("t", actor_id="test", conn=conn)
    async with db.immediate() as conn:
        await conn.execute(
            insert(archived_tasks).values(
                id="other",
                project_id="p",
                title="T",
                description="D",
                status="COMPLETED",
                created_at=1,
                updated_at=1,
                archived_at=1,
            )
        )
        with pytest.raises(RecordIntegrityError, match="both live and archive"):
            await db.get_task_record_domain_on("other", conn=conn)


async def test_exact_history_cas_and_no_implicit_commit(db):
    await seed_project(db)
    original = snapshot(body="Original bytes\r\nα")
    async with db.immediate() as conn:
        record_id, first = await create_knowledge(db, conn, doc=original)
        second = await append_revision(db, conn, record_id, first)
    async with db.immediate() as conn:
        assert (await db.get_knowledge_revision_on(record_id, conn=conn))["revision_id"] == second
        exact = await db.get_knowledge_revision_on(record_id, revision_id=first, conn=conn)
        assert exact["snapshot"] == original
        assert await db.get_knowledge_revision_on(record_id, revision_id=uuid4(), conn=conn) is None
        assert await db.get_knowledge_revision_on(uuid4(), revision_id=first, conn=conn) is None
        history = await db.list_knowledge_history_on(record_id, before_sequence=2, conn=conn)
        assert [r["revision_id"] for r in history] == [first]
        assert "snapshot" not in history[0]
        assert not await db.advance_knowledge_head_on(
            record_id,
            expected_revision=first,
            revision_id=uuid4(),
            sequence=3,
            updated_at=exact["created_at"],
            conn=conn,
        )
    with pytest.raises(RuntimeError, match="rollback"):
        async with db.immediate() as conn:
            rolled_back, _ = await create_knowledge(db, conn)
            raise RuntimeError("rollback")
    async with db.immediate() as conn:
        assert await db.get_record_on(record_id=rolled_back, conn=conn) is None
