"""``task_subtasks`` — durable, non-schedulable checklist rows (Task C1)."""

from __future__ import annotations

import pytest

from src.database import Database
from src.database.queries.task_subtask_queries import MAX_SUBTASKS_PER_TASK
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.IN_PROGRESS, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def test_add_two_batches_yields_contiguous_ordinals(db):
    await mktask(db, "t")
    first = await db.add_task_subtasks(
        "t", PROJECT_ID, [{"title": "one"}, {"title": "two", "context": "ctx"}]
    )
    assert [row["ordinal"] for row in first] == [1, 2]
    assert [row["id"] for row in first] == ["t#s1", "t#s2"]
    second = await db.add_task_subtasks("t", PROJECT_ID, [{"title": "three"}])
    assert [row["ordinal"] for row in second] == [3]


async def test_list_is_ordinal_ordered_and_omits_context(db):
    await mktask(db, "t")
    await db.add_task_subtasks(
        "t", PROJECT_ID, [{"title": "b", "context": "secret-b"}, {"title": "a"}]
    )
    rows = await db.list_task_subtasks("t")
    assert [r["ordinal"] for r in rows] == [1, 2]
    assert [r["title"] for r in rows] == ["b", "a"]
    assert all("context" not in r for r in rows)


async def test_get_returns_context(db):
    await mktask(db, "t")
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one", "context": "the context"}])
    row = await db.get_task_subtask("t", 1)
    assert row["context"] == "the context"
    assert row["title"] == "one"


async def test_get_missing_returns_none(db):
    await mktask(db, "t")
    assert await db.get_task_subtask("t", 1) is None


async def test_update_sets_status_and_note_and_bumps_updated_at(db):
    await mktask(db, "t")
    added = (await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}]))[0]
    updated = await db.update_task_subtask("t", 1, status="done", note="all set")
    assert updated["status"] == "done"
    assert updated["note"] == "all set"
    assert updated["updated_at"] > added["updated_at"]


async def test_update_invalid_status_raises(db):
    await mktask(db, "t")
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}])
    with pytest.raises(ValueError):
        await db.update_task_subtask("t", 1, status="bogus")


async def test_update_missing_returns_none(db):
    await mktask(db, "t")
    assert await db.update_task_subtask("t", 1, status="done") is None


async def test_count_returns_total_and_settled_and_omits_empty_tasks(db):
    await mktask(db, "t1")
    await mktask(db, "t2")
    await db.add_task_subtasks(
        "t1", PROJECT_ID, [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    )
    await db.update_task_subtask("t1", 1, status="done")
    await db.update_task_subtask("t1", 2, status="skipped")
    counts = await db.count_task_subtasks(["t1", "t2"])
    assert counts["t1"] == (3, 2)
    assert "t2" not in counts


async def test_add_past_limit_raises_subtask_limit(db):
    await mktask(db, "t")
    await db.add_task_subtasks(
        "t", PROJECT_ID, [{"title": f"item-{i}"} for i in range(MAX_SUBTASKS_PER_TASK)]
    )
    with pytest.raises(ValueError, match="subtask_limit"):
        await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one too many"}])


async def test_skip_open_flips_only_open_rows_and_returns_count(db):
    await mktask(db, "t")
    await db.add_task_subtasks(
        "t", PROJECT_ID, [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    )
    await db.update_task_subtask("t", 1, status="done")
    flipped = await db.skip_open_task_subtasks("t", "ran out of time")
    assert flipped == 2
    rows = await db.list_task_subtasks("t")
    statuses = {r["ordinal"]: r["status"] for r in rows}
    assert statuses == {1: "done", 2: "skipped", 3: "skipped"}
    assert (await db.get_task_subtask("t", 2))["note"] == "ran out of time"
    assert (await db.get_task_subtask("t", 1))["note"] is None


async def test_hard_delete_removes_subtasks(db):
    await mktask(db, "t", status=TaskStatus.DEFINED)
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}])
    await db.delete_task("t")
    assert await db.list_task_subtasks("t") == []


async def test_archive_keeps_subtasks(db):
    await mktask(db, "t", status=TaskStatus.COMPLETED)
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}])
    assert await db.archive_task("t") is True
    rows = await db.list_task_subtasks("t")
    assert [r["title"] for r in rows] == ["one"]


async def test_permanent_archive_delete_removes_subtasks(db):
    await mktask(db, "t", status=TaskStatus.COMPLETED)
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}])
    await db.archive_task("t")
    assert await db.list_task_subtasks("t") != []
    assert await db.delete_archived_task("t") is True
    assert await db.list_task_subtasks("t") == []


async def test_project_delete_removes_subtasks(db):
    await mktask(db, "t", status=TaskStatus.DEFINED)
    await db.add_task_subtasks("t", PROJECT_ID, [{"title": "one"}])
    await db.delete_project(PROJECT_ID)
    assert await db.list_task_subtasks("t") == []
