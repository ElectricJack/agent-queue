# tests/test_hierarchy_queries.py
"""HierarchyQueryMixin — spec Part I (§4–§8)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import task_metadata
from src.models import AgentState, DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


class TestSchemaFields:
    async def test_new_task_columns_have_defaults(self, db):
        await mktask(db, "a")
        t = await db.get_task("a")
        assert t.created_by_kind is None
        assert t.created_by_id is None
        assert t.claim_epoch == 0
        assert t.filed_count == 0

    async def test_created_by_round_trips(self, db):
        await mktask(db, "a", created_by_kind="session", created_by_id="s-1")
        t = await db.get_task("a")
        assert (t.created_by_kind, t.created_by_id) == ("session", "s-1")

    def test_agent_state_has_retired(self):
        assert AgentState.RETIRED.value == "RETIRED"


class TestSetParent:
    async def test_writes_edge_and_pointer_together(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
        assert (await db.get_task("c")).parent_task_id == "p"
        assert await db.get_typed_dependencies("c") == [("p", "parent-child")]
        async with db._engine.begin() as conn:
            assert await db.is_container("p", conn=conn) is True

    async def test_reparent_replaces_the_single_edge(self, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p1", conn=conn)
            await db.set_parent("c", "p2", conn=conn)
        assert (await db.get_task("c")).parent_task_id == "p2"
        assert await db.get_typed_dependencies("c") == [("p2", "parent-child")]

    async def test_to_root_clears_both(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
            await db.set_parent("c", None, conn=conn)
        assert (await db.get_task("c")).parent_task_id is None
        assert await db.get_typed_dependencies("c") == []

    async def test_child_is_withheld_while_container_is_defined(self, db):
        await mktask(db, "p")  # DEFINED
        await mktask(db, "c", status=TaskStatus.READY)
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
        assert (await db.get_task("c")).is_blocked is True

    @pytest.mark.parametrize(
        "setup, code",
        [
            ("self", "self_parent"),
            ("cycle", "cycle"),
            ("cross_project", "cross_project"),
            ("depth", "depth"),
            ("missing", "not_found"),
        ],
    )
    async def test_rejections(self, db, setup, code):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "b", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            if setup == "self":
                args = ("a", "a")
            elif setup == "cycle":
                await db.set_parent("b", "a", conn=conn)
                args = ("a", "b")
            elif setup == "cross_project":
                await db.create_project(Project(id="other", name="o"))
                await db.create_task(Task(id="x", project_id="other", title="x", description="x"))
                args = ("x", "a")
            elif setup == "depth":
                await mktask(db, "c", status=TaskStatus.IN_PROGRESS)
                await mktask(db, "d")
                await db.set_parent("b", "a", conn=conn)
                await db.set_parent("c", "b", conn=conn)
                args = ("d", "c")  # would be structural depth 4
            else:
                args = ("a", "ghost")
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent(*args, conn=conn)
        assert exc.value.code == code

    async def test_completed_container_refuses_children(self, db):
        await mktask(db, "p", status=TaskStatus.COMPLETED)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent("c", "p", conn=conn)
        assert exc.value.code == "container_closed"


class TestCreateTaskUnder:
    async def test_mints_dotted_id_and_links(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        task = Task(id="ignored", project_id=PROJECT_ID, title="t", description="t")
        tid, capped = await db.create_task_under(task, "p")
        assert (tid, capped) == ("p.1", False)
        t = await db.get_task("p.1")
        assert t.parent_task_id == "p"
        assert await db.get_typed_dependencies("p.1") == [("p", "parent-child")]

    async def test_naming_cap_uses_discovered_from(self, db):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        b, _ = await db.create_task_under(
            Task(id="", project_id=PROJECT_ID, title="b", description="b"), "a"
        )
        c, _ = await db.create_task_under(
            Task(id="", project_id=PROJECT_ID, title="c", description="c"), b
        )
        # c is "a.1.1" — naming depth 3.  Its child gets a root id + discovered-from.
        await db.transition_task(b, TaskStatus.IN_PROGRESS)
        await db.transition_task(c, TaskStatus.IN_PROGRESS)
        d, capped = await db.create_task_under(
            Task(id="", project_id=PROJECT_ID, title="d", description="d"), c
        )
        assert capped is True and "." not in d
        assert (await db.get_task(d)).parent_task_id is None
        assert await db.get_typed_dependencies(d) == [(c, "discovered-from")]


class TestDependencyDelegation:
    async def test_add_dependency_parent_child_sets_pointer(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        assert (await db.get_task("c")).parent_task_id == "p"

    async def test_remove_dependency_parent_child_clears_pointer(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        await db.remove_dependency("c", "p", "parent-child")
        assert (await db.get_task("c")).parent_task_id is None
        assert await db.get_typed_dependencies("c") == []


class TestSetParentReblocksWaitsFor:
    async def test_moving_the_last_open_child_away_unblocks_the_old_waiter(self, db):
        await mktask(db, "c1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "finalize", status=TaskStatus.READY)
        await mktask(db, "worker", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("finalize", "c1", DepType.WAITS_FOR.value)
        async with db._engine.begin() as conn:
            await db.set_parent("worker", "c1", conn=conn)
        assert (await db.get_task("finalize")).is_blocked is True

        async with db._engine.begin() as conn:
            flipped, settled = await db.set_parent("worker", None, conn=conn)
        assert "finalize" in flipped
        assert settled == []
        assert (await db.get_task("finalize")).is_blocked is False


class TestCreateTaskCommandHierarchyErrors:
    async def test_completed_parent_returns_hierarchy_error(self, handler, db):
        await mktask(db, "p", status=TaskStatus.COMPLETED)
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "parent_id": "p"}
        )
        assert res["code"] == "hierarchy.container_closed"


class TestMarkContainerIdempotent:
    async def test_called_twice_leaves_one_row(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.mark_container("p", conn=conn)
            await db.mark_container("p", conn=conn)
        async with db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(task_metadata).where(
                        task_metadata.c.task_id == "p",
                        task_metadata.c.key == "container",
                    )
                )
            ).fetchall()
        assert len(rows) == 1


class TestStructureReads:
    async def test_depth_height_and_subtree_on_a_three_level_chain(self, db):
        await mktask(db, "root", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "mid", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "leaf")
        async with db._engine.begin() as conn:
            await db.set_parent("mid", "root", conn=conn)
            await db.set_parent("leaf", "mid", conn=conn)

        async with db._engine.begin() as conn:
            assert await db.structural_depth("root", conn=conn) == 1
            assert await db.structural_depth("mid", conn=conn) == 2
            assert await db.structural_depth("leaf", conn=conn) == 3

            assert await db.subtree_height("leaf", conn=conn) == 1
            assert await db.subtree_height("mid", conn=conn) == 2
            assert await db.subtree_height("root", conn=conn) == 3

            assert await db.subtree_ids("root", conn=conn) == ["root", "mid", "leaf"]
