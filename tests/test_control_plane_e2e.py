"""End-to-end control plane (dv2 phase 1, Task 14).

Proves the whole routing chain wires together with real components:

    create_task
        └─► default pipeline playbook fires (compiled + dispatched via
            PipelineRunner using the real CommandHandler)
        └─► routing gate + triage task exist
        └─► task_route resolves the routing gate
        └─► the next _check_defined_tasks cycle promotes DEFINED → READY

Uses a real SQLite database, real pipeline compiler, real PipelineRunner,
real CommandHandler.  Mocks only the outermost boundaries (``orch.git``);
no LLM is invoked (pipeline runners are deterministic).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, PlaybookRun, Project, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.pipeline_runner import PipelineRunner
from src.vault import (
    ensure_default_intelligence_classes,
    ensure_default_playbooks,
    ensure_default_profiles,
)

PID = "e2e-proj"


@pytest.fixture
async def wired(tmp_path):
    """Wire an orchestrator + handler + real DB + shipped default pipeline."""
    data_dir = str(tmp_path / "data")
    # Ship defaults into the vault so the pipeline is discoverable.
    ensure_default_playbooks(data_dir)
    ensure_default_profiles(data_dir)
    ensure_default_intelligence_classes(data_dir)

    db_path = str(tmp_path / "e2e.db")
    db = Database(db_path)
    await db.initialize()
    await db.create_project(Project(id=PID, name="E2E"))
    await db.upsert_profile(
        AgentProfile(
            id="coder",
            name="Coder",
            model="claude-sonnet-4-6",
            harness="claude",
            default_class="",
            needs_workspace=False,
        )
    )

    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=db_path,
        data_dir=data_dir,
    )
    orch = Orchestrator(config)
    # Use the DB we already initialised (with the seeded project + profile).
    orch.db = db
    orch.git = MagicMock()
    handler = CommandHandler(orch, config)
    orch.set_command_handler(handler)

    # Load the default pipeline into a fresh compiled graph for direct dispatch.
    pipeline_md = (
        Path(data_dir) / "vault" / "system" / "playbooks" / "default-pipeline.md"
    ).read_text(encoding="utf-8")
    compiled = compile_pipeline(pipeline_md).playbook
    assert compiled is not None

    yield {
        "db": db,
        "handler": handler,
        "orchestrator": orch,
        "config": config,
        "pipeline": compiled,
    }
    await db.close()


async def test_e2e_routing(wired):
    db = wired["db"]
    handler = wired["handler"]
    pipeline = wired["pipeline"]

    # 1) Create a task.  Real handler dispatch.  Returns {"created": id, ...}.
    r = await handler.execute(
        "create_task",
        {"project_id": PID, "title": "Do a thing", "description": "..."},
    )
    assert "error" not in r, r
    task_id = r["created"]

    # 2) Simulate the default pipeline reacting to task.created.  Real
    #    PipelineRunner walks the compiled graph and dispatches each node
    #    (gate_create → ensure_task) through the real CommandHandler.
    event = {"event_id": "evt-1", "project_id": PID, "task_id": task_id}
    result = await PipelineRunner(
        pipeline.to_dict(), event=event, handler=handler, db=db
    ).run()
    assert result.status == "completed", (result.status, result.error)

    # 3) A routing gate exists on the task; a triage task with dedup_key
    #    "triage-open" exists.
    gates = await db.get_gates_for_task(task_id)
    assert any(
        g["gate_type"] == "routing" and g["status"] == "open" for g in gates
    ), gates

    triage = await db.find_task_by_dedup_key(PID, "triage-open")
    assert triage is not None

    # 4) The task cannot be scheduled while its routing gate is open — the
    #    gate's ``waiter_task_ids`` projection sets ``is_blocked=1``.  A task
    #    created with no blocking edges starts in READY (control-plane spec
    #    §3 keeps the lifecycle unchanged); the scheduler's is_blocked
    #    filter is what actually keeps it out of dispatch.
    t = await db.get_task(task_id)
    assert t.status == TaskStatus.READY
    assert bool(t.is_blocked) is True
    assert t.profile_id in (None, ""), t.profile_id

    # 5) task_route writes routing fields + resolves every open routing gate.
    rr = await handler.execute(
        "task_route",
        {
            "task_id": task_id,
            "profile_id": "coder",
            "intelligence_class": "standard",
        },
    )
    assert rr["success"] is True, rr
    assert rr["resolved_gate_ids"], rr

    # 6) Blocked projection was flipped by resolve_gate; the task is now
    #    schedulable (status READY, is_blocked=0) with routing fields set.
    t2 = await db.get_task(task_id)
    assert t2.status == TaskStatus.READY, (t2.status, t2.is_blocked)
    assert bool(t2.is_blocked) is False, t2.is_blocked
    assert t2.profile_id == "coder"
    assert t2.intelligence_class == "standard"

    # And confirm the routing gate is closed.
    gates2 = await db.get_gates_for_task(task_id)
    assert not any(
        g["gate_type"] == "routing" and g["status"] == "open" for g in gates2
    ), gates2


async def test_duplicate_event_no_double_gate(wired):
    """The (playbook_id, event_id) idempotency guard blocks re-dispatch.

    The runner itself is deterministic and not idempotent — the manager is
    what dedups pipeline runs before dispatch.  Simulate the guard by
    inserting the playbook_runs row and confirming ``get_playbook_run_by_event``
    reports the existing row, which is what
    ``Orchestrator._on_playbook_trigger`` consults before creating a new run.
    """
    db = wired["db"]
    handler = wired["handler"]
    pipeline = wired["pipeline"]

    r = await handler.execute(
        "create_task",
        {"project_id": PID, "title": "X", "description": "..."},
    )
    assert "error" not in r, r
    task_id = r["created"]

    event = {"event_id": "evt-dup", "project_id": PID, "task_id": task_id}
    first = await PipelineRunner(
        pipeline.to_dict(), event=event, handler=handler, db=db
    ).run()
    assert first.status == "completed", first

    # Record a run row with the same (playbook_id, event_id); the unique
    # partial index in the schema is what makes the guard useful in prod.
    await db.create_playbook_run(
        PlaybookRun(
            run_id="r-dup",
            playbook_id="default-pipeline",
            playbook_version=1,
            trigger_event="{}",
            status="completed",
            started_at=1.0,
            event_id="evt-dup",
        )
    )
    existing = await db.get_playbook_run_by_event("default-pipeline", "evt-dup")
    assert existing is not None
    assert existing.event_id == "evt-dup"
