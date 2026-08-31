"""Regression cover for ``immediate()`` vs. concurrent plain writers (P2-16).

With SQLite's ``StaticPool`` the whole process shared one DBAPI connection,
so a plain ``engine.begin()`` writer committing while an ``immediate()``
block was mid-transaction committed *that* transaction too; the
``immediate()`` block's own ``COMMIT`` then blew up with "cannot commit -
no transaction is active".  File databases now use ``NullPool``, so each
transaction owns its connection and SQLite's writer lock arbitrates.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from src.database import Database
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "immediate.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def test_immediate_survives_concurrent_plain_writers(db):
    """30 ``immediate()`` blocks interleaved with 30 plain transitions."""
    n = 30
    for i in range(n):
        await db.create_task(
            Task(
                id=f"imm-{i}",
                project_id=PROJECT_ID,
                title=f"imm {i}",
                description="d",
                status=TaskStatus.READY,
            )
        )
        await db.create_task(
            Task(
                id=f"plain-{i}",
                project_id=PROJECT_ID,
                title=f"plain {i}",
                description="d",
                status=TaskStatus.READY,
            )
        )

    async def immediate_writer(i: int) -> None:
        async with db.immediate() as conn:
            await conn.execute(
                text("UPDATE tasks SET title = :t WHERE id = :id"),
                {"t": f"touched-{i}", "id": f"imm-{i}"},
            )
            # Yield mid-transaction so a plain writer is guaranteed to
            # interleave here — the exact window that used to corrupt the
            # shared StaticPool connection.
            await asyncio.sleep(0.01)

    async def plain_writer(i: int) -> None:
        await db.transition_task(f"plain-{i}", TaskStatus.IN_PROGRESS)

    results = await asyncio.gather(
        *[immediate_writer(i) for i in range(n)],
        *[plain_writer(i) for i in range(n)],
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"concurrent writers raised: {errors!r}"

    # Every write is durably present.
    for i in range(n):
        imm = await db.get_task(f"imm-{i}")
        assert imm is not None and imm.title == f"touched-{i}"
        plain = await db.get_task(f"plain-{i}")
        assert plain is not None and plain.status == TaskStatus.IN_PROGRESS


async def test_immediate_rolls_back_every_write_after_exception(db):
    with pytest.raises(RuntimeError, match="rollback"):
        async with db.immediate() as conn:
            await conn.execute(
                text("INSERT INTO projects (id, name, created_at) VALUES ('rolled-back', 'r', 0)")
            )
            raise RuntimeError("rollback")
    assert await db.get_project("rolled-back") is None


async def test_file_sqlite_adapters_serialize_immediate_writers_without_cross_commit(tmp_path):
    first = Database(str(tmp_path / "shared.db"))
    second = Database(str(tmp_path / "shared.db"))
    await first.initialize()
    await second.initialize()
    try:
        await first.create_project(Project(id="shared", name="shared"))
        async with first.immediate() as conn:
            await conn.execute(text("UPDATE projects SET name = 'first' WHERE id = 'shared'"))
        assert (await second.get_project("shared")).name == "first"
    finally:
        await first.close()
        await second.close()
