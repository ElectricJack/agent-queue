"""Worker-filed work — spec §12 constraints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_project(Project(id="other", name="o"))
    yield database
    await database.close()


@pytest.fixture
async def handler(db, tmp_path):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "ws"), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path / "data"))
    cfg.swarm.enabled = True
    cfg.swarm.max_filings_per_task = 2
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    orch.bus.emit = AsyncMock()
    return CommandHandler(orch, cfg)


async def holding_session(db, sid="s1", task_id="held"):
    await db.create_agent(Agent(id="agent-1", name="a", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(Task(id=task_id, project_id=PROJECT_ID, title=task_id, description="x",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id="agent-1",
                              claim_epoch=1))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=sid, lifecycle="pool", work_dir="/wd", epoch="e", instance_token="t",
        started_at=time.time(), state="running", agent_id="agent-1", task_id=task_id,
        claim_phase="active"))
    return sid


def scoped(handler, sid):
    handler._current_scope = {"kind": "session", "session_id": sid, "task_id": None,
                              "project_id": PROJECT_ID, "elevated": False}
    return handler


def created_events(handler):
    return [c.args[1] for c in handler.orchestrator.bus.emit.await_args_list
            if c.args[0] == "task.created"]


class TestFiling:
    async def test_root_filing_gets_discovered_from_and_routing_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "found a bug",
                                                            "description": "d",
                                                            "status": "READY"})
        assert res["success"] is True and res["gate_id"]
        new = await db.get_task(res["task_id"])
        assert (new.status, new.created_by_kind, new.created_by_id, new.project_id) == (
            TaskStatus.DEFINED, "session", sid, PROJECT_ID)
        deps = await db.get_typed_dependencies(new.id)
        assert deps == [("held", "discovered-from")]
        gates = await db.get_gates_for_task(new.id)
        assert [g["gate_type"] for g in gates] == ["routing"]
        assert (await db.get_task("held")).filed_count == 1
        ev = created_events(handler)[0]
        assert (ev["created_by_kind"], ev["filed_by_profile_id"], ev["discovered_from"],
                ev["parent_task_id"]) == ("session", "worker", "held", None)

    async def test_child_filing_under_held_task_has_no_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "sub", "description": "d",
                                                            "parent_id": "held"})
        assert res["success"] is True and res.get("gate_id") is None
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id == "held" and new.id.startswith("held.")

    async def test_project_pin(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "project_id": "other"})
        assert res["success"] is False and "pinned" in res["error"]

    async def test_idle_session_cannot_file(self, handler, db):
        sid = await holding_session(db)
        await db.update_session(sid, task_id=None, claim_phase=None)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d"})
        assert res["success"] is False and res["code"] == "idle_session_cannot_file"

    async def test_parent_outside_subtree_rejected(self, handler, db):
        sid = await holding_session(db)
        await db.create_task(Task(id="elsewhere", project_id=PROJECT_ID, title="e",
                                  description="e", status=TaskStatus.READY))
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "parent_id": "elsewhere"})
        assert res["success"] is False

    async def test_quota_is_enforced_atomically(self, handler, db):
        sid = await holding_session(db)
        h = scoped(handler, sid)
        assert (await h._cmd_create_task({"title": "a", "description": "d"}))["success"]
        assert (await h._cmd_create_task({"title": "b", "description": "d"}))["success"]
        res = await h._cmd_create_task({"title": "c", "description": "d"})
        assert res["success"] is False and res["code"] == "filing_quota_exceeded"
        assert len(await db.list_tasks(PROJECT_ID)) == 3  # held + a + b

    async def test_gate_failure_rolls_back_task(self, handler, db, monkeypatch):
        sid = await holding_session(db)

        async def boom(*a, **k):
            raise RuntimeError("gate write failed")

        monkeypatch.setattr(db, "create_gate", boom)
        with pytest.raises(RuntimeError):
            await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d"})
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_elevated_caller_is_unconstrained(self, handler, db):
        handler._current_scope = {"kind": "local", "elevated": True}
        res = await handler._cmd_create_task({"title": "x", "description": "d",
                                              "project_id": PROJECT_ID, "status": "READY"})
        assert res["success"] and (await db.get_task(res["task_id"])).status == TaskStatus.READY
