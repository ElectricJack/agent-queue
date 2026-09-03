"""Admitted graph tasks must reach the project's selected routing pipeline."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.manager import PlaybookManager
from src.playbooks.definition import load_definition_json
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.routing import install_routing_activation_snapshot, uses_default_triage
from src.vault import ensure_default_intelligence_classes
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.test_routing_admission_v2 import FIXTURE, RecordingStore, SHA, _routing_artifact

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

PROJECT_ROUTING_PIPELINE = """---
id: project-routing
kind: pipeline
role: default-pipeline
scope: project
triggers: [task.created]
---
```json
{"rules":[{"id":"route-created","on":"task.created","when":{"field":"event.task.profile_id","is_null":true},"entry":"gate","nodes":{"gate":{"command":"gate_create","args":{"project_id":"{{event.project_id}}","gate_type":"routing","title":"Route task","waiter_task_ids":["{{event.task_id}}"]},"on_success":"route","on_failure":"done"},"route":{"command":"task_route","args":{"task_id":"{{event.task_id}}","profile_id":"coder","intelligence_class":"deep-high"},"on_success":"done","on_failure":"done"},"done":{"terminal":true}}}]}
```
"""


@pytest.fixture(params=["sqlite", "postgres"])
async def wired(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "graph-routing.db"))
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
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.playbooks.enabled = True
    config.dev_strict = True
    ensure_default_intelligence_classes(config.data_dir)
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)
    orch.set_command_handler(handler)

    observed = []

    async def observe_admission(event):
        task = await db.get_task(event["task_id"])
        gates = await db.get_gates_for_task(task.id)
        observed.append((task, gates, dict(event)))

    # Observe committed rows before the real manager dispatches asynchronously.
    orch.bus.subscribe("task.created", observe_admission)
    manager = PlaybookManager(
        config=config,
        event_bus=orch.bus,
        on_trigger=orch._on_playbook_trigger,
    )
    default = compile_pipeline(
        # Frozen pre-Package-6 V1 graph; the shipped source is prose now.
        Path("tests/fixtures/playbooks/v1/default-pipeline.md").read_text()
    ).playbook
    custom = compile_pipeline(PROJECT_ROUTING_PIPELINE).playbook
    for playbook in (default, custom):
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)
    manager.set_scope_identifier(custom.id, "p")
    artifact = _routing_artifact(
        scope={"type": "project", "project_id": "p"},
        default_triage=False,
    )
    install_routing_activation_snapshot(
        manager,
        [
            {
                "playbook_id": artifact.id,
                "scope": "project",
                "scope_identifier": "p",
                "active_artifact_sha256": SHA,
                "enabled": True,
                "health": "ready",
            }
        ],
        artifact_store=RecordingStore({SHA: artifact}),
    )
    manager.subscribe_to_events()
    orch.playbook_manager = manager
    assert not uses_default_triage(manager, "p")

    spawned = []
    create_task = asyncio.create_task

    def track_pipeline(coro, **kwargs):
        task = create_task(coro, **kwargs)
        if (kwargs.get("name") or "").startswith("pipeline:"):
            spawned.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", track_pipeline)
    yield SimpleNamespace(
        db=db,
        handler=handler,
        manager=manager,
        custom=custom,
        observed=observed,
        spawned=spawned,
    )
    if spawned:
        await asyncio.wait_for(asyncio.gather(*spawned), timeout=10)
    manager.unsubscribe_from_events()
    await db.close()


@pytest.mark.parametrize("existing_parent", [False, True])
async def test_custom_routing_receives_admitted_graph_node_after_commit(wired, existing_parent):
    args = {
        "project_id": "p",
        "graph": {
            "nodes": [
                {"key": "unrouted", "title": "Needs custom routing", "acceptance": ["Done"]},
                {
                    "key": "routed",
                    "title": "Already routed",
                    "profile": "coder",
                    "acceptance": ["Done"],
                },
            ]
        },
    }
    if existing_parent:
        await wired.db.create_task(
            Task(
                id="container",
                project_id="p",
                title="Container",
                description="",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        args["parent_id"] = "container"
    result = await wired.handler._cmd_create_task_graph(args)
    assert "error" not in result, result
    ids = {node["key"]: node["task_id"] for node in result["nodes"]}

    # Routing runs in the real detached pipeline task. Await it, not a sleep
    # or the create response: returning from the API does not mean it finished.
    if wired.spawned:
        await asyncio.wait_for(asyncio.gather(*wired.spawned), timeout=10)

    task = await wired.db.get_task(ids["unrouted"])
    assert task.profile_id == "coder", "the selected custom routing pipeline never ran"
    assert task.intelligence_class == "deep-high"
    routing_gates = await wired.db.get_gates_for_task(task.id)
    assert len(routing_gates) == 1
    assert routing_gates[0]["status"] == "resolved"
    assert await wired.db.find_task_by_dedup_key("p", "triage-open") is None

    assert len(wired.observed) == 1, "only admitted graph nodes emit task.created"
    admitted, before_routing, event = wired.observed[0]
    assert admitted.id == ids["unrouted"]
    assert admitted.profile_id is None
    assert admitted.is_blocked
    assert before_routing[0]["status"] == "open"
    assert event["parent_task_id"] == result["parent_id"]
    assert await wired.db.get_gates_for_task(ids["routed"]) == []
    assert await wired.db.get_gates_for_task(result["parent_id"]) == []
    runs = await wired.db.list_playbook_runs(playbook_id=wired.custom.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert await wired.db.list_playbook_runs(playbook_id="default-pipeline") == []


async def test_graph_without_routing_policy_preserves_existing_event_behavior(wired):
    wired.custom.nodes = {}
    wired.custom.pipeline_rules = {}
    artifact = load_definition_json(FIXTURE.read_text())
    install_routing_activation_snapshot(
        wired.manager,
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
    result = await wired.handler._cmd_create_task_graph(
        {
            "project_id": "p",
            "graph": {
                "nodes": [
                    {"key": "work", "title": "No custom routing", "acceptance": ["Done"]},
                ]
            },
        }
    )
    assert "error" not in result, result
    assert wired.observed == []
    assert wired.spawned == []
    assert await wired.db.get_gates_for_task(result["nodes"][0]["task_id"]) == []
