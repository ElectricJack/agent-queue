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
        # The routing gate attaches inside the same transaction, so the new
        # task is blocked by it as soon as the create returns.
        assert new.is_blocked is True
        deps = await db.get_typed_dependencies(new.id)
        assert deps == [("held", "discovered-from")]
        gates = await db.get_gates_for_task(new.id)
        assert [g["gate_type"] for g in gates] == ["routing"]
        assert (await db.get_task("held")).filed_count == 1
        ev = created_events(handler)[0]
        assert (ev["created_by_kind"], ev["filed_by_profile_id"], ev["discovered_from"],
                ev["parent_task_id"]) == ("session", "worker", "held", None)
        # ``log_blocked_flips`` post-commit audit row for the flip the gate
        # caused (task_commands._create_worker_filed_task must collect and
        # log the gate's flip set, not discard it).
        events = await db.get_recent_events(limit=50, task_id=new.id)
        assert "task.blocked" in [e["event_type"] for e in events]

    async def test_child_filing_under_held_task_has_no_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "sub", "description": "d",
                                                            "parent_id": "held"})
        assert res["success"] is True and res.get("gate_id") is None
        assert res["task_id"] == "held.1"
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

    async def test_depends_on_parent_child_edge_rejected(self, handler, db):
        """§12: parenting worker-filed work must go through ``parent_id`` —
        a ``parent-child`` entry smuggled into ``depends_on`` would bypass
        the subtree constraint entirely and must be rejected outright."""
        sid = await holding_session(db)
        await db.create_task(Task(id="elsewhere", project_id=PROJECT_ID, title="e",
                                  description="e", status=TaskStatus.READY))
        res = await scoped(handler, sid)._cmd_create_task({
            "title": "x", "description": "d",
            "depends_on": [{"task_id": "elsewhere", "dep_type": "parent-child"}],
        })
        assert res["success"] is False
        assert "parent_id" in res["error"] or "parent-child" in res["error"]
        # Nothing written at all — this is rejected before the transaction.
        # (held + the pre-existing "elsewhere" task only.)
        assert len(await db.list_tasks(PROJECT_ID)) == 2

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

        # ``_create_worker_filed_task`` calls the private ``_create_gate_on``
        # writer directly (not the public ``create_gate`` wrapper) so it can
        # fold the gate's own ``is_blocked`` flip set into its own
        # post-commit log — patch that entry point.
        monkeypatch.setattr(db, "_create_gate_on", boom)
        with pytest.raises(RuntimeError):
            await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d"})
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_filing_under_completed_container_rolls_back(self, handler, db):
        """A hierarchy error (``container_closed``) from ``set_parent`` must
        surface as a structured error, not a bare ``{"error": ...}``, and
        must not leave partial writes (reserve_filing included)."""
        sid = await holding_session(db)
        await db.transition_task("held", TaskStatus.COMPLETED)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "parent_id": "held"})
        assert res["success"] is False
        assert res["code"] == "hierarchy.container_closed"
        assert "container_closed" in res["error"]
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_elevated_caller_is_unconstrained(self, handler, db):
        handler._current_scope = {"kind": "local", "elevated": True}
        res = await handler._cmd_create_task({"title": "x", "description": "d",
                                              "project_id": PROJECT_ID, "status": "READY"})
        assert res["success"] and (await db.get_task(res["task_id"])).status == TaskStatus.READY


@pytest.mark.parametrize("project_id", [None, PROJECT_ID])
async def test_elevated_session_records_creator_without_worker_quota_or_gate(handler, db, project_id):
    await db.create_session(SessionRecord(
        id="supervisor-session", project_id=project_id, profile_id="supervisor",
        harness="claude", provider="fake", name="n-supervisor--global",
        lifecycle="named", work_dir="/wd", epoch="e", instance_token="t",
        started_at=time.time(), state="running",
    ))
    handler._current_scope = {"kind": "session", "session_id": "supervisor-session",
                              "project_id": project_id, "elevated": True}
    # No held task, and more creations than the worker quota: elevated behavior is unchanged.
    for index in range(3):
        result = await handler._cmd_create_task({
            "title": f"supervisor delegation {index}", "project_id": PROJECT_ID,
            "created_by_kind": "session", "created_by_id": "spoofed-session",
        })
        assert result["success"] is True
        created = await db.get_task(result["task_id"])
        assert (created.created_by_kind, created.created_by_id) == ("session", "supervisor-session")
        assert created.status == TaskStatus.READY
        assert await db.get_gates_for_task(created.id) == []
        # Event markers remain worker-only: supervisor provenance must not trigger triage.
        event = created_events(handler)[-1]
        assert event["created_by_kind"] is None and event["created_by_id"] is None
        assert event["filed_by_profile_id"] is None


async def test_local_task_creation_does_not_accept_spoofed_session_provenance(handler, db):
    handler._current_scope = {"kind": "local"}
    result = await handler._cmd_create_task({
        "title": "operator work", "project_id": PROJECT_ID,
        "created_by_kind": "session", "created_by_id": "spoofed-session",
    })
    created = await db.get_task(result["task_id"])
    assert created.created_by_kind is None and created.created_by_id is None
