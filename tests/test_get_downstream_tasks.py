from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PID = "p"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "gdt.db"))
    await d.initialize()
    await d.create_project(Project(id=PID, name="P"))
    for tid in ("a", "b", "c", "d", "unrelated"):
        await d.create_task(
            Task(id=tid, project_id=PID, title=tid, description=tid, status=TaskStatus.DEFINED)
        )
    # Chain: a <-blocks- b <-parent-child- c ; d waits-for a
    await d.add_dependency("b", "a", DepType.BLOCKS.value)
    await d.add_dependency("c", "b", DepType.PARENT_CHILD.value)
    await d.add_dependency("d", "a", DepType.WAITS_FOR.value)
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "gdt.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


async def test_returns_transitive_dependents(handler):
    res = await handler.execute("get_downstream_tasks", {"task_id": "a"})
    assert res["success"] is True
    ids = sorted(t["id"] for t in res["tasks"])
    assert ids == ["b", "c", "d"]


async def test_ignores_non_blocking_edges(handler, db):
    await db.add_dependency("unrelated", "a", DepType.RELATED.value)
    res = await handler.execute("get_downstream_tasks", {"task_id": "a"})
    ids = sorted(t["id"] for t in res["tasks"])
    assert ids == ["b", "c", "d"]


async def test_returns_empty_for_leaf(handler):
    res = await handler.execute("get_downstream_tasks", {"task_id": "c"})
    assert res["success"] is True
    assert res["tasks"] == []
