"""Tests for task.failed event emission from the orchestrator."""

from __future__ import annotations

import asyncio

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from tests.assignment_routing_helpers import install_already_routed
from tests.session_dispatch_helpers import (
    create_session_project,
    drain_running_tasks,
)


class MockAdapter:
    def __init__(self, result=AgentResult.FAILED, tokens=100):
        self._result = result
        self._tokens = tokens

    async def start(self, task):
        pass

    async def wait(self, on_message=None):
        return AgentOutput(result=self._result, summary="Failed", tokens_used=self._tokens)

    async def stop(self):
        pass

    async def is_alive(self):
        return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.FAILED, tokens=100):
        self.result = result
        self.tokens = tokens

    def create(self, agent_type: str, profile=None, llm_logger=None):
        return MockAdapter(result=self.result, tokens=self.tokens)


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    # Mock-git tests predate the worktrees P6 default; keep legacy path.
    config.worktrees.enabled = False
    o = Orchestrator(config, runtimes=MockAdapterFactory())
    await o.initialize()
    install_already_routed(o)
    yield o
    if o._running_tasks:
        await asyncio.gather(*o._running_tasks.values(), return_exceptions=True)
        o._running_tasks.clear()
    await o.shutdown()


async def _setup_project(db, project_id="p-1", workspace_path="/tmp/ws"):
    await db.create_project(Project(id=project_id, name="Test"))
    await db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=RepoSourceType.LINK,
        )
    )


