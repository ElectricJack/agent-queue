"""hierarchy.* doctor checks — spec §16."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, text, update

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.database.tables import task_dependencies, tasks
from src.doctor.hierarchy_checks import hierarchy_checks
from src.doctor.models import DoctorContext, Severity
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@pytest.fixture
def ctx(db, tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path),
    )
    return DoctorContext(config=cfg, db=db)


def check(cid):
    return next(c for c in hierarchy_checks() if c.id == cid)


async def mktask(db, tid, status=TaskStatus.DEFINED):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status)
    )


class TestParentPointer:
    async def test_ok_when_consistent(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        res = await check("hierarchy.parent_pointer").run(ctx)
        assert res.severity == Severity.OK

    async def test_detects_and_fixes_drift(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id="c", depends_on_task_id="p", dep_type="parent-child"
                )
            )
        res = await check("hierarchy.parent_pointer").run(ctx)
        assert res.severity == Severity.ERROR and res.fixable
        fixed = await check("hierarchy.parent_pointer").fix(ctx)
        assert fixed.fix_applied
        assert (await db.get_task("c")).parent_task_id == "p"

    async def test_fix_touches_only_the_drifted_row(self, db, ctx):
        """The repair used to rewrite every task row in the database."""
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await mktask(db, "bystander")
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id="c", depends_on_task_id="p", dep_type="parent-child"
                )
            )
        before = {tid: (await db.get_task(tid)).updated_at for tid in ("p", "c", "bystander")}

        fixed = await check("hierarchy.parent_pointer").fix(ctx)

        assert fixed.fix_applied
        after = {tid: (await db.get_task(tid)).updated_at for tid in ("p", "c", "bystander")}
        assert after["c"] > before["c"]
        assert after["p"] == before["p"]
        assert after["bystander"] == before["bystander"]


class TestSingleParent:
    async def test_ok_on_consistent_db(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        res = await check("hierarchy.single_parent").run(ctx)
        assert res.severity == Severity.OK


class TestDepth:
    async def test_ok_within_depth(self, db, ctx):
        await mktask(db, "root", TaskStatus.IN_PROGRESS)
        await mktask(db, "mid", TaskStatus.IN_PROGRESS)
        await mktask(db, "leaf")
        await db.add_dependency("mid", "root", "parent-child")
        await db.add_dependency("leaf", "mid", "parent-child")
        res = await check("hierarchy.depth").run(ctx)
        assert res.severity == Severity.OK

    async def test_detects_excess_depth(self, db, ctx):
        # set_parent enforces max depth 3 at insert time, so a depth-4 chain
        # can only exist via drift (e.g. a restored backup) — simulate that
        # with raw column writes, bypassing the guard on purpose.
        await mktask(db, "t0", TaskStatus.IN_PROGRESS)
        await mktask(db, "t1", TaskStatus.IN_PROGRESS)
        await mktask(db, "t2", TaskStatus.IN_PROGRESS)
        await mktask(db, "t3", TaskStatus.IN_PROGRESS)
        await mktask(db, "t4")
        async with db._engine.begin() as conn:
            for child, parent in (("t1", "t0"), ("t2", "t1"), ("t3", "t2"), ("t4", "t3")):
                await conn.execute(
                    update(tasks).where(tasks.c.id == child).values(parent_task_id=parent)
                )
        res = await check("hierarchy.depth").run(ctx)
        assert res.severity == Severity.ERROR
        assert "t4" in res.data["tasks"]


class TestOthers:
    async def test_closed_container_children(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c", TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        async with db._engine.begin() as conn:  # bypass the guard on purpose
            await conn.execute(update(tasks).where(tasks.c.id == "p").values(status="COMPLETED"))
        res = await check("hierarchy.closed_container_children").run(ctx)
        assert res.severity == Severity.ERROR and res.data["containers"] == ["p"]

    async def test_migration_rejects_warns(self, db, ctx):
        async with db._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO hierarchy_migration_rejects (run_id, task_id, parent_id, source, reason, "
                    "detail, created_at) VALUES ('r1', 'x', 'y', 'edge', 'cycle', '', 0)"
                )
            )
        res = await check("hierarchy.migration_rejects").run(ctx)
        assert res.severity == Severity.WARN and res.data["count"] == 1
