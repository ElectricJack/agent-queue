"""Task 12 (dv2-p1): ``task_route`` command + ``routing`` gate integration.

Adapted from the brief: ``Workspace`` requires ``source_type`` in this
codebase, so the ``test_workspace_must_belong_to_project`` fixture uses
``RepoSourceType.LINK``.
"""
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, RepoSourceType, Task, TaskStatus, Workspace
from src.orchestrator import Orchestrator

PID = "p"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "tr.db"))
    await d.initialize()
    await d.create_project(Project(id=PID, name="P"))
    await d.upsert_profile(
        AgentProfile(
            id="coder",
            name="Coder",
            model="claude-sonnet-4-6",
            harness="claude",
            default_class="",
            needs_workspace=True,
        )
    )
    await d.create_task(
        Task(id="t1", project_id=PID, title="do a thing",
             description="x", status=TaskStatus.DEFINED)
    )
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    from src.vault import ensure_default_intelligence_classes
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "tr.db"),
        data_dir=data_dir,
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


async def test_task_route_happy_path(handler, db):
    gate_id = await db.create_gate(
        project_id=PID, gate_type="routing", title="Route",
        waiter_task_ids=["t1"],
    )
    r = await handler.execute(
        "task_route",
        {"task_id": "t1", "profile_id": "coder", "intelligence_class": "standard"},
    )
    assert r["success"] is True
    assert gate_id in r["resolved_gate_ids"]
    t = await db.get_task("t1")
    assert t.profile_id == "coder"
    assert t.intelligence_class == "standard"
    g = await db.get_gate(gate_id)
    assert g["status"] == "resolved"


async def test_rejects_unknown_profile(handler):
    r = await handler.execute(
        "task_route", {"task_id": "t1", "profile_id": "nope"}
    )
    assert r["success"] is False
    assert "profile" in r["error"].lower()


async def test_rejects_unknown_class(handler):
    r = await handler.execute(
        "task_route",
        {"task_id": "t1", "profile_id": "coder", "intelligence_class": "warp-speed"},
    )
    assert r["success"] is False
    assert "class" in r["error"].lower()


async def test_workspace_must_belong_to_project(handler, db):
    await db.create_project(Project(id="other-project", name="Other"))
    await db.create_workspace(
        Workspace(
            id="w1",
            project_id="other-project",
            workspace_path="/tmp/x",
            source_type=RepoSourceType.LINK,
        )
    )
    r = await handler.execute(
        "task_route",
        {"task_id": "t1", "profile_id": "coder", "workspace_id": "w1"},
    )
    assert r["success"] is False


async def test_gate_resolve_refuses_routing(handler, db):
    gate_id = await db.create_gate(
        project_id=PID, gate_type="routing", title="Route",
        waiter_task_ids=["t1"],
    )
    r = await handler.execute(
        "gate_resolve",
        {"gate_id": gate_id, "resolved_by": "human"},
    )
    assert r["success"] is False
    assert "task_route" in r["error"].lower()