class TestTaskFailedEvent:
    @pytest.mark.asyncio
    async def test_stop_task_emits_task_failed(self, orch):
        """Stopping a task should emit task.failed with context='stop_task'."""
        await _setup_project(orch.db)
        agent = Agent(id="a-1", name="agent-1", profile_id="claude", state=AgentState.IDLE)
        await orch.db.create_agent(agent)
        task = Task(
            id="t-stop",
            project_id="p-1",
            title="Stoppable task",
            description="test",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a-1",
        )
        await orch.db.create_task(task)

        events = []
        orch.bus.subscribe("task.failed", lambda data: events.append(data))

        await orch.stop_task("t-stop")

        assert len(events) == 1
        assert events[0]["task_id"] == "t-stop"
        assert events[0]["context"] == "stop_task"
        assert events[0]["title"] == "Stoppable task"

    async def _launch_with_one_retry(self, session_orch):
        """A session-run task whose next transient failure spends its retry budget."""
        orch = session_orch
        await create_session_project(orch)
        await orch.db.create_task(
            Task(
                id="t-retry",
                project_id="p-1",
                title="Retry task",
                description="test",
                status=TaskStatus.READY,
                max_retries=1,
                retry_count=0,
            )
        )
        await orch.run_one_cycle()
        await drain_running_tasks(orch)
        task = await orch.db.get_task("t-retry")
        assert task.status == TaskStatus.IN_PROGRESS
        assert await orch.db.get_session_for_task("t-retry") is not None
        return task

    @pytest.mark.asyncio
    async def test_max_retries_on_session_close_blocks_and_emits_task_closed(self, session_orch):
        """Retry exhaustion now arrives through ``aq task close``, not a runtime result.

        The legacy result branch is gone; a transient failure with the budget
        spent goes BLOCKED with context ``max_retries`` on the close path and
        announces itself as ``task.closed``.  Companion state-only coverage:
        ``test_session_commands.py::TestEndToEndOnFakeProvider::
        test_transient_failure_blocks_once_retries_are_spent``.
        """
        orch = session_orch
        task = await self._launch_with_one_retry(orch)

        closed = []
        orch.bus.subscribe("task.closed", lambda data: closed.append(data))

        result = await orch.complete_session_task(
            task, outcome="fail", failure_class="transient", notes="flaky"
        )

        assert result["status"] == "BLOCKED"
        assert result["retry_count"] == 1
        assert (await orch.db.get_task("t-retry")).status == TaskStatus.BLOCKED
        assert [e["task_id"] for e in closed] == ["t-retry"]
        assert closed[0]["status"] == "BLOCKED"
        assert closed[0]["outcome"] == "fail"

    @pytest.mark.asyncio
    async def test_max_retries_on_session_close_emits_task_failed(self, session_orch):
        """When max retries are exhausted, task.failed is emitted with context='max_retries'."""
        orch = session_orch
        task = await self._launch_with_one_retry(orch)

        events = []
        orch.bus.subscribe("task.failed", lambda data: events.append(data))

        await orch.complete_session_task(
            task, outcome="fail", failure_class="transient", notes="flaky"
        )

        assert len(events) == 1
        assert events[0]["task_id"] == "t-retry"
        assert events[0]["context"] == "max_retries"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("close_kwargs", "expected_context"),
        [
            ({"failure_class": "transient"}, "max_retries"),
            ({"failure_class": "hard"}, "session_close_hard_failure"),
        ],
    )
    async def test_session_close_blocked_legs_emit_task_failed(
        self, orch, close_kwargs, expected_context
    ):
        """A session close that ends terminally in BLOCKED must emit task.failed.

        task.failed is the reflection playbook's trigger and the failure
        notification path; the session close path used to emit only
        task.closed, so a worker that spent its retry budget (or closed
        --failure-class hard) got neither.
        """
        await _setup_project(orch.db)
        await orch.db.create_profile(
            AgentProfile(id="claude", name="Claude", harness="claude")
        )
        agent = Agent(id="a-3", name="agent-3", profile_id="claude", state=AgentState.BUSY)
        await orch.db.create_agent(agent)
        task = Task(
            id="t-close",
            project_id="p-1",
            title="Session close task",
            description="test",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a-3",
            profile_id="claude",
            max_retries=1,
            retry_count=0,
        )
        await orch.db.create_task(task)

        seen: list[tuple[str, dict]] = []
        orch.bus.subscribe("task.failed", lambda d: seen.append(("task.failed", d)))
        orch.bus.subscribe("task.closed", lambda d: seen.append(("task.closed", d)))

        result = await orch.complete_session_task(
            task, outcome="fail", notes="it broke", **close_kwargs
        )

        assert result["status"] == TaskStatus.BLOCKED.value
        failed = [d for name, d in seen if name == "task.failed"]
        assert len(failed) == 1
        payload = failed[0]
        assert payload["task_id"] == "t-close"
        assert payload["project_id"] == "p-1"
        assert payload["title"] == "Session close task"
        assert payload["context"] == expected_context
        assert payload["status"] == TaskStatus.BLOCKED.value
        assert payload["error"] == "it broke"
        assert payload["agent_id"] == "a-3"
        assert payload["agent_type"] == "claude"
        # Ordering: subscribers see the failure before the close.
        assert [name for name, _ in seen] == ["task.failed", "task.closed"]

    @pytest.mark.asyncio
    async def test_session_close_retry_leg_does_not_emit_task_failed(self, orch):
        """A close that still has retry budget is re-queued, not failed."""
        await _setup_project(orch.db)
        await orch.db.create_profile(
            AgentProfile(id="claude", name="Claude", harness="claude")
        )
        agent = Agent(id="a-4", name="agent-4", profile_id="claude", state=AgentState.BUSY)
        await orch.db.create_agent(agent)
        task = Task(
            id="t-retryable",
            project_id="p-1",
            title="Retryable task",
            description="test",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a-4",
            profile_id="claude",
            max_retries=3,
            retry_count=0,
        )
        await orch.db.create_task(task)

        events = []
        orch.bus.subscribe("task.failed", lambda d: events.append(d))

        result = await orch.complete_session_task(
            task, outcome="fail", failure_class="transient", notes="flake"
        )

        assert result["status"] == TaskStatus.READY.value
        assert events == []

    @pytest.mark.asyncio
    async def test_emit_task_failure_payload_structure(self, orch):
        """The task.failed payload should include all expected fields."""
        await _setup_project(orch.db)
        task = Task(
            id="t-payload",
            project_id="p-1",
            title="Payload test",
            description="test",
            status=TaskStatus.BLOCKED,
        )
        await orch.db.create_task(task)

        events = []
        orch.bus.subscribe("task.failed", lambda data: events.append(data))

        await orch._emit_task_failure(task, "test_context", error="test error")

        assert len(events) == 1
        payload = events[0]
        assert payload["task_id"] == "t-payload"
        assert payload["project_id"] == "p-1"
        assert payload["title"] == "Payload test"
        assert payload["context"] == "test_context"
        assert payload["error"] == "test error"
        assert "status" in payload
