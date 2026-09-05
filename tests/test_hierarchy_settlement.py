"""Container settlement — spec §7."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import and_, insert, select, update

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.database.tables import events, task_dependencies, task_integration_checkpoints, tasks
from src.models import DepType, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def family(db, n=2):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    kids = []
    for i in range(n):
        await mktask(db, f"c{i}", status=TaskStatus.READY)
        await db.add_dependency(f"c{i}", "p", "parent-child")
        kids.append(f"c{i}")
    return kids


class TestSettlement:
    async def test_last_child_completion_completes_container_in_same_call(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS
        await db.transition_task(kids[1], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED

    async def test_listener_receives_settled_ids(self, db):
        seen = []

        async def cb(ids):
            seen.append(list(ids))

        db.set_settlement_listener(cb)
        kids = await family(db, n=1)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert seen == [["p"]]

    async def test_live_session_guard(self, db):
        kids = await family(db, n=1)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="p",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-p",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_failed_child_does_not_settle(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        await db.transition_task(kids[1], TaskStatus.FAILED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_disabled_checkpoint_without_episode_retains_legacy_settlement(self, db):
        kids = await family(db, n=1)
        async with db.immediate() as conn:
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id="p",
                    repository_id="repo",
                    branch="aq/p",
                    generation=1,
                    checkpoint_sha="a" * 40,
                    state="awaiting_children",
                    version=0,
                    updated_at=1.0,
                )
            )

        await db.transition_task(kids[0], TaskStatus.COMPLETED)

        assert (await db.get_task("p")).status == TaskStatus.COMPLETED

    async def test_settles_up_to_three_levels(self, db):
        await mktask(db, "g", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("p", "g", "parent-child")
        await db.add_dependency("c", "p", "parent-child")
        await db.transition_task("c", TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("g")).status == TaskStatus.COMPLETED

    async def test_emptied_container_settles_on_reparent(self, db):
        kids = await family(db, n=1)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.set_parent(kids[0], "p2", conn=conn)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p2")).status == TaskStatus.IN_PROGRESS

    async def test_non_container_in_progress_leaf_is_untouched(self, db):
        await mktask(db, "leaf", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            result = await db.settle_containers({"leaf"}, conn=conn)
        assert result.settled == []
        assert result.flipped == set()
        assert (await db.get_task("leaf")).status == TaskStatus.IN_PROGRESS

    async def test_settlement_flip_reaches_caller_and_audit_log(self, db):
        """A ``waits-for`` waiter on ``p`` unblocks in the same call, and the
        flip is both in ``transition_task``'s return value and in the
        ``task.unblocked`` audit log — not dropped on the floor when the
        flip was produced while settling a container (review finding #1).
        """
        kids = await family(db, n=1)
        await mktask(db, "waiter", status=TaskStatus.DEFINED)
        await db.add_dependency("waiter", "p", DepType.WAITS_FOR.value)
        assert (await db.get_task("waiter")).is_blocked is True

        flipped = await db.transition_task(kids[0], TaskStatus.COMPLETED)

        assert "waiter" in flipped
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("waiter")).is_blocked is False

        async with db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(events.c.event_type).where(
                        and_(
                            events.c.task_id == "waiter",
                            events.c.event_type == "task.unblocked",
                        )
                    )
                )
            ).fetchall()
        assert rows, "expected a task.unblocked audit row for 'waiter'"

    async def test_settles_exactly_max_structural_depth_levels(self, db):
        """A 4-deep container chain settles 3 levels up from the completed
        leaf (``MAX_STRUCTURAL_DEPTH``) and stops — the topmost container is
        left untouched (review finding #2).  Built with raw edge/pointer
        inserts because :meth:`Database.set_parent` rejects a chain this
        deep by design.
        """
        for tid in ("p1", "p2", "p3", "p4"):
            await mktask(db, tid, status=TaskStatus.IN_PROGRESS)
        await mktask(db, "leaf", status=TaskStatus.READY)

        async with db._engine.begin() as conn:
            for child, parent in (("leaf", "p1"), ("p1", "p2"), ("p2", "p3"), ("p3", "p4")):
                await conn.execute(
                    insert(task_dependencies).values(
                        task_id=child,
                        depends_on_task_id=parent,
                        dep_type=DepType.PARENT_CHILD.value,
                    )
                )
                await conn.execute(
                    update(tasks).where(tasks.c.id == child).values(parent_task_id=parent)
                )
                await db.mark_container(parent, conn=conn)

        await db.transition_task("leaf", TaskStatus.COMPLETED)

        assert (await db.get_task("p1")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p2")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p3")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p4")).status == TaskStatus.IN_PROGRESS

    async def test_listener_exception_does_not_fail_transition(self, db):
        async def bad_cb(ids):
            raise RuntimeError("boom")

        db.set_settlement_listener(bad_cb)
        kids = await family(db, n=1)

        await db.transition_task(kids[0], TaskStatus.COMPLETED)  # must not raise

        assert (await db.get_task("p")).status == TaskStatus.COMPLETED


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def orch(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    o.bus = MagicMock()
    o.bus.emit = AsyncMock()
    o._emit_text_notify = AsyncMock()
    o._check_workflow_stage_completion = AsyncMock()
    o.register_settlement_listener()
    return o


class TestOrchestratorSettlement:
    async def test_listener_emits_task_completed_and_notifies(self, orch, db):
        kids = await family(db, n=1)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert "task.completed" in emitted
        orch._emit_text_notify.assert_awaited()
        orch._check_workflow_stage_completion.assert_awaited()

    async def test_no_per_tick_scan_method_remains(self, orch):
        assert not hasattr(orch, "_check_plan_parent_completion")

    async def test_backstop_sweep_settles_and_warns(self, orch, db, caplog):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.COMPLETED)
        # Bypass the event path: create the edge with a raw insert so nothing settled.
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id="c", depends_on_task_id="p", dep_type="parent-child"
                )
            )
            await db.mark_container("p", conn=conn)
            await conn.execute(update(tasks).where(tasks.c.id == "c").values(parent_task_id="p"))
        orch._last_container_sweep = 0.0
        with caplog.at_level("WARNING"):
            await orch._sweep_container_completion()
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert "backstop" in caplog.text


class TestSettlementFanOutIsolation:
    """One container's fan-out failure must not cost the next one its own."""

    async def test_failing_container_does_not_abort_the_batch(self, orch, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.mark_container("p1", conn=conn)
            await db.mark_container("p2", conn=conn)

        calls: list[str] = []

        async def flaky(text, project_id=None):
            calls.append(text)
            if "p1" in text:
                raise RuntimeError("notify exploded")

        orch._emit_text_notify = flaky
        await orch._on_containers_settled(["p1", "p2"])

        emitted = [
            c.kwargs.get("task_id") or (c.args[1] if len(c.args) > 1 else None)
            for c in orch.bus.emit.await_args_list
        ]
        # p2's task.completed still went out after p1 blew up.
        assert any("p1" in t for t in calls) and any("p2" in t for t in calls)
        assert len(orch.bus.emit.await_args_list) == 2, emitted
