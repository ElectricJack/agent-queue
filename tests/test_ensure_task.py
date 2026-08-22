from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "p"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "et.db"))
    await d.initialize()
    await d.create_project(Project(id=PROJECT_ID, name="P"))
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "et.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


async def test_ensure_task_creates_when_missing(handler):
    res = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    assert res["success"] is True
    assert res["created"] is True
    assert res["task_id"]


async def test_ensure_task_returns_existing(handler):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Different title"},
    )
    assert r2["success"] is True
    assert r2["created"] is False
    assert r2["task_id"] == r1["task_id"]


async def test_ensure_task_ignores_completed_task(handler, db):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "review-branch-feat", "title": "Review"},
    )
    # Complete r1's task and ensure a fresh one is created.
    await db.transition_task(r1["task_id"], TaskStatus.COMPLETED, force=True)
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "review-branch-feat", "title": "Review"},
    )
    assert r2["created"] is True
    assert r2["task_id"] != r1["task_id"]


async def test_ensure_task_requires_dedup_key(handler):
    res = await handler.execute(
        "ensure_task", {"project_id": PROJECT_ID, "title": "x"}
    )
    assert res.get("success") is False or "error" in res


async def test_ensure_task_reuses_in_progress_task(handler, db):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    await db.transition_task(r1["task_id"], TaskStatus.IN_PROGRESS, force=True)
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    assert r2["created"] is False
    assert r2["task_id"] == r1["task_id"]
