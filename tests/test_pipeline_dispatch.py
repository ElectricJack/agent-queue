"""Real orchestrator pipeline dispatch — coverage follow-up FU-2 / X7.

``tests/conftest.py``'s ``PipelineEngine`` re-implements rule selection and
``event.task`` hydration, so ``src/orchestrator/core.py``'s dispatch of
``_eval_pipeline_when`` was previously exercised only by that test-local
mirror.  These tests drive the production path end to end instead:

    EventBus.emit → PlaybookManager trigger subscription
        → Orchestrator._on_playbook_trigger (event.task hydration, rule
          selection by trigger, real when-guard evaluation, entry-node
          pinning, run-row idempotency)
            → PipelineRunner → CommandHandler

against the real compiled default pipeline on a real database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.manager import PlaybookManager
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.store import CompiledPlaybookStore

DEFAULT_PIPELINE_PATH = (
    Path(__file__).parent.parent / "src" / "prompts" / "default_playbooks" / "default-pipeline.md"
)

PLAYBOOK_ID = "default-pipeline"

LEGACY_CACHED_DEFAULT = """---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers: [task.created]
---
```json
{"rules":[{"id":"task-created-routing","on":"task.created","entry":"gate","nodes":{"gate":{"command":"gate_create","args":{"project_id":"{{event.project_id}}","gate_type":"routing","title":"Route task","waiter_task_ids":["{{event.task_id}}"]},"on_success":"done","on_failure":"done"},"done":{"terminal":true}}}]}
```
"""


@pytest.fixture
def dispatch_env(tmp_path):
    """A real Orchestrator + CommandHandler + EventBus + PlaybookManager with
    the compiled default pipeline installed and its triggers subscribed —
    the exact production dispatch wiring, no test-local mirror."""

    async def _make():
        db = Database(str(tmp_path / "dispatch.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "dispatch.db"),
            data_dir=str(tmp_path / "d"),
        )
        orch = Orchestrator(cfg)
        orch.db = db
        orch.git = MagicMock()
        orch.bus = EventBus()
        handler = CommandHandler(orch, cfg)
        orch.command_handler = handler

        compiled = compile_pipeline(DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8"))
        assert compiled.success, f"default-pipeline.md did not compile: {compiled.errors}"

        store = CompiledPlaybookStore(SimpleNamespace(compiled_root=str(tmp_path / "compiled")))
        manager = PlaybookManager(
            config=cfg,
            event_bus=orch.bus,
            data_dir=cfg.data_dir,
            store=store,
            command_handler=handler,
        )
        await manager.install_compiled(compiled.playbook)
        orch.playbook_manager = manager
        orch.set_command_handler(handler)
        manager.on_trigger = orch._on_playbook_trigger
        assert manager.subscribe_to_events() > 0
        return orch, handler

    return _make


async def _seed(handler: CommandHandler) -> None:
    await handler.db.create_project(Project(id="p", name="P"))
    await handler.db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await handler.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await handler.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))


async def _mktask(db, task_id: str, **fields) -> None:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            title=task_id,
            description="",
            status=TaskStatus.COMPLETED,
            profile_id="worker",
            **fields,
        )
    )


async def _emit_completed_and_wait(orch, task_id: str, event_id: str):
    """Emit ``task.completed`` on the real bus, then wait for the pipeline run
    row the real dispatch created for this event to leave ``running``."""
    await orch.bus.emit(
        "task.completed",
        {"task_id": task_id, "project_id": "p", "title": task_id, "event_id": event_id},
    )
    for _ in range(200):
        run = await orch.db.get_playbook_run_by_event(PLAYBOOK_ID, event_id)
        if run is not None and run.status != "running":
            return run
        await asyncio.sleep(0.05)
    pytest.fail(f"pipeline run for event {event_id} did not finish")


async def _emit_completed_slim(orch, task_id: str, event_id: str) -> None:
    """Emit the slim ``task.completed`` an older daemon or a hand-written
    event sends: no ``no_code``, no ``review_task``."""
    await orch.bus.emit(
        "task.completed",
        {"task_id": task_id, "project_id": "p", "title": task_id, "event_id": event_id},
    )


async def _tasks_by_profile(db, profile_id: str) -> list[Task]:
    return [t for t in await db.list_tasks(project_id="p") if t.profile_id == profile_id]


@pytest.mark.asyncio
async def test_cached_system_default_does_not_dispatch_legacy_assignment_rule(dispatch_env):
    orch, _handler = await dispatch_env()
    stale = compile_pipeline(LEGACY_CACHED_DEFAULT).playbook
    assert stale is not None

    await orch._on_playbook_trigger(
        stale,
        {"type": "task.created", "event_id": "legacy-created", "project_id": "p"},
    )

    assert await orch.db.get_playbook_run_by_event(stale.id, "legacy-created") is None


@pytest.mark.asyncio
async def test_real_dispatch_selects_rules_via_when_guards(dispatch_env):
    """Branch-only completion fires the per-task-review rule but the real
    ``_eval_pipeline_when`` rejects per-branch-final-review's ``all`` guard
    (no ``pr_url``); a branch+PR completion then fires both rules."""
    orch, handler = await dispatch_env()
    try:
        await _seed(handler)

        # Task with a branch but no PR URL.
        await _mktask(orch.db, "t-branch-only", branch_name="feat/one")
        run = await _emit_completed_and_wait(orch, "t-branch-only", "evt-branch-only")
        assert run.status == "completed", run.error
        reviews = await _tasks_by_profile(orch.db, "reviewer")
        assert len(reviews) == 1
        # The rule graph ran against the hydrated event: discovered-from edge
        # points at the completed task (event.task hydration + entry pinning).
        deps = await orch.db.get_typed_dependencies(reviews[0].id)
        assert ("t-branch-only", "discovered-from") in set(deps)
        # The final-review rule's when-guard rejected — real guard evaluation.
        assert await _tasks_by_profile(orch.db, "final-reviewer") == []

        # Task with branch + PR: both task.completed rules dispatch.
        await _mktask(
            orch.db,
            "t-branch-pr",
            branch_name="feat/two",
            pr_url="https://github.com/o/r/pull/7",
        )
        run2 = await _emit_completed_and_wait(orch, "t-branch-pr", "evt-branch-pr")
        assert run2.status == "completed", run2.error
        assert len(await _tasks_by_profile(orch.db, "reviewer")) == 2
        finals = await _tasks_by_profile(orch.db, "final-reviewer")
        assert len(finals) == 1
    finally:
        await orch.db.close()


@pytest.mark.asyncio
async def test_real_dispatch_is_idempotent_per_event_id(dispatch_env):
    """Re-emitting the same event_id must not create a second run row — the
    dispatch's event-level dedup, previously mirrored in the test helper."""
    orch, handler = await dispatch_env()
    try:
        await _seed(handler)
        await _mktask(orch.db, "t-once", branch_name="feat/once")
        run = await _emit_completed_and_wait(orch, "t-once", "evt-once")
        assert run.status == "completed", run.error
        runs_before = await orch.db.list_playbook_runs(playbook_id=PLAYBOOK_ID)

        await orch.bus.emit(
            "task.completed",
            {"task_id": "t-once", "project_id": "p", "title": "t-once", "event_id": "evt-once"},
        )
        await asyncio.sleep(0.2)  # give a (wrongly) spawned run time to appear

        runs_after = await orch.db.list_playbook_runs(playbook_id=PLAYBOOK_ID)
        assert len(runs_after) == len(runs_before)
        assert len(await _tasks_by_profile(orch.db, "reviewer")) == 1
    finally:
        await orch.db.close()


