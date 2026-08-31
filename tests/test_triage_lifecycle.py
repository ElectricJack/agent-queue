"""Triage reuses one history-preserving task without losing routing wakeups."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.api.models.task import EnsureTaskResponse
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    Project,
    SessionRecord,
    Task,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.manager import PlaybookManager
from src.playbooks.pipeline_runner import PipelineRunner
from tests.pg_dsn import ensure_worker_postgres_dsn


POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def setup(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "triage.db"))
        await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_project(Project(id="other", name="Other"))
    await db.upsert_profile(AgentProfile(id="triage", name="Triage", needs_workspace=False))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=False))
    await db.create_agent(
        Agent(id="agent-triage", name="Triage worker", harness="codex", profile_id="triage")
    )
    cfg = AppConfig(data_dir=str(tmp_path / "data"), database_path=str(tmp_path / "triage.db"))
    cfg.playbooks.enabled = True
    orch = Orchestrator(cfg)
    orch.db = db
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, cfg)
    orch.set_command_handler(handler)
    yield handler, db, orch
    await db.close()


async def add_routing_work(db, task_id, *, project_id="p", status=TaskStatus.READY):
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title=task_id,
            description="Needs routing",
            status=status,
        )
    )
    gate_id, _ = await db.create_gate(
        project_id, "routing", "Route task", waiter_task_ids=[task_id]
    )
    return gate_id


async def ensure_triage(handler, project_id="p"):
    return await handler.execute(
        "ensure_task",
        {
            "project_id": project_id,
            "dedup_key": "triage-open",
            "profile_id": "triage",
            "title": "Triage unrouted tasks",
            "priority": 1,
            "description": "Route pending tasks, then close.",
        },
    )


async def triage_rows(db, project_id="p"):
    return [
        t
        for t in await db.list_tasks(project_id=project_id)
        if t.dedup_key == "triage-open" and t.profile_id == "triage"
    ]


async def test_no_open_routing_work_does_not_create_empty_triage(setup):
    handler, db, _ = setup
    result = await ensure_triage(handler)
    assert result["success"] is True
    assert result["created"] is False
    assert result["task_id"] is None
    assert EnsureTaskResponse(**result).task_id is None
    assert await triage_rows(db) == []


async def test_old_pipeline_skipped_gate_does_not_create_empty_triage(setup):
    handler, db, _ = setup
    await db.create_task(
        Task(
            id="routed",
            project_id="p",
            title="Already routed",
            description="",
            profile_id="worker",
            status=TaskStatus.READY,
        )
    )
    # An already-installed pipeline may still follow success after gate_create skips.
    graph = {
        "nodes": {
            "gate": {
                "entry": True,
                "command": "gate_create",
                "args": {
                    "project_id": "p",
                    "gate_type": "routing",
                    "title": "Route",
                    "waiter_task_ids": ["routed"],
                },
                "on_success": "triage",
            },
            "triage": {
                "command": "ensure_task",
                "args": {
                    "project_id": "p",
                    "dedup_key": "triage-open",
                    "profile_id": "triage",
                    "title": "Triage unrouted tasks",
                },
                "on_success": "done",
            },
            "done": {"terminal": True},
        }
    }
    result = await PipelineRunner(graph, {}, handler).run()
    assert result.status == "completed"
    assert await db.get_gates_for_task("routed") == []
    assert await triage_rows(db) == []


async def test_concurrent_routing_events_create_only_one_triage_task(setup):
    handler, db, _ = setup
    await add_routing_work(db, "one")
    await add_routing_work(db, "two")
    results = await asyncio.gather(*(ensure_triage(handler) for _ in range(8)))
    assert all(r.get("success") is True for r in results), results
    assert len({r["task_id"] for r in results}) == 1
    assert sum(r["created"] for r in results) == 1
    rows = await triage_rows(db)
    assert len(rows) == 1
    assert rows[0].status == TaskStatus.READY
    assert await db.get_gates_for_task(rows[0].id) == []


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
async def test_new_routing_work_reuses_terminal_task_and_keeps_history(setup, status):
    handler, db, _ = setup
    first_gate = await add_routing_work(db, "one")
    first = await ensure_triage(handler)
    tid = first["task_id"]
    await db.add_task_context(
        tid, type="triage_report", label="Previous run", content="Routed one."
    )
    await db.save_task_result(
        tid,
        "agent-triage",
        AgentOutput(result=AgentResult.COMPLETED, summary="Original routing report"),
    )
    await db.set_task_meta(tid, "summary", "Original summary")
    await db.resolve_gate(first_gate, resolved_by="test", resolution="routed")
    await db.transition_task(tid, status, force=True, assigned_agent_id=None)
    await add_routing_work(db, "two")
    second = await ensure_triage(handler)
    assert second["task_id"] == tid
    assert second["created"] is False
    assert second["restarted"] is True
    assert (await db.get_task(tid)).status == TaskStatus.READY
    assert len(await triage_rows(db)) == 1
    assert (await db.get_task_results(tid))[0]["summary"] == "Original routing report"
    assert (await db.get_task_contexts(tid))[0]["content"] == "Routed one."
    assert await db.get_task_meta(tid, "summary") == "Original summary"


async def test_unchanged_or_removed_gates_do_not_repeatedly_restart_triage(setup):
    handler, db, orch = setup
    first_gate = await add_routing_work(db, "one")
    await add_routing_work(db, "unroutable")
    first = await ensure_triage(handler)
    tid = first["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    await db.resolve_gate(first_gate, resolved_by="test", resolution="routed")
    for _ in range(2):
        await ensure_triage(handler)
        await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    assert len(await triage_rows(db)) == 1
    await add_routing_work(db, "new-work")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.READY


async def test_gate_arriving_during_run_is_not_lost_after_close_and_drain(setup):
    handler, db, orch = setup
    gate = await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(
        tid, TaskStatus.IN_PROGRESS, force=True, assigned_agent_id="agent-triage"
    )
    await db.create_session(
        SessionRecord(
            id="triage-session",
            project_id="p",
            profile_id="triage",
            harness="codex",
            provider="fake",
            name="triage",
            lifecycle="task",
            work_dir="/tmp/triage-test",
            epoch="test",
            instance_token="test-token",
            started_at=time.time(),
            task_id=tid,
            state="running",
            agent_id="agent-triage",
        )
    )
    await db.resolve_gate(gate, resolved_by="test", resolution="routed")
    await add_routing_work(db, "arrived-during-run")
    assert (await ensure_triage(handler))["task_id"] == tid
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS
    # The prompt's real completion command emits task.updated, not task.closed.
    await handler.execute("edit_task", {"task_id": tid, "status": "COMPLETED"})
    await db.update_task(tid, assigned_agent_id=None)
    await db.update_session("triage-session", state="draining")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    await db.update_session("triage-session", state="stopped", desired_state="stopped")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.READY
    assert len(await triage_rows(db)) == 1
    assert (await db.get_session("triage-session")).state == "stopped"
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "status", [TaskStatus.BLOCKED, TaskStatus.PAUSED, TaskStatus.WAITING_INPUT]
)
async def test_manual_stopped_or_waiting_triage_is_not_resumed(setup, status):
    handler, db, orch = setup
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, status, force=True)
    await add_routing_work(db, "new-work")
    assert (await ensure_triage(handler))["task_id"] == tid
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == status
    assert len(await triage_rows(db)) == 1


async def test_hold_is_not_cleared_or_consumed_by_reconciliation(setup):
    handler, db, orch = setup
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    await db.add_task_label(tid, "hold:operator")
    await add_routing_work(db, "new-work")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    assert await db.get_task_labels(tid) == ["hold:operator"]
    await db.remove_task_label(tid, "hold:operator")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.READY


async def test_terminal_or_other_project_waiters_do_not_wake_triage(setup):
    handler, db, _ = setup
    await add_routing_work(db, "finished", status=TaskStatus.COMPLETED)
    await add_routing_work(db, "elsewhere", project_id="other")
    result = await ensure_triage(handler)
    assert result["created"] is False
    assert await triage_rows(db) == []
    other = await ensure_triage(handler, "other")
    assert other["created"] is True
    assert len(await triage_rows(db, "other")) == 1


async def test_existing_terminal_triage_is_adopted_without_creating_another_row(setup):
    handler, db, _ = setup
    await db.create_task(
        Task(
            id="legacy-triage",
            project_id="p",
            title="Triage unrouted tasks",
            description="Keep this report history",
            profile_id="triage",
            dedup_key="triage-open",
            status=TaskStatus.COMPLETED,
        )
    )
    await add_routing_work(db, "new-work")
    result = await ensure_triage(handler)
    assert result["task_id"] == "legacy-triage"
    assert result["created"] is False
    assert (await db.get_task("legacy-triage")).description == "Keep this report history"
    assert len(await triage_rows(db)) == 1


async def test_generic_ensure_task_still_creates_a_new_run_after_completion(setup):
    handler, db, _ = setup
    args = {"project_id": "p", "dedup_key": "review:one", "title": "Review", "profile_id": "worker"}
    first = await handler.execute("ensure_task", args)
    await db.transition_task(first["task_id"], TaskStatus.COMPLETED, force=True)
    second = await handler.execute("ensure_task", args)
    assert second["created"] is True
    assert second["task_id"] != first["task_id"]


async def test_reusable_triage_survives_auto_archive(setup):
    handler, db, _ = setup
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=0)
    assert tid not in archived
    assert await db.get_task(tid) is not None


async def test_shipped_pipeline_routes_and_coalesces_real_pending_work(setup):
    handler, db, _ = setup
    await db.create_task(Task(id="unrouted", project_id="p", title="New work", description=""))
    markdown = Path("src/prompts/default_playbooks/default-pipeline.md").read_text()
    compiled = compile_pipeline(markdown)
    assert compiled.success
    result = await PipelineRunner(
        compiled.playbook.to_dict(), {"project_id": "p", "task_id": "unrouted"}, handler
    ).run()
    assert result.status == "completed"
    assert len(await triage_rows(db)) == 1
    assert any(g["status"] == "open" for g in await db.get_gates_for_task("unrouted"))


async def test_auto_archive_still_handles_ordinary_unprofiled_tasks(setup):
    _, db, _ = setup
    await db.create_task(
        Task(
            id="ordinary",
            project_id="p",
            title="Finished work",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=0)
    assert "ordinary" in archived


async def test_disabled_playbooks_leave_pending_triage_stopped(setup):
    handler, db, orch = setup
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    await add_routing_work(db, "new-work")
    orch.config.playbooks.enabled = False
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    orch.config.playbooks.enabled = True
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.READY


async def test_legacy_duplicate_history_does_not_start_a_second_active_triage(setup):
    handler, db, _ = setup
    await db.create_task(
        Task(
            id="old-triage",
            project_id="p",
            title="Triage",
            description="Old report",
            profile_id="triage",
            dedup_key="triage-open",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_task(
        Task(
            id="active-triage",
            project_id="p",
            title="Triage",
            description="Active run",
            profile_id="triage",
            dedup_key="triage-open",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="agent-triage",
        )
    )
    await add_routing_work(db, "one")
    result = await ensure_triage(handler)
    assert result["created"] is False
    assert (await db.get_task("old-triage")).status == TaskStatus.COMPLETED
    assert (await db.get_task("active-triage")).assigned_agent_id == "agent-triage"
    assert len(await triage_rows(db)) == 2


async def test_legacy_draining_duplicate_blocks_reuse_until_session_stops(setup):
    handler, db, _ = setup
    for tid in ("canonical", "old-duplicate"):
        await db.create_task(
            Task(
                id=tid,
                project_id="p",
                title="Triage",
                description="",
                profile_id="triage",
                dedup_key="triage-open",
                status=TaskStatus.COMPLETED,
            )
        )
    await db.create_session(
        SessionRecord(
            id="old-session",
            project_id="p",
            profile_id="triage",
            harness="codex",
            provider="fake",
            name="old-triage",
            lifecycle="task",
            work_dir="/tmp/triage-test",
            epoch="test",
            instance_token="test-token",
            started_at=time.time(),
            task_id="old-duplicate",
            state="draining",
            agent_id="agent-triage",
        )
    )
    await add_routing_work(db, "one")
    await ensure_triage(handler)
    assert (await db.get_task("canonical")).status == TaskStatus.COMPLETED
    await db.update_session("old-session", state="stopped", desired_state="stopped")
    await ensure_triage(handler)
    assert (await db.get_task("canonical")).status == TaskStatus.READY


def install_default_pipeline(orch):
    manager = PlaybookManager(config=orch.config)
    compiled = compile_pipeline(
        Path("src/prompts/default_playbooks/default-pipeline.md").read_text()
    )
    assert compiled.success
    pb = compiled.playbook
    manager._active[pb.id] = pb
    manager._index_triggers(pb)
    orch.playbook_manager = manager
    return manager, pb


async def test_first_wakeup_recovers_from_persisted_gate_when_event_was_lost(setup):
    _, db, orch = setup
    install_default_pipeline(orch)
    await add_routing_work(db, "lost-event")
    assert await triage_rows(db) == []
    await orch._reconcile_triage_tasks()
    rows = await triage_rows(db)
    assert len(rows) == 1
    assert rows[0].status == TaskStatus.READY


async def test_project_override_can_disable_recovery_of_old_default_triage(setup):
    handler, db, orch = setup
    manager, pb = install_default_pipeline(orch)
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    custom = replace(pb, id="custom-pipeline", scope="project", nodes={}, pipeline_rules={})
    manager._active[custom.id] = custom
    manager._index_triggers(custom)
    manager.set_scope_identifier(custom.id, "p")
    await add_routing_work(db, "new-work")
    await orch._reconcile_triage_tasks()
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED


async def test_no_configured_default_policy_does_not_invent_first_triage_task(setup):
    _, db, orch = setup
    orch.playbook_manager = PlaybookManager(config=orch.config)
    await add_routing_work(db, "custom-routed-work")
    await orch._reconcile_triage_tasks()
    assert await triage_rows(db) == []


async def test_session_only_summary_remains_in_history_after_next_run_overwrites_metadata(setup):
    handler, db, _ = setup
    await add_routing_work(db, "one")
    tid = (await ensure_triage(handler))["task_id"]
    await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    await db.set_task_meta(tid, "summary", "First session routed the queue")
    await db.set_task_meta(tid, "outcome", "pass")
    await db.set_task_meta(tid, "close_session_id", "first-session")
    await add_routing_work(db, "second")
    await ensure_triage(handler)
    await db.set_task_meta(tid, "summary", "Second session report")
    contexts = await db.get_task_contexts(tid)
    assert any(
        "First session routed the queue" in row["content"] and "first-session" in row["content"]
        for row in contexts
    )
    assert await db.get_task_meta(tid, "summary") == "Second session report"
