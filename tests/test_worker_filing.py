"""Worker-filed work — spec §12 constraints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

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
    async def test_reason_is_required_before_worker_filing_mutates_state(self, handler, db):
        sid = await holding_session(db)

        res = await scoped(handler, sid)._cmd_create_task(
            {"title": "found a bug", "description": "d"}
        )

        assert res["success"] is False
        assert res["code"] == "reason_required"
        assert "why" in res["error"].lower()
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_root_filing_gets_discovered_from_and_routing_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "found a bug",
                                                            "description": "d",
                                                            "reason": "The held task exposed a parser defect",
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
        assert (await db.get_typed_dependencies_detailed(new.id))[0]["description"] == (
            "The held task exposed a parser defect"
        )
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

    async def test_filed_task_is_an_assignment_routing_candidate(self, handler, db):
        """The gate a root filing is born with must not hide it from the router.

        A filing is DEFINED and blocked by its own routing gate; if the
        assignment coordinator does not consider that shape, nothing ever
        picks a class and nothing ever resolves the gate.
        """
        sid = await holding_session(db)
        root = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d",
            "reason": "The held task exposed a parser defect",
        })
        child = await scoped(handler, sid)._cmd_create_task({
            "title": "sub", "description": "d", "parent_id": "held",
            "reason": "This can ship independently",
        })
        assert root["success"] and child["success"]

        candidates = await handler.orchestrator.assignment_routing._eligible_candidates()

        assert {root["task_id"], child["task_id"]} <= {task.id for task in candidates}

    async def test_child_filing_under_held_task_has_no_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "sub", "description": "d",
                                                            "parent_id": "held",
                                                            "reason": "This can ship independently"})
        assert res["success"] is True and res.get("gate_id") is None
        assert res["task_id"] == "held.1"
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id == "held" and new.id.startswith("held.")
        assert (await db.get_typed_dependencies_detailed(new.id))[0]["description"] == (
            "This can ship independently"
        )

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
            "title": "x", "description": "d", "reason": "Discovered invalid nesting",
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
        assert (await h._cmd_create_task({"title": "a", "description": "d", "reason": "one"}))["success"]
        assert (await h._cmd_create_task({"title": "b", "description": "d", "reason": "two"}))["success"]
        res = await h._cmd_create_task({"title": "c", "description": "d", "reason": "three"})
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
            await scoped(handler, sid)._cmd_create_task(
                {"title": "x", "description": "d", "reason": "The task exposed this"}
            )
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_filing_under_completed_container_rolls_back(self, handler, db):
        """A hierarchy error (``container_closed``) from ``set_parent`` must
        surface as a structured error, not a bare ``{"error": ...}``, and
        must not leave partial writes (reserve_filing included)."""
        sid = await holding_session(db)
        await db.transition_task("held", TaskStatus.COMPLETED)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "parent_id": "held",
                                                            "reason": "Split before closing"})
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


async def holding_child_session(db, sid="s1", epic_id="epic", task_id="epic.1"):
    """A pool session holding ``epic.1``, a child of the container ``epic``."""
    await db.create_task(Task(id=epic_id, project_id=PROJECT_ID, title="epic", description="e",
                              status=TaskStatus.IN_PROGRESS))
    await holding_session(db, sid=sid, task_id=task_id)
    async with db._engine.begin() as conn:
        await db.set_parent(task_id, epic_id, conn=conn)
    return sid


class TestSiblingFiling:
    """Emergent work found under a child task is filed as its sibling (§12)."""

    async def test_default_filing_from_child_becomes_sibling_with_both_edges(self, handler, db):
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d",
            "reason": "epic.1 exposed a parser defect",
        })

        assert res["success"] is True
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id == "epic"
        assert new.id.startswith("epic.") and new.id != "epic.1"
        assert new.status == TaskStatus.DEFINED
        deps = await db.get_typed_dependencies(new.id)
        assert ("epic", "parent-child") in deps
        assert ("epic.1", "discovered-from") in deps
        assert len(deps) == 2
        by_target = {
            (d["depends_on_task_id"], d["dep_type"]): d["description"]
            for d in await db.get_typed_dependencies_detailed(new.id)
        }
        assert by_target[("epic", "parent-child")] == "epic.1 exposed a parser defect"
        assert by_target[("epic.1", "discovered-from")] == "epic.1 exposed a parser defect"
        assert (await db.get_task("epic.1")).filed_count == 1

    async def test_sibling_filing_has_no_root_routing_gate(self, handler, db):
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d", "reason": "epic.1 exposed it",
        })

        assert res["success"] is True and res.get("gate_id") is None
        assert await db.get_gates_for_task(res["task_id"]) == []

    async def test_sibling_filing_event_reports_parent_and_origin(self, handler, db):
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d", "reason": "epic.1 exposed it",
        })

        assert res["success"] is True
        ev = created_events(handler)[0]
        assert (ev["parent_task_id"], ev["discovered_from"], ev["created_by_kind"]) == (
            "epic", "epic.1", "session")

    async def test_explicit_immediate_parent_is_allowed(self, handler, db):
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d", "parent_id": "epic",
            "reason": "epic.1 exposed it",
        })

        assert res["success"] is True
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id == "epic"
        deps = await db.get_typed_dependencies(new.id)
        assert ("epic", "parent-child") in deps and ("epic.1", "discovered-from") in deps

    async def test_grandparent_and_aunt_remain_rejected(self, handler, db):
        """Only the *immediate* parent opens up — not arbitrary ancestors or
        the parent's other children."""
        await db.create_task(Task(id="grand", project_id=PROJECT_ID, title="g", description="g",
                                  status=TaskStatus.IN_PROGRESS))
        await db.create_task(Task(id="grand.1", project_id=PROJECT_ID, title="e", description="e",
                                  status=TaskStatus.IN_PROGRESS))
        await db.create_task(Task(id="grand.2", project_id=PROJECT_ID, title="aunt",
                                  description="a", status=TaskStatus.READY))
        sid = await holding_session(db, task_id="grand.1.1")
        async with db._engine.begin() as conn:
            await db.set_parent("grand.1", "grand", conn=conn)
            await db.set_parent("grand.2", "grand", conn=conn)
            await db.set_parent("grand.1.1", "grand.1", conn=conn)
        h = scoped(handler, sid)

        for bad in ("grand", "grand.2"):
            res = await h._cmd_create_task({"title": "x", "description": "d", "parent_id": bad,
                                            "reason": "r"})
            assert res["success"] is False, bad
            assert "parent" in res["error"]
        assert (await db.get_task("grand.1.1")).filed_count == 0

    async def test_explicit_held_task_as_parent_still_nests(self, handler, db):
        """``--parent <held>`` keeps making a grandchild; the sibling default
        only applies when no parent is supplied."""
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "sub", "description": "d", "parent_id": "epic.1",
            "reason": "This can ship independently",
        })

        assert res["success"] is True
        assert res["task_id"] == "epic.1.1"
        deps = await db.get_typed_dependencies(res["task_id"])
        # The parent-child edge to the held task already carries provenance;
        # no redundant discovered-from edge to the same target.
        assert deps == [("epic.1", "parent-child")]
        ev = created_events(handler)[0]
        assert (ev["parent_task_id"], ev["discovered_from"]) == ("epic.1", "epic.1")

    async def test_root_held_task_keeps_root_filing_behaviour(self, handler, db):
        sid = await holding_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "found a bug", "description": "d", "reason": "held exposed it",
        })

        assert res["success"] is True and res["gate_id"]
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id is None and "." not in new.id
        assert await db.get_typed_dependencies(new.id) == [("held", "discovered-from")]

    async def test_explicit_root_from_child_bypasses_sibling_default(self, handler, db):
        """``root=True`` means project root even while the worker holds a child."""
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "cross-cutting bug", "description": "d", "root": True,
            "reason": "epic.1 exposed a project-wide parser defect",
        })

        assert res["success"] is True and res["gate_id"]
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id is None and "." not in new.id
        assert (new.status, new.is_blocked) == (TaskStatus.DEFINED, True)
        assert await db.get_typed_dependencies(new.id) == [
            ("epic.1", "discovered-from")
        ]
        detail = await db.get_typed_dependencies_detailed(new.id)
        assert detail[0]["description"] == "epic.1 exposed a project-wide parser defect"
        assert [g["gate_type"] for g in await db.get_gates_for_task(new.id)] == ["routing"]
        event = created_events(handler)[0]
        assert (event["parent_task_id"], event["discovered_from"]) == (None, "epic.1")
        assert (await db.get_task("epic.1")).filed_count == 1

    async def test_explicit_root_and_parent_are_rejected_before_mutation(self, handler, db):
        sid = await holding_child_session(db)

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "ambiguous", "description": "d", "root": True,
            "parent_id": "epic.1", "reason": "epic.1 exposed it",
        })

        assert res["success"] is False
        assert "mutually exclusive" in res["error"]
        assert {task.id for task in await db.list_tasks(PROJECT_ID)} == {"epic", "epic.1"}
        assert (await db.get_task("epic.1")).filed_count == 0

    async def test_explicit_root_does_not_widen_discovered_from_scope(self, handler, db):
        sid = await holding_child_session(db)
        await db.create_task(Task(
            id="elsewhere", project_id=PROJECT_ID, title="e", description="e",
            status=TaskStatus.READY,
        ))

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "out of scope", "description": "d", "root": True,
            "discovered_from": "elsewhere", "reason": "claimed provenance",
        })

        assert res["success"] is False
        assert "discovered_from" in res["error"]
        assert {task.id for task in await db.list_tasks(PROJECT_ID)} == {
            "epic", "epic.1", "elsewhere",
        }
        assert (await db.get_task("epic.1")).filed_count == 0

    async def test_sibling_default_at_naming_depth_cap_does_not_fall_back(self, handler, db):
        """A held task at the naming-depth cap (``a.b.c``) still gets a real
        sibling (``a.b.N``) — the sibling default never trips the cap that an
        explicit ``--parent <held>`` would."""
        await db.create_task(Task(id="a", project_id=PROJECT_ID, title="a", description="a",
                                  status=TaskStatus.IN_PROGRESS))
        await db.create_task(Task(id="a.1", project_id=PROJECT_ID, title="b", description="b",
                                  status=TaskStatus.IN_PROGRESS))
        sid = await holding_session(db, task_id="a.1.1")
        async with db._engine.begin() as conn:
            await db.set_parent("a.1", "a", conn=conn)
            await db.set_parent("a.1.1", "a.1", conn=conn)
        h = scoped(handler, sid)

        sibling = await h._cmd_create_task({"title": "s", "description": "d", "reason": "r1"})
        assert sibling["success"] is True
        assert sibling["task_id"].startswith("a.1.") and sibling["task_id"] != "a.1.1"
        assert (await db.get_task(sibling["task_id"])).parent_task_id == "a.1"

        capped = await h._cmd_create_task({"title": "c", "description": "d", "reason": "r2",
                                           "parent_id": "a.1.1"})
        assert capped["success"] is True
        assert "." not in capped["task_id"]
        assert (await db.get_task(capped["task_id"])).parent_task_id is None
        assert await db.get_typed_dependencies(capped["task_id"]) == [("a.1.1", "discovered-from")]
        ev = created_events(handler)[-1]
        assert (ev["parent_task_id"], ev["discovered_from"]) == (None, "a.1.1")

    async def test_sibling_filing_under_closed_epic_rolls_back(self, handler, db):
        sid = await holding_child_session(db)
        # ``transition_task`` refuses to close a container with open children;
        # model the race the guard exists for (epic closed by another writer
        # between the scope check and ``set_parent``) with a direct update.
        from sqlalchemy import update

        from src.database.tables import tasks

        async with db._engine.begin() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == "epic").values(status="COMPLETED")
            )

        res = await scoped(handler, sid)._cmd_create_task({
            "title": "x", "description": "d", "reason": "epic.1 exposed it",
        })

        assert res["success"] is False
        assert res["code"] == "hierarchy.container_closed"
        assert {t.id for t in await db.list_tasks(PROJECT_ID)} == {"epic", "epic.1"}
        assert (await db.get_task("epic.1")).filed_count == 0

    async def test_sibling_provenance_edge_failure_rolls_back_everything(
        self, handler, db, monkeypatch
    ):
        """If the discovered-from write fails after the parent-child edge, the
        task row, the parent-child edge and the quota reservation all roll back."""
        sid = await holding_child_session(db)
        real_add = db.add_dependency

        async def boom(task_id, depends_on, dep_type="blocks", **kw):
            if dep_type == "discovered-from":
                raise RuntimeError("provenance write failed")
            return await real_add(task_id, depends_on, dep_type, **kw)

        monkeypatch.setattr(db, "add_dependency", boom)
        with pytest.raises(RuntimeError):
            await scoped(handler, sid)._cmd_create_task({
                "title": "x", "description": "d", "reason": "epic.1 exposed it",
            })
        assert {t.id for t in await db.list_tasks(PROJECT_ID)} == {"epic", "epic.1"}
        assert (await db.get_task("epic.1")).filed_count == 0
        async with db._engine.begin() as conn:
            assert set(await db.subtree_ids("epic", conn=conn)) == {"epic", "epic.1"}


