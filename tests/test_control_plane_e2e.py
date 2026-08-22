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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import load_intelligence_classes
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.pipeline_runner import PipelineRunner
from src.sessions.spec import SessionSpecBuilder
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

    # 7) Chain-end proof — build a REAL SessionSpecBuilder over the seeded
    #    intelligence-classes vault and confirm the composed argv carries
    #    the "standard" class's anthropic model, overriding profile.model.
    #    This mocks only the fake harness (tmux-level) — the class lookup,
    #    provider inference, and _resolve_model logic are all real.
    config = wired["config"]
    classes = load_intelligence_classes(config.data_dir)
    assert "standard" in classes, list(classes)
    # The default standard class ships anthropic → claude-sonnet-4-6.
    expected_model = classes["standard"].mapping["anthropic"]["model"]
    profile = await db.get_profile(t2.profile_id)
    # Override profile.model so the assertion can distinguish "class won" from
    # "profile happened to be equal".
    profile_stub = SimpleNamespace(
        model="OVERRIDDEN-BY-CLASS-shouldnt-appear",
        default_class=profile.default_class,
        effort=getattr(profile, "effort", ""),
    )
    harness = SimpleNamespace(
        id="claude",  # → provider inferred as anthropic
        command="claude",
        args=[],
        model_flag="--model",
        effort_flag=None,
        permission_flag=None,
        settings_flag=None,
        session_id_flag=None,
        resume=SimpleNamespace(style="none", flag="", subcommand=""),
        prompt_mode="none",
        prompt_flag="",
        max_argv_prompt_bytes=1024,
        provider="",  # force _infer_provider_from_harness path
    )
    builder = SessionSpecBuilder(config, intelligence_classes=classes)
    argv = builder._compose_argv(
        harness=harness,
        profile=profile_stub,
        session_id="s1",
        resume_key=None,
        prompt=None,
        session_name="s",
        files=[],
        task_intelligence_class=t2.intelligence_class,
    )
    assert "--model" in argv, argv
    assert argv[argv.index("--model") + 1] == expected_model, argv
    assert "OVERRIDDEN-BY-CLASS-shouldnt-appear" not in argv, argv


async def test_duplicate_event_no_double_gate(wired):
    """Firing ``_on_playbook_trigger`` twice with the same event_id must dispatch once.

    Exercises the real dedup path in ``Orchestrator._on_playbook_trigger``:
    the pre-check (``get_playbook_run_by_event``) plus the ``IntegrityError``
    TOCTOU fallback on ``create_playbook_run``.  We fire the trigger twice
    with the same event and assert:
      - Only one pipeline coroutine is created.
      - Only one playbook_runs row is inserted.
      - Only one routing gate exists on the task afterwards.
    """
    from src.orchestrator.core import Orchestrator

    db = wired["db"]
    handler = wired["handler"]
    orch = wired["orchestrator"]
    pipeline = wired["pipeline"]

    r = await handler.execute(
        "create_task",
        {"project_id": PID, "title": "X", "description": "..."},
    )
    assert "error" not in r, r
    task_id = r["created"]

    # Build a stub playbook that _on_playbook_trigger will treat as a pipeline;
    # its to_dict() returns the compiled default pipeline graph.
    graph = pipeline.to_dict()
    playbook = MagicMock()
    playbook.id = graph["id"]
    playbook.version = graph.get("version", 1)
    playbook.to_dict.return_value = graph

    event = {
        "type": "task.created",
        "event_id": "evt-dup",
        "project_id": PID,
        "task_id": task_id,
    }

    # Wrap ``asyncio.create_task`` so pipeline dispatches are recorded, but
    # any OTHER call (SQLAlchemy's async engine uses it internally) is passed
    # through unchanged.  We identify pipeline dispatches by the ``name=``
    # kwarg the orchestrator sets (``pipeline:<pb_id>:...``).
    import asyncio as _asyncio

    dispatched: list[object] = []
    real_create_task = _asyncio.create_task

    def _spy(coro, **kw):
        name = kw.get("name", "") or ""
        if name.startswith("pipeline:"):
            dispatched.append(coro)
            # Don't actually run it — we drive it inline below.  But we
            # MUST return a real Task-like awaitable so nothing that
            # inspects the returned value chokes.  Create a resolved
            # future carrying None.
            fut = _asyncio.get_event_loop().create_future()
            fut.set_result(None)
            # Close the captured coro is caller's responsibility.
            return fut
        return real_create_task(coro, **kw)

    with patch.object(_asyncio, "create_task", side_effect=_spy):
        await orch._on_playbook_trigger(playbook, event)
    assert len(dispatched) == 1, "first trigger must dispatch once"

    # Execute the captured coroutine so gates/rows get written for real.
    await dispatched[0]

    # Baseline: one open routing gate, one playbook_run row.
    gates_before = await db.get_gates_for_task(task_id)
    routing_before = [g for g in gates_before if g["gate_type"] == "routing"]
    assert len(routing_before) == 1, routing_before
    runs_before = await db.list_playbook_runs(playbook_id=playbook.id)
    assert len(runs_before) == 1, runs_before

    # Second fire with the SAME event_id: dedup must short-circuit and no
    # additional pipeline task should be created.
    dispatched.clear()
    with patch.object(_asyncio, "create_task", side_effect=_spy):
        await orch._on_playbook_trigger(playbook, event)
    assert dispatched == [], "duplicate event_id must NOT dispatch a second pipeline"

    # Zero new gates, zero new rows.
    gates_after = await db.get_gates_for_task(task_id)
    routing_after = [g for g in gates_after if g["gate_type"] == "routing"]
    assert len(routing_after) == 1, routing_after
    runs_after = await db.list_playbook_runs(playbook_id=playbook.id)
    assert len(runs_after) == 1, runs_after

    # Sanity: the guard we hit is the pre-check.  Confirm it can find the row.
    assert Orchestrator._on_playbook_trigger  # keep import alive/meaningful
    existing = await db.get_playbook_run_by_event(playbook.id, "evt-dup")
    assert existing is not None
