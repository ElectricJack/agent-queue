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
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    DepType,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn

PROJECT_ID = "proj"
POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    """SQLite always; PostgreSQL when ``POSTGRES_TEST_DSN`` is set (CI).

    The module ``db`` fixture stays SQLite-only to keep the bulk of the
    suite fast; the tests below assert the guards whose SQL genuinely
    differs per dialect (FOR UPDATE, recursive CTEs) on both backends.
    """
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "any.db"))
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


async def test_set_parent_rejects_blocking_dependency_cycle_on_both_backends(any_db):
    """DB-3: a drifted blocking edge must not let a reparent close a cycle.

    ``parent`` already waits on ``child`` through a blocking edge; adding
    the parent-child edge child -> parent would make the blocking DAG
    cyclic, so ``set_parent`` must raise ``cycle`` and leave the pointer
    untouched.
    """
    db = any_db
    await mktask(db, "parent", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "child")
    await db.add_dependency("parent", "child", DepType.WAITS_FOR.value)
    async with db._engine.begin() as conn:
        with pytest.raises(HierarchyError, match="cycle"):
            await db.set_parent("child", "parent", conn=conn)
    assert (await db.get_task("child")).parent_task_id is None


async def test_set_parent_bulk_rejects_nonleaf_and_preserves_all_children(any_db):
    db = any_db
    await mktask(db, "parent", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "child")
    await mktask(db, "grandchild")
    async with db._engine.begin() as conn:
        await db.set_parent("grandchild", "child", conn=conn)
        with pytest.raises(HierarchyError, match="cycle_check_skipped"):
            await db.set_parent_bulk(["child"], "parent", conn=conn)
    # The refused bulk link left the whole existing structure intact.
    assert (await db.get_task("child")).parent_task_id is None
    assert (await db.get_task("grandchild")).parent_task_id == "child"


async def test_live_descendant_sessions_reports_holder_of_any_subtree_task(any_db):
    """The abandon guard sees a live session holding a descendant.

    On PostgreSQL the rows come back FOR UPDATE so a session cannot start
    holding a descendant between the check and the abandonment; a stopped
    session must not block the subtree.
    """
    db = any_db
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "descendant", status=TaskStatus.IN_PROGRESS)
    await db.create_session(
        SessionRecord(
            id="live-1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="p-worker--proj--live-1",
            lifecycle="pool",
            work_dir="/wd/live-1",
            epoch="e",
            instance_token="t",
            started_at=0.0,
            state="running",
            task_id="descendant",
        )
    )
    async with db._engine.begin() as conn:
        await db.set_parent("descendant", "container", conn=conn)
        assert await db.live_descendant_sessions("container", conn=conn) == [
            ("live-1", "descendant")
        ]
    await db.update_session("live-1", state="stopped")
    async with db._engine.begin() as conn:
        assert await db.live_descendant_sessions("container", conn=conn) == []


async def test_abandon_subtree_releases_each_descendant_resource_once(any_db):
    """Abandonment frees the workspace lock and the agent, same transaction."""
    db = any_db
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await mktask(db, "root", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "leaf", status=TaskStatus.IN_PROGRESS)
    await db.create_agent(
        Agent(id="leaf-agent", name="leaf-agent", profile_id="worker", state=AgentState.BUSY,
              current_task_id="leaf")
    )
    await db.create_workspace(
        Workspace(
            id="ws-leaf",
            project_id=PROJECT_ID,
            workspace_path="/wd/leaf",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
            locked_by_agent_id="leaf-agent",
            locked_by_task_id="leaf",
        )
    )
    async with db._engine.begin() as conn:
        await db.set_parent("leaf", "root", conn=conn)
        result = await db.abandon_subtree("root", conn=conn)
    assert result.abandoned == ["leaf"]
    assert (await db.get_task("leaf")).status == TaskStatus.COMPLETED
    assert await db.get_task_meta("leaf", "work_outcome") == "abandoned"
    ws = await db.get_workspace("ws-leaf")
    assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, None)
    agent = await db.get_agent("leaf-agent")
    assert (agent.state, agent.current_task_id) == (AgentState.IDLE, None)


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
            result = await db.set_parent("worker", None, conn=conn)
        assert "finalize" in result.flipped
        # c1 is now a childless container still IN_PROGRESS — spec §7 settles
        # it as soon as its last child leaves, same as reparenting the last
        # open child away (see test_hierarchy_settlement.py).
        assert result.settled == ["c1"]
        assert (await db.get_task("finalize")).is_blocked is False
        assert (await db.get_task("c1")).status == TaskStatus.COMPLETED


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


