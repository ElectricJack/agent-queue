from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, TaskStatus
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


async def test_ensure_task_does_NOT_emit_task_created(handler):
    """Control-plane invariant: ensure_task must NOT emit task.created.

    Emitting would re-fire the default pipeline against the bookkeeping task,
    attaching a routing gate resolvable only by the triage agent — the
    triage task would deadlock blocked on its own gate.  Rationale is
    documented on the suppression site (src/commands/task_commands.py).
    """
    orch = handler.orchestrator
    with patch.object(orch, "_emit_task_event", new=AsyncMock()) as spy_task, \
         patch.object(orch, "_emit_notify", new=AsyncMock()):
        res = await handler.execute(
            "ensure_task",
            {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
        )
    assert res["success"] is True
    assert res["created"] is True
    # Zero task.created (or any other task.*) emissions from the ensure path.
    called_types = [c.args[0] for c in spy_task.call_args_list]
    assert "task.created" not in called_types, called_types


async def test_create_task_DOES_emit_task_created(handler):
    """Foil to the ensure_task suppression test: normal create_task emits."""
    orch = handler.orchestrator
    with patch.object(orch, "_emit_task_event", new=AsyncMock()) as spy_task, \
         patch.object(orch, "_emit_notify", new=AsyncMock()):
        res = await handler.execute(
            "create_task",
            {"project_id": PROJECT_ID, "title": "Do a thing"},
        )
    assert "error" not in res, res
    called_types = [c.args[0] for c in spy_task.call_args_list]
    assert "task.created" in called_types, called_types


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
