# tests/test_hierarchy_queries.py
"""HierarchyQueryMixin — spec Part I (§4–§8)."""

from __future__ import annotations

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.models import AgentState, Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


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
        b, _ = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="b", description="b"), "a")
        c, _ = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="c", description="c"), b)
        # c is "a.1.1" — naming depth 3.  Its child gets a root id + discovered-from.
        await db.transition_task(b, TaskStatus.IN_PROGRESS)
        await db.transition_task(c, TaskStatus.IN_PROGRESS)
        d, capped = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="d", description="d"), c)
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
