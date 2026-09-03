"""Routing gates must exist before newly created work becomes visible."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.manager import PlaybookManager
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.routing import install_routing_activation_snapshot
from src.vault import ensure_default_intelligence_classes
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.test_routing_admission_v2 import RecordingStore, SHA, _routing_artifact

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

LEGACY_ROUTING_PIPELINE = """---
id: legacy-routing-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers: [task.created]
---
```json
{"rules":[{"id":"legacy-routing","on":"task.created","when":{"field":"event.task.profile_id","is_null":true},"entry":"gate","nodes":{"gate":{"command":"gate_create","args":{"project_id":"{{event.project_id}}","gate_type":"routing","title":"Route task","waiter_task_ids":["{{event.task_id}}"]},"on_success":"triage","on_failure":"done"},"triage":{"command":"ensure_task","args":{"project_id":"{{event.project_id}}","dedup_key":"triage-open","title":"Triage","profile_id":"triage"},"on_success":"done","on_failure":"done"},"done":{"terminal":true}}}]}
```
"""


def test_cached_system_default_does_not_restore_legacy_assignment_admission():
    from src.playbooks.routing import requires_routing_gate

    config = AppConfig()
    manager = PlaybookManager(config=config)
    playbook = compile_pipeline(
        LEGACY_ROUTING_PIPELINE.replace("id: legacy-routing-pipeline", "id: default-pipeline")
    ).playbook
    manager._active[playbook.id] = playbook
    manager._index_triggers(playbook)

    task = Task(id="new", project_id="p", title="New", description="")
    assert not requires_routing_gate(manager, task)


@pytest.fixture(params=["sqlite", "postgres"])
async def setup(tmp_path, request):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "admission.db"))
        await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(
        AgentProfile(
            id="coder",
            name="Coder",
            harness="codex",
            model="gpt-5.6-sol",
            default_class="deep-high",
            needs_workspace=False,
        )
    )
    await db.create_agent(Agent(id="worker", name="Worker", profile_id="coder"))
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(data_dir=data_dir, database_path=str(tmp_path / "admission.db"))
    config.playbooks.enabled = True
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    orch._emit_task_event = AsyncMock()
    handler = CommandHandler(orch, config)
    manager = PlaybookManager(config=config)
    pb = compile_pipeline(LEGACY_ROUTING_PIPELINE).playbook
    manager._active[pb.id] = pb
    manager._index_triggers(pb)
    artifact = _routing_artifact(artifact_id=pb.id)
    install_routing_activation_snapshot(
        manager,
        [
            {
                "playbook_id": artifact.id,
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": SHA,
                "enabled": True,
                "health": "ready",
            }
        ],
        artifact_store=RecordingStore({SHA: artifact}),
    )
    orch.playbook_manager = manager
    yield handler, db, manager, pb
    await db.close()


@pytest.mark.parametrize("parented", [False, True])
async def test_gate_precedes_creation_event_and_any_claim(setup, parented):
    handler, db, _, _ = setup
    args = {"project_id": "p", "title": "Must be routed first"}
    if parented:
        await db.create_task(
            Task(
                id="parent",
                project_id="p",
                title="Parent",
                description="",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        args["parent_id"] = "parent"
    observed = []

    async def inspect_before_pipeline(event, task, **extras):
        if event != "task.created":
            return
        saved = await db.get_task(task.id)
        assert saved.is_blocked, "task was visible to the scheduler before its routing gate"
        assert len(await db.get_gates_for_task(task.id)) == 1
        assert not await db.assign_task_to_agent(task.id, "worker")
        observed.append(task.id)

    handler.orchestrator._emit_task_event = AsyncMock(side_effect=inspect_before_pipeline)
    handler.config.dev_strict = True
    result = await handler._cmd_create_task(args)
    assert "error" not in result
    assert observed == [result["created"]]


async def test_lost_creation_event_still_leaves_work_blocked(setup):
    handler, db, _, _ = setup
    handler.orchestrator._emit_task_event = AsyncMock(side_effect=RuntimeError("event lost"))
    result = await handler._cmd_create_task({"project_id": "p", "title": "Recover after crash"})
    task = await db.get_task(result["created"])
    assert task.is_blocked
    assert not await db.assign_task_to_agent(task.id, "worker")


async def test_profiled_work_and_bookkeeping_are_not_automatically_gated(setup):
    handler, db, _, _ = setup
    for extra in ({"profile_id": "coder"}, {"_suppress_created_event": True}):
        result = await handler._cmd_create_task({"project_id": "p", "title": "Routed", **extra})
        task = await db.get_task(result["created"])
        assert not task.is_blocked
        assert await db.get_gates_for_task(task.id) == []


async def test_graph_admission_gates_only_unrouted_nodes(setup):
    handler, db, _, _ = setup
    result = await handler._cmd_create_task_graph(
        {
            "project_id": "p",
            "graph": {
                "nodes": [
                    {"key": "unrouted", "title": "Needs routing", "acceptance": ["Done"]},
                    {
                        "key": "routed",
                        "title": "Use Sol",
                        "profile": "coder",
                        "acceptance": ["Done"],
                    },
                ]
            },
        }
    )
    assert "error" not in result
    nodes = {n["key"]: await db.get_task(n["task_id"]) for n in result["nodes"]}
    assert nodes["unrouted"].is_blocked
    assert not nodes["routed"].is_blocked
    assert await db.get_gates_for_task(result["parent_id"]) == []


async def test_precreated_gate_deduplicates_pipeline_call(setup):
    handler, db, _, _ = setup
    result = await handler._cmd_create_task({"project_id": "p", "title": "One gate"})
    before = await db.get_gates_for_task(result["created"])
    assert len(before) == 1
    gate = await handler._cmd_gate_create(
        {
            "project_id": "p",
            "gate_type": "routing",
            "title": "Route task",
            "waiter_task_ids": [result["created"]],
        }
    )
    assert gate["gate_id"] == before[0]["id"]
    assert gate["was_created"] is False


async def test_route_winning_before_gate_write_cannot_leave_stale_gate(setup, monkeypatch):
    handler, db, _, _ = setup
    task = Task(id="race", project_id="p", title="Race", description="", status=TaskStatus.READY)
    await db.create_task(task)
    original = db.create_gate

    async def routed_after_preread(*args, **kwargs):
        assert await db.update_task_routing(
            task.id, profile_id="coder", intelligence_class="deep-high", preferred_workspace_id=None
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "create_gate", routed_after_preread)
    result = await handler._cmd_gate_create(
        {
            "project_id": "p",
            "gate_type": "routing",
            "title": "Route task",
            "waiter_task_ids": [task.id],
        }
    )
    assert result.get("skipped") is True
    assert await db.get_gates_for_task(task.id) == []


async def test_atomic_admission_rolls_back_task_if_gate_write_fails(setup, monkeypatch):
    handler, db, _, _ = setup

    async def fail(*args, **kwargs):
        raise RuntimeError("gate failure")

    monkeypatch.setattr(db, "_create_gate_on", fail)
    with pytest.raises(RuntimeError, match="gate failure"):
        await handler._cmd_create_task({"project_id": "p", "title": "No half admission"})
    assert await db.list_tasks(project_id="p") == []


async def test_policy_reads_v2_activation_not_v1_enablement(setup):
    from src.playbooks.routing import requires_routing_gate, uses_default_triage

    _, _, manager, pb = setup
    task = Task(id="new", project_id="p", title="New", description="")
    assert requires_routing_gate(manager, task)
    assert uses_default_triage(manager, "p")
    pb.enabled = False
    assert requires_routing_gate(manager, task)
    assert uses_default_triage(manager, "p")


async def test_policy_matches_event_filter_when_and_ignores_cooldown(setup):
    from src.playbooks.routing import requires_routing_gate

    _, _, manager, pb = setup
    task = Task(id="new", project_id="p", title="New", description="")
    artifact = _routing_artifact(
        artifact_id=pb.id,
        trigger_filter={"task_type": "feature"},
    )
    install_routing_activation_snapshot(
        manager,
        [
            {
                "playbook_id": artifact.id,
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": SHA,
                "enabled": True,
                "health": "ready",
            }
        ],
        artifact_store=RecordingStore({SHA: artifact}),
    )
    assert not requires_routing_gate(manager, task)
    assert requires_routing_gate(manager, task, {"task_type": "feature"})
    assert requires_routing_gate(manager, task, {"task_type": "feature"})
    manager.is_on_cooldown = MagicMock(return_value=True)
    assert requires_routing_gate(
        manager, task, {"task_type": "feature", "parent_task_id": "parent"}
    )


async def test_competing_pipeline_gate_callbacks_share_one_gate(setup):
    _, db, _, _ = setup
    await db.create_task(Task(id="concurrent", project_id="p", title="Concurrent", description=""))
    results = await asyncio.gather(
        *(
            db.create_gate(
                "p",
                "routing",
                "Route task",
                waiter_task_ids=["concurrent"],
                unrouted_only=True,
            )
            for _ in range(8)
        )
    )
    assert len({gate_id for gate_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len(await db.get_gates_for_task("concurrent")) == 1


async def test_concurrent_route_and_gate_never_strand_routed_task(setup):
    handler, db, _, _ = setup
    for index in range(4):
        tid = f"race-{index}"
        await db.create_task(Task(id=tid, project_id="p", title="Race", description=""))
        gate, route = await asyncio.gather(
            handler._cmd_gate_create(
                {
                    "project_id": "p",
                    "gate_type": "routing",
                    "title": "Route task",
                    "waiter_task_ids": [tid],
                }
            ),
            handler._cmd_task_route(
                {"task_id": tid, "profile_id": "coder", "intelligence_class": "deep-high"}
            ),
        )
        assert gate.get("success"), gate
        assert route.get("success"), route
        task = await db.get_task(tid)
        assert task.profile_id == "coder"
        assert not task.is_blocked


async def test_admitted_gates_emit_created_once_after_commit(setup):
    handler, db, _, _ = setup
    observed = []

    async def on_gate(payload):
        saved = await db.get_task(payload["waiter_task_ids"][0])
        assert saved.is_blocked
        assert await db.get_gate(payload["gate_id"])
        observed.append(payload["gate_id"])

    handler.orchestrator.bus.subscribe("gate.created", on_gate)
    result = await handler._cmd_create_task({"project_id": "p", "title": "Notify gate"})
    graph = await handler._cmd_create_task_graph(
        {
            "project_id": "p",
            "graph": {
                "nodes": [
                    {"key": "work", "title": "Graph routing", "acceptance": ["Done"]},
                ]
            },
        }
    )
    assert len(observed) == 2
    assert len(set(observed)) == 2
    for task_id in (result["created"], graph["nodes"][0]["task_id"]):
        await handler._cmd_gate_create(
            {
                "project_id": "p",
                "gate_type": "routing",
                "title": "Route task",
                "waiter_task_ids": [task_id],
            }
        )
    assert len(observed) == 2


async def test_worker_filed_child_is_gated_before_created_event(setup):
    handler, db, _, _ = setup
    handler.config.swarm.enabled = True
    await db.create_task(
        Task(id="held", project_id="p", title="Held", description="", status=TaskStatus.READY)
    )
    assert await db.assign_task_to_agent("held", "worker")
    await db.create_session(
        SessionRecord(
            id="filing",
            project_id="p",
            profile_id="coder",
            harness="codex",
            provider="fake",
            name="filing",
            lifecycle="pool",
            work_dir="/wd",
            epoch="epoch",
            instance_token="instance",
            started_at=1,
            state="running",
            agent_id="worker",
            task_id="held",
            claim_phase="active",
        )
    )
    handler._current_scope = {
        "kind": "session",
        "session_id": "filing",
        "task_id": None,
        "project_id": "p",
        "elevated": False,
    }
    result = await handler._cmd_create_task(
        {
            "project_id": "p",
            "title": "Discovered subtask",
            "parent_id": "held",
            "reason": "The held task discovered additional required work.",
        }
    )
    assert result.get("success"), result
    assert (await db.get_task(result["created"])).is_blocked
    assert len(await db.get_gates_for_task(result["created"])) == 1


@pytest.mark.parametrize("shape", ["string", "dict", "string_list"])
async def test_v2_policy_ignores_legacy_pipeline_rule_shapes(setup, shape):
    from src.playbooks.routing import requires_routing_gate, uses_default_triage

    _, _, manager, pb = setup
    entry = pb.pipeline_rules["task.created"][0]["entry"]
    pb.pipeline_rules["task.created"] = {
        "string": entry,
        "dict": {"entry": entry},
        "string_list": [entry],
    }[shape]
    task = Task(id="new", project_id="p", title="New", description="")
    assert requires_routing_gate(manager, task)
    assert uses_default_triage(manager, "p")


@pytest.mark.parametrize(
    "field,parented", [("event.task_id", False), ("event.task.parent_task_id", True)]
)
async def test_admission_policy_sees_final_task_identity_and_parent(setup, field, parented):
    handler, db, _, pb = setup
    path = field.removeprefix("event.")
    artifact = _routing_artifact(
        artifact_id=pb.id,
        guard={
            "type": "exists",
            "value": {"type": "event_ref", "path": path},
            "mode": "truthy",
        },
    )
    install_routing_activation_snapshot(
        handler.orchestrator.playbook_manager,
        [
            {
                "playbook_id": artifact.id,
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": SHA,
                "enabled": True,
                "health": "ready",
            }
        ],
        artifact_store=RecordingStore({SHA: artifact}),
    )
    args = {"project_id": "p", "title": "Guarded routing"}
    if parented:
        await db.create_task(
            Task(
                id="parent",
                project_id="p",
                title="Parent",
                description="",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        args["parent_id"] = "parent"
    result = await handler._cmd_create_task(args)
    assert result.get("success"), result
    task = await db.get_task(result["created"])
    assert task.is_blocked
    assert len(await db.get_gates_for_task(task.id)) == 1
