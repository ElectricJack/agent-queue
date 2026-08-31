"""Committed command changes reach the dashboard without invoking routing again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.api.websocket import WebSocketManager
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "graph.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="One"))
    await db.create_project(Project(id="p2", name="Two"))
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "graph.db"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    handler = CommandHandler(orch, config)
    manager = WebSocketManager(orch.bus, db)
    queue = asyncio.Queue()
    manager._clients[object()] = queue
    manager.start()
    yield handler, db, queue
    manager.shutdown()
    await db.close()


async def task(db, task_id="t", project_id="p1", status=TaskStatus.READY):
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title=task_id,
            description="PRIVATE DESCRIPTION",
            status=status,
        )
    )


def frames(queue, kind):
    out = []
    while not queue.empty():
        frame = queue.get_nowait()
        if frame["_event_type"] == kind:
            out.append(frame)
    return out


async def assert_change(db, queue, kind, task_id="t", projects=("p1",)):
    sent = frames(queue, kind)
    assert {frame["project_id"] for frame in sent} == set(projects)
    assert len(sent) == len(projects)
    for frame in sent:
        assert frame["task_id"] == task_id
        assert "PRIVATE DESCRIPTION" not in str(frame)
        assert isinstance(frame["seq"], int)
        persisted = await db.get_recent_events(after_id=frame["seq"] - 1)
        assert persisted[0]["event_type"] == kind
        assert persisted[0]["project_id"] == frame["project_id"]


async def test_edit_publishes_committed_graph_change_for_both_projects(setup):
    handler, db, queue = setup
    await task(db)
    res = await handler._cmd_edit_task({"task_id": "t", "title": "Renamed", "project_id": "p2"})
    assert res["updated"] == "t"
    assert (await db.get_task("t")).project_id == "p2"
    await assert_change(db, queue, "task.updated", projects=("p1", "p2"))


@pytest.mark.parametrize("dep_type", ["blocks", "parent-child", "related"])
async def test_dependency_edits_reach_live_graph_even_without_blocked_flip(setup, dep_type):
    handler, db, queue = setup
    await task(db)
    await task(db, "parent")
    args = {"task_id": "t", "depends_on": "parent", "dep_type": dep_type}
    assert (await handler._cmd_add_dependency(args))["ok"]
    assert ("parent", dep_type) in await db.get_typed_dependencies("t")
    await assert_change(db, queue, "task.updated")
    assert (await handler._cmd_remove_dependency(args))["ok"]
    assert await db.get_typed_dependencies("t") == []
    await assert_change(db, queue, "task.updated")


@pytest.mark.parametrize("cascade", [False, True])
async def test_deletion_keeps_project_context_after_row_is_gone(setup, cascade):
    handler, db, queue = setup
    await task(db)
    if cascade:
        await task(db, "child")
        await db.add_dependency("child", "t", "parent-child")
    assert (await handler._cmd_delete_task({"task_id": "t", "cascade": cascade}))["deleted"] == "t"
    assert await db.get_task("t") is None
    await assert_change(db, queue, "task.deleted")


@pytest.mark.parametrize("bulk", [False, True])
async def test_archive_notifies_only_successfully_archived_rows(setup, bulk):
    handler, db, queue = setup
    await task(db, status=TaskStatus.COMPLETED)
    args = {"project_id": "p1"} if bulk else {"task_id": "t"}
    assert "error" not in await handler._cmd_archive_task(args)
    assert await db.get_task("t") is None
    await assert_change(db, queue, "task.archived")


async def test_ensure_creation_refreshes_graph_without_refiring_creation_pipeline(setup):
    handler, db, queue = setup
    args = {"project_id": "p1", "title": "Triage", "dedup_key": "triage-key"}
    first = await handler._cmd_ensure_task(args)
    assert first["created"] is True
    sent = list(queue._queue)
    assert not any(frame["_event_type"] == "task.created" for frame in sent)
    await assert_change(db, queue, "task.updated", task_id=first["task_id"])
    second = await handler._cmd_ensure_task(args)
    assert second["created"] is False
    assert queue.empty()


async def test_rejected_changes_emit_no_success_events(setup):
    handler, db, queue = setup
    await task(db)
    for operation, args in [
        (handler._cmd_edit_task, {"task_id": "t", "project_id": "missing"}),
        (handler._cmd_add_dependency, {"task_id": "t", "depends_on": "missing"}),
        (handler._cmd_remove_dependency, {"task_id": "t", "depends_on": "missing"}),
        (handler._cmd_delete_task, {"task_id": "missing"}),
        (handler._cmd_archive_task, {"task_id": "t"}),
    ]:
        assert "error" in await operation(args)
    assert queue.empty()


async def test_subscriber_failure_does_not_report_committed_edit_as_failed(setup):
    handler, db, queue = setup
    await task(db)

    def broken(_frame):
        raise RuntimeError("offline subscriber")

    handler.orchestrator.bus.subscribe("task.updated", broken)
    assert (await handler._cmd_edit_task({"task_id": "t", "priority": 20}))["updated"] == "t"
    assert (await db.get_task("t")).priority == 20
    assert await db.get_recent_events(event_type="task.updated", task_id="t")


def graph_document():
    return {
        "version": 1,
        "parent": {"title": "Epic"},
        "nodes": [
            {"key": "a", "title": "A", "acceptance": ["done"]},
            {"key": "b", "title": "B", "acceptance": ["done"], "needs": [{"on": "a"}]},
        ],
    }


def graph_operation(handler, kind):
    if kind == "graph":
        return handler._cmd_create_task_graph, {"project_id": "p1", "graph": graph_document()}
    from src.task_graph.formulas import FormulaRegistry, load_from_vault

    vault = Path(handler.config.vault_root)
    (vault / "formulas").mkdir(parents=True)
    (vault / "formulas" / "live.md").write_text(
        "---\nname: live\n---\n# Live\n\n```aq-graph\n" + json.dumps(graph_document()) + "\n```\n"
    )
    registry = FormulaRegistry()
    assert load_from_vault(registry, str(vault)) == []
    handler.orchestrator.formula_registry = registry
    return handler._cmd_formula_cook, {"project_id": "p1", "name": "live"}


@pytest.mark.parametrize("kind", ["graph", "formula"])
async def test_batch_creation_notifies_once_only_after_whole_graph_commits(setup, kind):
    handler, db, queue = setup
    operation, args = graph_operation(handler, kind)
    observed = []

    async def inspect_commit(frame):
        rows = await db.list_tasks(project_id=frame["project_id"])
        observed.append({row.id for row in rows})

    handler.orchestrator.bus.subscribe("task.updated", inspect_commit)
    result = await operation(args)
    assert result["created"] is True
    assert len(observed) == 1
    assert observed[0] == {result["parent_id"], *result["task_ids"]}
    await assert_change(db, queue, "task.updated", task_id=result["parent_id"])


@pytest.mark.parametrize("kind", ["graph", "formula"])
async def test_batch_dry_run_publishes_nothing(setup, kind):
    handler, db, queue = setup
    operation, args = graph_operation(handler, kind)
    result = await operation({**args, "dry_run": True})
    assert result["created"] is False
    assert await db.list_tasks("p1") == []
    assert queue.empty()


@pytest.mark.parametrize("kind", ["graph", "formula"])
async def test_failed_batch_transaction_does_not_emit_update(setup, kind, monkeypatch):
    from sqlalchemy import select
    from src.database.tables import tasks

    handler, db, queue = setup
    operation, args = graph_operation(handler, kind)

    async def abort_before_commit(_task_ids, *, conn):
        assert (await conn.execute(select(tasks.c.id))).first() is not None
        raise RuntimeError("abort graph transaction")

    monkeypatch.setattr(db, "recompute_blocked", abort_before_commit)
    with pytest.raises(RuntimeError, match="abort graph transaction"):
        await operation(args)
    assert await db.list_tasks("p1") == []
    assert queue.empty()
