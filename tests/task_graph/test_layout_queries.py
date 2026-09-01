import pytest

from src.database import Database
from src.models import Project


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "lq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def test_dirty_marks_round_trip(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1", "t2"], "task.created", conn=conn)
    assert await db.dirty_layout_projects() == ["p1"]
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert seq >= 2 and sorted(r[0] for r in rows) == ["t1", "t2"]


async def test_pop_respects_debounce(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1"], "task.created", conn=conn)
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=60)
    assert rows == [] and seq == 0


async def test_jobs_lifecycle(db):
    job = await db.enqueue_layout_job("p1", "all", "tidy")
    again = await db.enqueue_layout_job("p1", "all", "tidy")
    assert again["id"] == job["id"]
    nxt = await db.next_layout_job()
    assert nxt["id"] == job["id"] and nxt["status"] == "running"
    await db.finish_layout_job(job["id"], error=None)
    assert await db.next_layout_job() is None
    assert (await db.get_layout_job(job["id"]))["status"] == "done"


async def test_meta_absent_until_published(db):
    assert await db.get_layout_meta("p1", "all") is None
