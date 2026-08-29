"""Container-close semantics and the hierarchy command surface — spec §7, §14."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, SessionRecord, Task, TaskStatus
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