@pytest.mark.asyncio
async def test_real_dispatch_never_reviews_a_pipeline_review_task(dispatch_env):
    """A finished review must not spawn a review of itself, whatever the emitter sent.

    The close path sets ``review_task`` on ``task.completed``, but an emitter
    that predates that flag (a daemon still running older code, a hand-written
    event, container settlement) omits it, and the rules' ``truthy: false``
    guard passes on a missing key — that is how ``Review: Review: Review: ...``
    chains six deep reached the live queue (task prime-cascade-64).  The
    dispatch path therefore derives the flag from the task row's own dedup
    key: a ``review:task:`` / ``branch-review:`` row *is* a review.
    """
    orch, handler = await dispatch_env()
    try:
        await _seed(handler)

        # A per-task review the pipeline created earlier, finishing on its own
        # slot branch — exactly the row that used to be reviewed again.
        await orch.db.create_task(
            Task(
                id="rv-1",
                project_id="p",
                title="Review: t-orig",
                description="",
                status=TaskStatus.COMPLETED,
                profile_id="reviewer",
                dedup_key="review:task:t-orig",
                branch_name="aq/rv-1",
                pr_url="https://github.com/o/r/pull/9",
            )
        )
        # Slim payload: neither ``review_task`` nor ``no_code`` present.  The
        # bus awaits the trigger, and dispatch writes the run row before the
        # graph walk starts, so once ``emit`` returns a run row exists exactly
        # when some rule fired.
        await _emit_completed_slim(orch, "rv-1", "evt-rv-1")
        assert await orch.db.get_playbook_run_by_event(PLAYBOOK_ID, "evt-rv-1") is None, (
            "a task.completed rule fired for a pipeline-created review task"
        )
        reviews = await _tasks_by_profile(orch.db, "reviewer")
        assert [t.id for t in reviews] == ["rv-1"], (
            f"a review of the review was spawned: {[(t.id, t.title) for t in reviews]}"
        )
        assert await _tasks_by_profile(orch.db, "final-reviewer") == []

        # Same for the branch-level final review row.
        await orch.db.create_task(
            Task(
                id="fr-1",
                project_id="p",
                title="Final review: feat/x",
                description="",
                status=TaskStatus.COMPLETED,
                profile_id="final-reviewer",
                dedup_key="branch-review:feat/x",
                branch_name="aq/fr-1",
                pr_url="https://github.com/o/r/pull/10",
            )
        )
        await _emit_completed_slim(orch, "fr-1", "evt-fr-1")
        assert await orch.db.get_playbook_run_by_event(PLAYBOOK_ID, "evt-fr-1") is None
        assert [t.id for t in await _tasks_by_profile(orch.db, "reviewer")] == ["rv-1"]
        assert [t.id for t in await _tasks_by_profile(orch.db, "final-reviewer")] == ["fr-1"]
    finally:
        await orch.db.close()
