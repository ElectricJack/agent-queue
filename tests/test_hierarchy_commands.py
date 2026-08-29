"""Container-close semantics and the hierarchy command surface — spec §7, §14."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, DepType, Project, SessionRecord, Task, TaskStatus
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
    orchestrator.complete_session_task = AsyncMock(return_value={"status": "COMPLETED"})
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


async def container_with_open_child(db):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "c", status=TaskStatus.READY)
    await db.add_dependency("c", "p", "parent-child")


class TestCloseRefusals:
    async def test_task_close_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close({"task_id": "p", "outcome": "pass", "summary": "x"})
        assert res["success"] is False
        assert res["code"] == "hierarchy.open_children"
        assert res["open_children"] == ["c"]

    async def test_set_task_status_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_set_task_status({"task_id": "p", "status": "COMPLETED"})
        assert res.get("code") == "hierarchy.open_children"

    async def test_skip_refuses_open_children(self, handler, db):
        await mktask(db, "p", status=TaskStatus.BLOCKED)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        res = await handler._cmd_skip_task({"task_id": "p"})
        assert "open_children" in res["error"]


class TestAbandonChildren:
    async def test_abandons_when_no_live_descendants(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED
        assert await db.get_task_meta("c", "work_outcome") == "abandoned"

    async def test_refused_while_descendant_has_live_session(self, handler, db):
        await container_with_open_child(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="c",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-c",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["code"] == "hierarchy.live_descendants"
        assert res["sessions"] == [{"session_id": "s1", "task_id": "c"}]
        assert (await db.get_task("c")).status == TaskStatus.READY

    async def test_summary_refusal_precedes_abandon(self, handler, db):
        """A refused close (missing summary) must never abandon anything —
        the summary check runs before the container-close block (review
        finding #1)."""
        await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS, profile_id="worker")
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "abandon_children": True}
        )
        assert res["success"] is False
        assert "summary is required" in res["error"]
        assert (await db.get_task("c")).status == TaskStatus.READY

    async def test_abandon_settlement_flip_reaches_events(self, handler, db):
        """A sibling ``blocks``-dependent on the abandoned child unblocks in
        the same call, and the flip lands in the ``task.unblocked`` audit
        log — not dropped when produced inside ``abandon_subtree`` (review
        finding #2)."""
        await container_with_open_child(db)
        await mktask(db, "sib", status=TaskStatus.DEFINED)
        await db.add_dependency("sib", "c", DepType.BLOCKS.value)
        assert (await db.get_task("sib")).is_blocked is True

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert (await db.get_task("sib")).is_blocked is False

        rows = await db.get_recent_events(event_type="task.unblocked", task_id="sib")
        assert rows, "expected a task.unblocked audit row for 'sib'"

    async def test_abandon_forces_invalid_transition(self, handler, db):
        """A PAUSED descendant has no ordinary path to COMPLETED; the abandon
        must pass ``force=True`` so it succeeds even with state-machine
        enforcement on (review finding #3)."""
        await container_with_open_child(db)
        await db.transition_task("c", TaskStatus.PAUSED, context="test-setup", force=True)
        assert (await db.get_task("c")).status == TaskStatus.PAUSED
        db.set_state_machine_enforcement(True)

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED

    async def test_abandons_deepest_first_multi_level(self, handler, db):
        """container -> child -> grandchild, only the grandchild open: both
        the child (now-emptied container) and grandchild close, deepest
        first (review finding #7)."""
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "g", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await db.add_dependency("g", "c", "parent-child")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert set(res["abandoned"]) == {"c", "g"}
        assert res["abandoned"].index("g") < res["abandoned"].index("c")
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED
        assert (await db.get_task("g")).status == TaskStatus.COMPLETED
        assert await db.get_task_meta("g", "work_outcome") == "abandoned"
        assert await db.get_task_meta("c", "work_outcome") == "abandoned"

    async def test_close_with_all_terminal_children_needs_no_flag(self, handler, db):
        """A container whose children are already terminal closes normally —
        no ``abandon_children`` needed, and nothing is reported abandoned.

        Uses a FAILED child rather than COMPLETED: a COMPLETED child would
        already have settled ``p`` via the ordinary settlement cascade
        (spec §7), leaving nothing left to close by the time this test
        calls ``_cmd_task_close`` — FAILED is terminal for the close-refusal
        check but does not participate in that cascade.
        """
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.FAILED)
        await db.add_dependency("c", "p", "parent-child")

        res = await handler._cmd_task_close({"task_id": "p", "outcome": "pass", "summary": "x"})
        assert res["success"] is True
        assert res["abandoned"] == []


class TestCascadeDeleteLiveDescendants:
    """Cascade delete refuses rather than pulling a live session out from
    under a grandchild (spec §7, controller ruling on task 7 review)."""

    async def _grandchild_tree(self, db):
        await mktask(db, "p", status=TaskStatus.DEFINED)
        await mktask(db, "c", status=TaskStatus.READY)
        await mktask(db, "gc", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await db.add_dependency("gc", "c", "parent-child")

    async def test_refused_while_grandchild_has_live_session(self, handler, db):
        await self._grandchild_tree(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="gc",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-gc",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )

        res = await handler._cmd_delete_task({"task_id": "p", "cascade": True})
        assert res["success"] is False
        assert res["code"] == "hierarchy.live_descendants"
        assert res["sessions"] == [{"session_id": "s1", "task_id": "gc"}]

        # Nothing was deleted.
        assert await db.get_task("p") is not None
        assert await db.get_task("c") is not None
        assert await db.get_task("gc") is not None

    async def test_succeeds_once_session_stopped(self, handler, db):
        await self._grandchild_tree(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="gc",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-gc",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        await db.update_session("s1", state="stopped")

        res = await handler._cmd_delete_task({"task_id": "p", "cascade": True})
        assert res == {"deleted": "p", "title": "p"}

        assert await db.get_task("p") is None
        assert await db.get_task("c") is None
        assert await db.get_task("gc") is None
