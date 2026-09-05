"""Dotted ids and --parent in the graph creator — spec §6."""

from __future__ import annotations

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.task_graph import parse_graph
from src.task_graph.creator import build_plan, write_plan

PROJECT_ID = "proj"

GRAPH = {
    "version": 1,
    "parent": {"title": "Epic"},
    "nodes": [
        {"key": "a", "title": "A"},
        {"key": "b", "title": "B", "needs": [{"on": "a"}]},
    ],
}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


class TestNewContainer:
    async def test_enabled_project_rejects_before_graph_inserts_any_task(self, db, monkeypatch):
        await db.create_repo(
            RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="hierarchy",
            integration_repository_id="repo",
        )
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        inserted = []

        async def observed_insert(conn, row):
            inserted.append(row["id"])

        monkeypatch.setattr("src.task_graph.creator._insert_task", observed_insert)
        with pytest.raises(HierarchyError) as exc:
            await write_plan(db, plan)

        assert exc.value.code == "integration_required"
        assert inserted == []
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    async def test_dotted_ids_known_at_plan_time(self, db):
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        assert plan.provisional is False
        assert plan.task_ids == [f"{plan.parent_id}.1", f"{plan.parent_id}.2"]
        assert plan.parent_row["next_child_ordinal"] == 3

    async def test_write_links_children_and_marks_container(self, db):
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        await write_plan(db, plan)
        a, b = plan.task_ids
        assert (await db.get_task(a)).parent_task_id == plan.parent_id
        assert (plan.parent_id, "parent-child") in await db.get_typed_dependencies(b)
        assert (a, "blocks") in await db.get_typed_dependencies(b)
        async with db._engine.begin() as conn:
            assert await db.is_container(plan.parent_id, conn=conn)

    async def test_recompute_blocked_after_write(self, db):
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        await write_plan(db, plan)
        a, b = plan.task_ids
        assert (await db.get_task(b)).is_blocked is True
        assert (await db.get_task(a)).is_blocked is False


class TestExistingParent:
    async def test_provisional_ids_then_reserved_on_write(self, db):
        await db.create_task(
            Task(
                id="epic",
                project_id=PROJECT_ID,
                title="e",
                description="e",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID, parent_id="epic")
        assert plan.provisional is True
        assert plan.task_ids == ["epic.?", "epic.?"]
        assert plan.parent_row is None
        await write_plan(db, plan)
        assert plan.task_ids == ["epic.1", "epic.2"]
        assert (await db.get_task("epic.2")).parent_task_id == "epic"
        assert ("epic.1", "blocks") in await db.get_typed_dependencies("epic.2")
        from sqlalchemy import select

        from src.database.tables import tasks as tasks_table

        async with db._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(tasks_table.c.next_child_ordinal).where(tasks_table.c.id == "epic")
                )
            ).fetchone()
        assert row[0] == 3

    async def test_dry_run_reserves_nothing(self, db):
        await db.create_task(
            Task(
                id="epic",
                project_id=PROJECT_ID,
                title="e",
                description="e",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID, parent_id="epic")
        assert (await db.get_task("epic")).parent_task_id is None
        async with db._engine.begin() as conn:
            from src.task_names import reserve_child_ordinal

            assert await reserve_child_ordinal(conn, "epic") == 1
