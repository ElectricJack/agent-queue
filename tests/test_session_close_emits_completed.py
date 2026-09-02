"""Regression: ``aq task close`` must emit ``task.completed`` on the bus.

Every agent is session-routed today (``_execute_task`` forks to
``_launch_session_for_task`` and returns — execution.py §"Session-runtime
fork").  The legacy blocking tail below that fork — which held the only
``bus.emit("task.completed", ...)`` for an ordinary task — is dead code.

That left the session close path emitting ``task.closed`` alone, while the
default pipeline's ``per-task-review`` and ``per-branch-final-review`` rules
are both ``"on": "task.completed"``.  The result was silent: workers opened
PRs, closed their tasks, and no reviewer task was ever spawned and no PR was
ever merged.

``tests/test_review_pipeline_e2e.py`` did not catch it because it dispatches
``task.completed`` into the engine by hand rather than letting the close path
raise it.  These tests close the loop: they assert the *real* close path
raises the event, and that ``branch_name``/``pr_url`` are on the row (and so
on the hydrated ``event.task``) by the time it fires.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator


@pytest.fixture
def orchestrator_factory(tmp_path):
    async def _make():
        db = Database(str(tmp_path / "close.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "close.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.git.arev_parse = AsyncMock(return_value="")
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        o.command_handler = CommandHandler(o, cfg)

        # The completion pipeline is repo mechanics; these tests are about the
        # event the close path raises.  Echo back any PR the task already
        # carries, the way a real discovery would.
        async def _noop_pipeline(ctx):
            return (getattr(ctx.task, "pr_url", None) or "", True)

        o._run_completion_pipeline = _noop_pipeline

        async def _noop_release(task_id, *, agent_id=None, workspace_path=None,
                                expect_claim_epoch=None):
            return None

        o.release_session_task_resources = _noop_release
        return o

    return _make


def _emitted(bus, event_type: str) -> list[dict]:
    return [
        call.args[1]
        for call in bus.emit.await_args_list
        if call.args and call.args[0] == event_type
    ]


async def _seed(h: CommandHandler) -> None:
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="Worker"))


@pytest.mark.asyncio
async def test_session_close_emits_task_completed(orchestrator_factory):
    """A passing close raises ``task.completed`` with the base event triple."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    result = await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )
    assert result.get("success"), result

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1, (
        "session close must raise exactly one task.completed; "
        f"emitted={[c.args[0] for c in orch.bus.emit.await_args_list if c.args]}"
    )
    payload = completed[0]
    # The base triple every task.* consumer relies on (event_schemas.py).
    assert payload["task_id"] == task_id
    assert payload["project_id"] == "p"
    assert payload["title"] == "Do work"


@pytest.mark.asyncio
async def test_branch_and_pr_survive_to_the_completed_event(orchestrator_factory):
    """``event.task`` hydration reads the row, so the row must carry both fields.

    ``Orchestrator._dispatch_playbook`` hydrates ``event.task`` from a fresh
    ``db.get_task``; the pipeline's ``when`` guards then read
    ``event.task.branch_name`` / ``event.task.pr_url``.  Assert the row still
    has both once the close has committed — a close that cleared either would
    silently disarm both review rules.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    assert (await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )).get("success")

    row = await h.db.get_task(task_id)
    assert row.status == TaskStatus.COMPLETED
    assert row.branch_name == "aq/do-work"
    assert row.pr_url == "https://github.com/o/r/pull/7"


@pytest.mark.asyncio
async def test_failed_close_does_not_emit_task_completed(orchestrator_factory):
    """Only a task that actually reaches COMPLETED may raise the event.

    A transient failure re-queues to READY; spawning a reviewer for work that
    is about to be retried would be worse than the bug this file guards.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(task_id, branch_name="aq/do-work")
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    await h.execute(
        "task_close",
        {
            "task_id": task_id,
            "outcome": "fail",
            "failure_class": "transient",
            "summary": "broke",
        },
    )

    assert _emitted(orch.bus, "task.completed") == []
    # ``task.closed`` is the always-emitted close record and must still fire.
    assert len(_emitted(orch.bus, "task.closed")) == 1


@pytest.mark.asyncio
async def test_task_show_reports_branch_and_pr(orchestrator_factory):
    """``task_show`` must surface ``branch_name``/``pr_url`` from the row.

    ``cli/formatters.py`` renders ``task.branch_name or "—"``, so omitting the
    key from the payload made every task read as branchless on ``aq task show``
    — which is what made this outage look like a persistence bug when the rows
    were correct all along.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )

    info = await h.execute("task_show", {"task_id": task_id})
    assert info["branch_name"] == "aq/do-work"
    assert info["pr_url"] == "https://github.com/o/r/pull/7"

    # The keys are unconditional: absent values read as None, not as a
    # missing key, so callers can rely on the payload's shape.
    bare = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Bare", "profile_id": "worker"},
        )
    )["created"]
    bare_info = await h.execute("task_show", {"task_id": bare})
    assert bare_info["branch_name"] is None
    assert bare_info["pr_url"] is None


@pytest.mark.asyncio
async def test_close_drives_the_review_chain(orchestrator_factory, pipeline_engine_factory):
    """The real close path's own event spawns the review + final-review chain.

    ``tests/test_review_pipeline_e2e.py`` proves the chain given a
    ``task.completed`` event; it hand-writes that event, so it stayed green
    through an outage where nothing emitted one.  This test never writes the
    payload: it takes whatever the close path put on the bus and feeds exactly
    that to the pipeline, so the two halves can no longer drift apart.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    assert (await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )).get("success")

    completed = _emitted(orch.bus, "task.completed")
    assert completed, "close path emitted no task.completed to dispatch"
    await engine.dispatch("task.completed", completed[0], event_id="close-emitted")

    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.profile_id == "reviewer"], (
        "per-task-review did not fire on the close path's own event"
    )
    assert [t for t in tasks if t.profile_id == "final-reviewer"], (
        "per-branch-final-review did not fire on the close path's own event"
    )