class TestReads:
    async def test_tree_shape_and_depth_bound(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1.1")
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.1.1", "r.1", "parent-child")
        tree = await db.get_task_tree("r")
        assert tree["task"].id == "r"
        assert tree["children"][0]["task"].id == "r.1"
        assert tree["children"][0]["children"][0]["task"].id == "r.1.1"
        shallow = await db.get_task_tree("r", max_depth=1)
        assert shallow["children"][0]["children"] == []

    async def test_children_filters(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.READY)
        await mktask(db, "r.2", status=TaskStatus.COMPLETED)
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.2", "r", "parent-child")
        assert [t.id for t in await db.get_children("r", status="READY")] == ["r.1"]
        assert [t.id for t in await db.get_children("r", limit=1, offset=1)] == ["r.2"]
        summary = await db.get_children_summary("r")
        assert summary == {"total": 2, "done": 1, "ready": 1, "blocked": 0, "in_progress": 0}
        assert await db.get_children_summary("r.1") is None

    async def test_progress_extras(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        for c in ("r.1", "r.2", "r.3"):
            await mktask(db, c, status=TaskStatus.READY)
            await db.add_dependency(c, "r", "parent-child")
        await db.add_dependency("r.3", "r.1", "blocks")
        p = await db.get_group_progress("r")
        assert p["waves"] == [["r.1", "r.2"], ["r.3"]]
        assert p["max_parallelism"] == 2
        assert p["depth"] == 2


class TestSetParentBulk:
    """The bulk twin used by ``write_plan`` (spec §5, §15.2)."""

    async def test_links_every_child_and_marks_the_container(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        kids = [await mktask(db, f"p.{i}") for i in (1, 2, 3)]
        async with db._engine.begin() as conn:
            await db.set_parent_bulk(kids, "p", conn=conn)
        for k in kids:
            assert (await db.get_task(k)).parent_task_id == "p"
            assert ("p", DepType.PARENT_CHILD.value) in await db.get_typed_dependencies(k)
        async with db._engine.begin() as conn:
            assert await db.is_container("p", conn=conn)

    async def test_empty_batch_is_a_no_op(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            assert await db.set_parent_bulk([], "p", conn=conn) == (set(), [])

    async def test_missing_parent_rejected(self, db):
        await mktask(db, "a")
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["a"], "nope", conn=conn)
        assert exc.value.code == "not_found"

    async def test_completed_parent_rejected(self, db):
        await mktask(db, "p", status=TaskStatus.COMPLETED)
        await mktask(db, "a")
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["a"], "p", conn=conn)
        assert exc.value.code == "container_closed"

    async def test_cross_project_child_rejected(self, db):
        await db.create_project(Project(id="other", name="Other"))
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await db.create_task(
            Task(id="x", project_id="other", title="x", description="x", status=TaskStatus.DEFINED)
        )
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["x"], "p", conn=conn)
        assert exc.value.code == "cross_project"

    async def test_depth_cap_enforced(self, db):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "a.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "a.1.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "leaf")
        async with db._engine.begin() as conn:
            await db.set_parent("a.1", "a", conn=conn)
            await db.set_parent("a.1.1", "a.1", conn=conn)
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["leaf"], "a.1.1", conn=conn)
        assert exc.value.code == "depth"

    async def test_child_with_blocking_edges_refuses_the_shortcut(self, db):
        """The skipped DAG walk is only sound for edge-free leaves."""
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "a")
        await mktask(db, "b")
        await db.add_dependency("b", "a", DepType.BLOCKS.value)
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["b"], "p", conn=conn)
        assert exc.value.code == "cycle_check_skipped"

    async def test_child_with_children_refuses_the_shortcut(self, db):
        """... and only for leaves: a taller subtree would break the depth check."""
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "a.1")
        async with db._engine.begin() as conn:
            await db.set_parent("a.1", "a", conn=conn)
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["a"], "p", conn=conn)
        assert exc.value.code == "cycle_check_skipped"

    async def test_parent_in_the_batch_rejected(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent_bulk(["p"], "p", conn=conn)
        assert exc.value.code == "self_parent"


class TestOpenChildrenGuard:
    """Invariant 6 lives in ``_apply_transition``, not only at the surfaces —
    approval, execution and the workflow sync all complete tasks directly."""

    async def test_transition_to_completed_refused_with_open_child(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p.1", status=TaskStatus.READY)
        async with db._engine.begin() as conn:
            await db.set_parent("p.1", "p", conn=conn)
        with pytest.raises(HierarchyError) as exc:
            await db.transition_task("p", TaskStatus.COMPLETED, context="pr_merged")
        assert exc.value.code == "open_children"
        assert "p.1" in exc.value.detail
        # ... and the task is exactly where it was.
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_force_bypasses_the_guard(self, db):
        """Abandonment is administrative and passes ``force=True``."""
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p.1", status=TaskStatus.READY)
        async with db._engine.begin() as conn:
            await db.set_parent("p.1", "p", conn=conn)
        await db.transition_task("p", TaskStatus.COMPLETED, context="admin", force=True)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED

    async def test_terminal_children_do_not_block(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p.1", status=TaskStatus.READY)
        await mktask(db, "p.2", status=TaskStatus.READY)
        async with db._engine.begin() as conn:
            await db.set_parent("p.1", "p", conn=conn)
            await db.set_parent("p.2", "p", conn=conn)
        # FAILED last: a COMPLETED child would settle ``p`` on its own.
        await db.transition_task("p.1", TaskStatus.COMPLETED, context="t")
        await db.transition_task("p.2", TaskStatus.FAILED, context="t")
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS
        await db.transition_task("p", TaskStatus.COMPLETED, context="pr_merged")
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED


class TestAbandonReleasesResources:
    """An abandoned descendant holds nothing (spec §7)."""

    async def test_workspace_lock_and_agent_pointer_are_cleared(self, db):
        from sqlalchemy import insert as sa_insert, select as sa_select

        from src.database.tables import agents as agents_t, workspaces as workspaces_t
        from src.models import Agent

        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p.1", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.set_parent("p.1", "p", conn=conn)

        await db.create_agent(
            Agent(
                id="ag",
                name="ag",
                profile_id="claude",
                state=AgentState.BUSY,
                current_task_id="p.1",
            )
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_insert(workspaces_t).values(
                    id="ws",
                    project_id=PROJECT_ID,
                    workspace_path="/tmp/ws",
                    locked_by_task_id="p.1",
                    locked_by_agent_id="ag",
                    locked_at=1.0,
                    lock_mode="exclusive",
                    created_at=1.0,
                )
            )

        async with db._engine.begin() as conn:
            res = await db.abandon_subtree("p", conn=conn)
        assert res.abandoned == ["p.1"]

        async with db._engine.begin() as conn:
            ws = (
                await conn.execute(
                    sa_select(
                        workspaces_t.c.locked_by_task_id,
                        workspaces_t.c.locked_by_agent_id,
                        workspaces_t.c.locked_at,
                        workspaces_t.c.lock_mode,
                    ).where(workspaces_t.c.id == "ws")
                )
            ).fetchone()
            ag = (
                await conn.execute(
                    sa_select(agents_t.c.current_task_id, agents_t.c.state).where(
                        agents_t.c.id == "ag"
                    )
                )
            ).fetchone()
        assert tuple(ws) == (None, None, None, None)
        assert ag.current_task_id is None
        assert ag.state == AgentState.IDLE.value
