"""Stuck-task observations and the repeated, notification-only system timer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts import CONTRACTS, ContractRegistry
from src.commands.contracts.builtin import GetStuckTasksArgs, set_handler_provider
from src.commands.contracts.models import CommandResult
from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL, principal_context
from src.database import tables as t
from src.database.queries.stall_watch_queries import read_snapshot
from src.models import Agent, AgentState, TaskStatus
from src.playbooks.definition import granted_aq_commands, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.private_values import PrivateValueService
from src.profiles.capabilities import CapabilityPolicy
from src.stall_watch import Snapshot, detect
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    artifact_ref_for,
)
from tests.test_get_stuck_tasks_command import (
    _task,
    config as config,
    db as db,
    handler as handler,
)

NOW = 1_800_000_000.0


def task(tid="task", status="IN_PROGRESS", age=3600, **kwargs):
    return {"id": tid, "project_id": "proj", "status": status, "updated_at": NOW - age,
            "parent_task_id": None, "claim_epoch": 1, "assigned_agent_id": "agent", **kwargs}


def snapshot(*tasks, **kwargs):
    return Snapshot(tasks={t["id"]: t for t in tasks}, **kwargs)


def findings(snap):
    return detect(snap, NOW, assigned=1800, in_progress=7200)


@pytest.mark.parametrize("status", ["ASSIGNED", "IN_PROGRESS"])
def test_orphan_age_threshold_and_epic_exclusion(status):
    assert findings(snapshot(task(status=status)))[0]["kind"] == "ORPHANED"
    assert not findings(snapshot(task(status=status, age=1200)))
    assert not findings(snapshot(task(status=status), containers={"task"}))


def working_snapshot():
    return snapshot(task(), agents={"agent": {"state": "BUSY", "current_task_id": "task",
                                             "last_heartbeat": NOW - 10}}, sessions=[{
        "agent_id": "agent", "task_id": "task", "lifecycle": "pool", "state": "running",
        "started_at": NOW - 7200, "last_activity": NOW - 10, "last_claim_epoch": 1,
    }])


def test_live_holder_is_not_orphaned():
    assert not findings(working_snapshot())


@pytest.mark.parametrize("target,patch", [
    ("agent", {"state": "IDLE"}),
    ("agent", {"current_task_id": "other"}),
    ("agent", {"last_heartbeat": NOW - 1201}),
    ("session", {"last_activity": NOW - 1201}),
    ("session", {"last_claim_epoch": 0}),
    ("session", {"state": "stopped"}),
    ("session", {"ended_at": NOW - 1}),
    ("session", {"task_id": "other"}),
])
def test_orphan_worker_failure_modes(target, patch):
    snap = working_snapshot()
    (snap.agents["agent"] if target == "agent" else snap.sessions[0]).update(patch)
    assert findings(snap)[0]["kind"] == "ORPHANED"


@pytest.mark.parametrize("code", ["route_no_candidates", "frontier_route_not_claimable"])
def test_ready_route_failure_requires_thirty_minutes(code):
    snap = snapshot(task(status="READY", age=1801), reasons={"task": [{"code": code}]})
    assert findings(snap)[0]["kind"] == "STUCK READY"
    snap.tasks["task"]["updated_at"] = NOW - 1800
    assert not findings(snap)


@pytest.mark.parametrize("code", ["blocked_dependency", "no_idle_agent", "awaiting_pool_session"])
def test_normal_waits_are_not_failed_routes(code):
    assert not findings(snapshot(task(status="READY"), reasons={"task": [{"code": code}]}))


async def seed_task(db, tid="task", status=TaskStatus.IN_PROGRESS, age=3600, **kwargs):
    await db.create_task(_task(tid, status=status, **kwargs))
    async with db._engine.begin() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == tid).values(updated_at=NOW - age))


async def test_real_command_readonly_orphan_and_summary(handler, db):
    await seed_task(db)
    for _ in range(2):
        result = await handler.execute("get_stuck_tasks", {"now": NOW})
        assert result["success"] and result["stuck"][0]["kind"] == "ORPHANED"
        assert result["summary"].startswith("proj task [IN_PROGRESS, 60m] ORPHANED:")
        assert "aq task explain --task-id task" in result["summary"]
    assert (await db.get_task("task")).status == TaskStatus.IN_PROGRESS
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(t.messages.c.id))).all()
        assert not (await conn.execute(select(t.system_config.c.key).where(
            t.system_config.c.key.like("%stuck%"),
        ))).all()


async def test_command_honors_actual_worker_and_heartbeat(handler, db):
    await db.create_agent(Agent(id="agent", name="agent", profile_id="worker", state=AgentState.BUSY))
    await seed_task(db, assigned_agent_id="agent")
    async with db._engine.begin() as conn:
        await conn.execute(update(t.agents).where(t.agents.c.id == "agent").values(
            current_task_id="task", last_heartbeat=NOW - 10,
        ))
        await conn.execute(insert(t.sessions).values(
            id="session", agent_id="agent", task_id="task", project_id="proj", profile_id="worker",
            harness="codex", provider="fake", name="s-task", lifecycle="task", state="running",
            work_dir="/tmp", epoch="epoch", instance_token="token", started_at=NOW - 3600,
            last_activity=NOW - 10,
        ))
    assert not (await handler.execute("get_stuck_tasks", {"now": NOW}))["stuck"]
    async with db._engine.begin() as conn:
        await conn.execute(update(t.agents).values(last_heartbeat=NOW - 1201))
    assert (await handler.execute("get_stuck_tasks", {"now": NOW}))["stuck"][0]["kind"] == "ORPHANED"


async def test_command_excludes_declared_and_actual_containers(handler, db):
    await seed_task(db, "declared")
    async with db._engine.begin() as conn:
        await conn.execute(insert(t.task_metadata).values(task_id="declared", key="declared_container", value="true"))
    await seed_task(db, "parent")
    await seed_task(db, "parent.1", status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent.1").values(
            parent_task_id="parent",
        ))
    assert not (await handler.execute("get_stuck_tasks", {"now": NOW}))["stuck"]


@pytest.mark.parametrize("paused", ["status", "scheduling"])
async def test_command_skips_paused_projects(handler, db, paused):
    await seed_task(db)
    async with db._engine.begin() as conn:
        if paused == "status":
            await conn.execute(update(t.projects).values(status="PAUSED"))
        else:
            await conn.execute(insert(t.project_constraints).values(project_id="proj", pause_scheduling=1, created_at=NOW))
    assert not (await handler.execute("get_stuck_tasks", {"now": NOW}))["stuck"]


async def test_human_gate_on_ancestor_suppresses_descendant(handler, db):
    await seed_task(db, "parent")
    await seed_task(db, "child")
    async with db._engine.begin() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "child").values(parent_task_id="parent"))
        await conn.execute(insert(t.gates).values(
            id="human", project_id="proj", gate_type="human", title="Approve", created_at=NOW,
        ))
        await conn.execute(insert(t.task_gates).values(task_id="parent", gate_id="human"))
    snap = await read_snapshot(db)
    assert snap.held == {"parent", "child"}
    assert not (await handler.execute("get_stuck_tasks", {"now": NOW}))["stuck"]


async def test_old_ready_uses_explain_reasons_and_failed_read_fails_tick(handler, db, monkeypatch):
    await seed_task(db, status=TaskStatus.READY)
    explain = AsyncMock(return_value={"success": True, "reasons": [
        {"code": "route_no_candidates", "detail": "No eligible provider\nroute"},
    ]})
    monkeypatch.setattr(handler, "_cmd_explain_task", explain)
    result = await handler.execute("get_stuck_tasks", {"now": NOW})
    assert result["stuck"][0]["kind"] == "STUCK READY"
    assert len(result["summary"].splitlines()) == 1
    explain.return_value = {"success": False, "error": "read unavailable"}
    assert not (await handler.execute("get_stuck_tasks", {"now": NOW}))["success"]


async def test_scoped_project_cannot_read_other_project(handler):
    with principal_context(ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK, project_id="proj",
        policy=CapabilityPolicy(aq_commands=frozenset({"get_stuck_tasks"})),
    )):
        result = await handler._cmd_get_stuck_tasks({"project_id": "other"})
    assert result["success"] is False


async def test_read_contract_roundtrip():
    handler = SimpleNamespace(execute=AsyncMock(return_value={
        "success": True, "stuck": [], "summary": "", "now_used": NOW, "thresholds": {},
    }))
    registration = CONTRACTS.require("get_stuck_tasks")
    assert registration.contract.execution.side_effect.value == "read"
    set_handler_provider(lambda: handler)
    try:
        result = await registration.invoke(GetStuckTasksArgs(), None)
        assert result.outcome == "read" and result.value.stuck == []
    finally:
        set_handler_provider(None)


@pytest.mark.parametrize("has_findings", [False, True])
async def test_timer_dry_run_and_repeated_ticks(has_findings):
    definition = load_definition_json(Path(
        "src/prompts/reviewed_playbooks/stuck-task-watch/artifact.json",
    ).read_text())
    assert definition.rules[0].trigger.event_type == "timer.10m"
    assert granted_aq_commands(definition) == {"get_stuck_tasks", "message_send"}
    store = InMemoryArtifactStore()
    store.put(definition)
    registry = ContractRegistry()
    calls = []
    for name in ("get_stuck_tasks", "message_send"):
        registration = CONTRACTS.require(name)

        async def preview(args, _principal, name=name, registration=registration):
            calls.append((name, args.model_dump()))
            value = ({"stuck": [{"id": "task"}] if has_findings else [], "summary": "one line"}
                     if name == "get_stuck_tasks" else {"message_id": "msg", "state": "queued"})
            return CommandResult(outcome="read" if name == "get_stuck_tasks" else "queued",
                                 value=registration.contract.execution.result_model(**value), summary="preview")

        registry.register(replace(registration, preview=preview, contract=registration.contract.model_copy(
            update={"execution": registration.contract.execution.model_copy(update={"supports_preview": True})},
        )))
    engine = PlaybookEngine(services=EngineServices(contracts=registry, artifact_store=store, clock=lambda: NOW),
                            runs=RecordingRunRepository())
    for tick in range(2):
        tree = await engine.dry_run(artifact_ref_for(definition), {
            "event_type": "timer.10m", "event_id": f"tick-{tick}",
        }, TRUSTED_LOCAL)
        assert not tree.truncated and len(tree.paths) == 1 and tree.paths[0].completed, tree
        assert tree.paths[0].nodes[-1].outcome == "completed", tree
    messages = [args for name, args in calls if name == "message_send"]
    assert len(messages) == (2 if has_findings else 0), (calls, tree)
    for message in messages:
        assert message["to_kind"] == "session" and message["to_id"] == "supervisor-global"
        assert message["body"] == "one line"


async def test_live_timer_queues_each_tick_then_stops_when_resolved(handler, db, monkeypatch):
    import src.commands.system_commands as system_commands

    monkeypatch.setattr(system_commands, "time", SimpleNamespace(time=lambda: NOW))
    await seed_task(db)
    definition = load_definition_json(Path(
        "src/prompts/reviewed_playbooks/stuck-task-watch/artifact.json",
    ).read_text())
    store = InMemoryArtifactStore()
    store.put(definition)
    # A3 requires configured protected storage before a step binds diagnostic
    # data. Exercise the real command with a disposable encryption key.
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    engine = PlaybookEngine(
        services=EngineServices(contracts=CONTRACTS, artifact_store=store, clock=lambda: NOW,
                                private_values=private),
        runs=RecordingRunRepository(),
    )
    set_handler_provider(lambda: handler)
    try:
        for tick in range(3):
            if tick == 2:
                async with db._engine.begin() as conn:
                    await conn.execute(update(t.tasks).values(status="COMPLETED"))
            result = await engine.run_rule(
                artifact_ref_for(definition), "watch-stalled-work",
                {"event_type": "timer.10m", "event_id": f"tick-{tick}"}, TRUSTED_LOCAL,
            )
            assert result.outcome == "completed", result
    finally:
        set_handler_provider(None)
    async with db._engine.connect() as conn:
        messages = (await conn.execute(select(t.messages))).mappings().all()
    assert len(messages) == 2
    assert all(m["to_id"] == "supervisor-global" and m["project_id"] is None for m in messages)
    assert messages[0]["body"] == messages[1]["body"]
    assert "ORPHANED" in messages[0]["body"]


@pytest.mark.parametrize("code", ["frontier_sibling_prerequisite_not_delivered",
                                  "frontier_prerequisite_not_on_default_branch",
                                  "frontier_epic_refresh_pending"])
def test_delivery_hold_is_waiting_on_delivery_even_with_route_pressure(code):
    snap = snapshot(task(status="READY"), reasons={"task": [
        {"code": code, "detail": "Land source.6 on dev; refresh aq/epic"},
        {"code": "route_no_candidates", "detail": "No worker"},
    ]})
    [finding] = findings(snap)
    assert finding["kind"] == "WAITING-ON-DELIVERY"
    assert "source.6" in finding["reason"] and "dev" in finding["reason"]