def _cli_client(captured_args: dict):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def execute(command, args=None):
        if command == "create_task":
            captured_args.update(args or {})
            return {"created": "root-task", "title": (args or {}).get("title", "")}
        return {}

    client.execute = AsyncMock(side_effect=execute)
    return client


class TestRootFilingCLI:
    def test_root_is_forwarded_to_single_task_creation(self):
        from src.cli.app import cli

        captured_args: dict = {}
        client = _cli_client(captured_args)

        with patch("src.cli.tasks._get_client", return_value=client):
            result = CliRunner().invoke(cli, [
                "task", "create", "--project", PROJECT_ID, "--title", "T",
                "--description", "D", "--reason", "Found elsewhere", "--root",
            ])

        assert result.exit_code == 0, result.output
        assert captured_args["root"] is True
        assert "parent_id" not in captured_args

    def test_root_and_parent_conflict_is_rejected_before_daemon_call(self):
        from src.cli.app import cli

        captured_args: dict = {}
        client = _cli_client(captured_args)

        with patch("src.cli.tasks._get_client", return_value=client):
            result = CliRunner().invoke(cli, [
                "task", "create", "--project", PROJECT_ID, "--title", "T",
                "--description", "D", "--parent", "epic", "--root",
            ])

        assert result.exit_code == 2
        assert "mutually exclusive" in result.output
        client.execute.assert_not_awaited()

    def test_root_is_rejected_for_graph_creation(self):
        from src.cli.app import cli

        captured_args: dict = {}
        client = _cli_client(captured_args)

        with patch("src.cli.tasks._get_client", return_value=client):
            result = CliRunner().invoke(cli, [
                "task", "create", "--project", PROJECT_ID, "--from-spec", "spec.md",
                "--root",
            ])

        assert result.exit_code == 2
        assert "--root only applies to single-task creation" in result.output
        client.execute.assert_not_awaited()
