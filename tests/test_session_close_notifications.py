"""``notify.*`` task-outcome events raised by the session-close path.

``DiscordNotificationHandler`` subscribes to ``notify.task_completed``,
``notify.task_failed`` and ``notify.task_blocked``, but the only code that
ever emitted them was the legacy blocking execution tail, deleted in
``refactor(orchestrator): remove legacy execution tail``.  Every agent is
session-routed now, so ``_complete_session_task_locked`` is the single place
an ordinary task reaches a terminal state — these tests pin the pairing there
so the Discord failure/success feed cannot silently go dead again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Agent, AgentState, Project, Task, TaskStatus
from src.orchestrator import Orchestrator


@pytest.fixture
async def orch(tmp_path):
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
    o.bus = EventBus(env="dev")  # payload schema violations raise here

    async def _noop_pipeline(ctx):
        return (getattr(ctx.task, "pr_url", None) or "", True)

    o._run_completion_pipeline = _noop_pipeline

    async def _noop_release(task_id, *, agent_id=None, workspace_path=None, expect_claim_epoch=None):
        return None

    o.release_session_task_resources = _noop_release
    return o


async def _seed(orch, *, task_id: str, retry_count: int = 0, max_retries: int = 3) -> Task:
    await orch.db.create_project(Project(id="p-1", name="Test"))
    await orch.db.create_agent(
        Agent(id="a-1", name="agent-1", profile_id="claude", state=AgentState.BUSY)
    )
    task = Task(
        id=task_id,
        project_id="p-1",
        title="Close me",
        description="test",
        status=TaskStatus.IN_PROGRESS,
        assigned_agent_id="a-1",
        retry_count=retry_count,
        max_retries=max_retries,
    )
    await orch.db.create_task(task)
    return task


def _collect(bus: EventBus, *event_types: str) -> dict[str, list[dict]]:
    seen: dict[str, list[dict]] = {t: [] for t in event_types}
    for event_type in event_types:
        bus.subscribe(event_type, lambda data, t=event_type: seen[t].append(data))
    return seen


NOTIFY = ("notify.task_completed", "notify.task_failed", "notify.task_blocked")


@pytest.mark.asyncio
async def test_pass_close_emits_notify_task_completed(orch):
    task = await _seed(orch, task_id="t-pass")
    seen = _collect(orch.bus, *NOTIFY)

    await orch.complete_session_task(
        task, outcome="pass", work_outcome="shipped", notes="all done"
    )

    assert len(seen["notify.task_completed"]) == 1
    event = seen["notify.task_completed"][0]
    assert event["task"]["id"] == "t-pass"
    assert event["agent"]["id"] == "a-1"
    assert event["summary"] == "all done"
    assert event["task"]["status"] == TaskStatus.COMPLETED.value
    assert event["project_id"] == "p-1"
    assert seen["notify.task_failed"] == []
    assert seen["notify.task_blocked"] == []


@pytest.mark.asyncio
async def test_pass_close_without_agent_emits_notify_task_completed(orch):
    """A reaped or unassigned agent must not suppress the outcome event."""
    await orch.db.create_project(Project(id="p-1", name="Test"))
    task = Task(
        id="t-no-agent",
        project_id="p-1",
        title="Close me",
        description="test",
        status=TaskStatus.IN_PROGRESS,
    )
    await orch.db.create_task(task)
    seen = _collect(orch.bus, *NOTIFY)

    await orch.complete_session_task(
        task, outcome="pass", work_outcome="shipped", notes="all done"
    )

    assert len(seen["notify.task_completed"]) == 1
    event = seen["notify.task_completed"][0]
    assert event["task"]["id"] == "t-no-agent"
    assert event["agent"]["id"] == ""
    assert event["agent"]["settings"]["profile_id"] == ""
    assert seen["notify.task_failed"] == []
    assert seen["notify.task_blocked"] == []


@pytest.mark.asyncio
async def test_transient_failure_emits_notify_task_failed_with_retry(orch):
    task = await _seed(orch, task_id="t-retry", retry_count=0, max_retries=3)
    seen = _collect(orch.bus, *NOTIFY)

    await orch.complete_session_task(
        task, outcome="fail", failure_class="transient", notes="flaky run"
    )

    assert len(seen["notify.task_failed"]) == 1
    event = seen["notify.task_failed"][0]
    assert event["task"]["id"] == "t-retry"
    assert event["agent"]["id"] == "a-1"
    assert event["error_detail"] == "flaky run"
    assert event["retry_count"] == 1
    assert event["task"]["status"] == TaskStatus.READY.value
    assert event["task"]["retry_count"] == 1
    assert event["max_retries"] == 3
    assert event["severity"] == "error"
    assert seen["notify.task_completed"] == []
    assert seen["notify.task_blocked"] == []


@pytest.mark.asyncio
async def test_last_retry_emits_notify_task_blocked(orch):
    task = await _seed(orch, task_id="t-blocked", retry_count=2, max_retries=3)
    seen = _collect(orch.bus, *NOTIFY)

    await orch.complete_session_task(
        task, outcome="fail", failure_class="transient", notes="still broken"
    )

    assert len(seen["notify.task_blocked"]) == 1
    event = seen["notify.task_blocked"][0]
    assert event["task"]["id"] == "t-blocked"
    assert event["last_error"] == "still broken"
    assert event["task"]["status"] == TaskStatus.BLOCKED.value
    assert seen["notify.task_failed"] == []
    assert seen["notify.task_completed"] == []


@pytest.mark.asyncio
async def test_hard_failure_emits_notify_task_failed_not_blocked(orch):
    """A hard failure is terminal but is not retry exhaustion."""
    task = await _seed(orch, task_id="t-hard")
    seen = _collect(orch.bus, *NOTIFY)

    await orch.complete_session_task(
        task, outcome="fail", failure_class="hard", notes="unfixable"
    )

    assert len(seen["notify.task_failed"]) == 1
    assert seen["notify.task_failed"][0]["error_detail"] == "unfixable"
    assert seen["notify.task_blocked"] == []
    assert seen["notify.task_completed"] == []


@pytest.mark.asyncio
async def test_notification_failure_does_not_undo_the_transition(orch):
    """A blown-up transport must not propagate out of the close."""
    task = await _seed(orch, task_id="t-boom")

    async def _boom(*args, **kwargs):
        raise RuntimeError("discord is down")

    orch._emit_notify = _boom

    result = await orch.complete_session_task(task, outcome="pass", work_outcome="shipped")

    assert result["status"] == TaskStatus.COMPLETED.value
    refreshed = await orch.db.get_task("t-boom")
    assert refreshed.status == TaskStatus.COMPLETED
