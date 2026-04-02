import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.orchestrator import Orchestrator
from src.database import Database
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState, AgentResult,
    TaskContext, AgentOutput, RepoConfig, RepoSourceType, Workspace,
)
from src.adapters.base import AgentAdapter, MessageCallback
from src.config import AppConfig, AutoTaskConfig


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000,
                 on_wait=None):
        self._result = result
        self._tokens = tokens
        self._on_wait = on_wait
        self._ctx = None

    async def start(self, task):
        self._ctx = task  # TaskContext

    async def wait(self, on_message=None):
        if self._on_wait:
            self._on_wait(self._ctx)
        return AgentOutput(result=self._result, summary="Done",
                           tokens_used=self._tokens)

    async def stop(self): pass
    async def is_alive(self): return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000,
                 on_wait=None):
        self.result = result
        self.tokens = tokens
        self.on_wait = on_wait
        self.last_profile = None
        self.create_calls = []

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        self.last_profile = profile
        self.create_calls.append({"agent_type": agent_type, "profile": profile})
        return MockAdapter(result=self.result, tokens=self.tokens,
                           on_wait=self.on_wait)


async def _drain_running_tasks(orch: Orchestrator) -> None:
    """Wait for all background tasks launched by the orchestrator to complete.

    ``run_one_cycle`` launches ``_execute_task_safe`` as background
    ``asyncio.Task`` objects.  Tests must await these before asserting on
    final task status, otherwise there is a race between the background
    coroutine and the assertions.
    """
    if orch._running_tasks:
        await asyncio.gather(*orch._running_tasks.values(), return_exceptions=True)
        orch._running_tasks.clear()


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    o = Orchestrator(config, adapter_factory=MockAdapterFactory())
    await o.initialize()
    yield o
    # Drain any remaining background tasks before closing DB
    await _drain_running_tasks(o)
    await o.shutdown()


async def _create_project_with_workspace(
    db, project_id: str = "p-1", name: str = "alpha",
    workspace_path: str = "/tmp/test-workspace",
) -> None:
    """Create a project and an associated workspace so task execution succeeds."""
    await db.create_project(Project(id=project_id, name=name))
    await db.create_workspace(Workspace(
        id=f"ws-{project_id}",
        project_id=project_id,
        workspace_path=workspace_path,
        source_type=RepoSourceType.LINK,
    ))


async def _run_cycle_and_wait(orch):
    """Run one scheduling cycle and wait for all background task executions."""
    await orch.run_one_cycle()
    await orch.wait_for_running_tasks()


async def _approve_plan_for_task(orch, task_id: str) -> list:
    """Simulate plan approval: transition to COMPLETED and promote subtasks.

    Returns an empty list (subtask creation is now handled by the supervisor
    LLM via break_plan_into_tasks, not by the orchestrator).
    """
    task = await orch.db.get_task(task_id)
    if not task or task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
        return []
    await orch.db.transition_task(task_id, TaskStatus.COMPLETED, context="plan_approved")
    await orch._check_defined_tasks()
    return []


async def _run_cycle_and_approve_plan(orch, task_id: str) -> list:
    """Run one cycle, wait for completion, then auto-approve any plan.

    Convenience wrapper for tests that expect the old behaviour where
    plan subtasks were created automatically on task completion.
    """
    await _run_cycle_and_wait(orch)
    return await _approve_plan_for_task(orch, task_id)


async def _discover_and_create_subtasks(orch, task, workspace: str) -> list:
    """Discover+store plan (subtask creation is now supervisor-only).

    Returns empty list — subtask creation via regex parsing has been removed.
    Plan discovery still works for the AWAITING_PLAN_APPROVAL flow.
    """
    await orch._discover_and_store_plan(task, workspace)
    return []


class TestOrchestratorLifecycle:
    async def test_full_task_lifecycle(self, orch):
        """DEFINED → READY → ASSIGNED → IN_PROGRESS → COMPLETED"""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
        ))

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

    async def test_failed_task_retries(self, orch):
        orch._adapter_factory = MockAdapterFactory(result=AgentResult.FAILED)
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
            max_retries=2,
        ))

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        # Should be READY for retry (failed once, max 2)
        assert task.status == TaskStatus.READY
        assert task.retry_count == 1

    async def test_paused_on_token_exhaustion(self, orch):
        orch._adapter_factory = MockAdapterFactory(
            result=AgentResult.PAUSED_TOKENS
        )
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
        ))

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.PAUSED
        assert task.resume_after is not None

    async def test_dependencies_block_scheduling(self, orch):
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="First",
            description="Do first", status=TaskStatus.DEFINED,
        ))
        await orch.db.create_task(Task(
            id="t-2", project_id="p-1", title="Second",
            description="Do second", status=TaskStatus.DEFINED,
        ))
        await orch.db.add_dependency("t-2", depends_on="t-1")

        # t-1 has no deps so it gets promoted to READY and executed.
        # After t-1 completes, the pipeline re-checks DEFINED tasks,
        # promoting t-2 to READY within the same cycle.
        await _run_cycle_and_wait(orch)

        t1 = await orch.db.get_task("t-1")
        t2 = await orch.db.get_task("t-2")
        # t-1 was promoted, scheduled, executed, completed
        assert t1.status == TaskStatus.COMPLETED
        # t-2 gets promoted to READY in the same cycle (post-completion re-check)
        assert t2.status == TaskStatus.READY


def _make_plan_toucher(workspace):
    """Create an on_wait callback that touches pre-created plan files.

    Tests pre-create plan files before the orchestration cycle to simulate
    agent-written plans.  The staleness check in _discover_and_store_plan()
    compares file mtime against the task execution start time.  This callback
    runs during adapter.wait() to refresh the mtime, simulating the agent
    writing the file during execution.
    """
    import glob as _glob

    def _touch_plan_files(ctx):
        for pattern in ("**/*.md",):
            for md in _glob.glob(
                os.path.join(str(workspace), ".claude", pattern),
                recursive=True,
            ):
                if os.path.isfile(md) and "plans/" not in md:
                    os.utime(md, None)
        root_plan = os.path.join(str(workspace), "plan.md")
        if os.path.isfile(root_plan):
            os.utime(root_plan, None)

    return _touch_plan_files
class TestAwaitingApprovalNopr:
    """Tests for handling AWAITING_APPROVAL tasks without a PR URL."""

    async def test_auto_completes_no_approval_no_pr(self, orch):
        """Task without requires_approval and no pr_url gets auto-completed
        after the grace period."""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="No-PR Task",
            description="This task has no PR",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=False,
            pr_url=None,
        ))

        # Backdate updated_at so the grace period has elapsed
        await orch.db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (time.time() - 300, "t-1"),
        )
        await orch.db._db.commit()

        # Reset throttle so _check_awaiting_approval actually runs
        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

    async def test_no_auto_complete_within_grace_period(self, orch):
        """Task without requires_approval should NOT be auto-completed while
        still within the grace period."""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Fresh Task",
            description="Just entered AWAITING_APPROVAL",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=False,
            pr_url=None,
        ))
        # updated_at is set to now by create_task, so grace period hasn't elapsed

        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.AWAITING_APPROVAL

    async def test_sends_reminder_for_manual_approval(self, orch):
        """Task with requires_approval=True and no pr_url should trigger
        a notification."""
        notifications = []

        async def capture_notify(msg, project_id=None):
            notifications.append(msg)

        orch.set_notify_callback(capture_notify)

        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Manual Review",
            description="Needs manual review",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=True,
            pr_url=None,
        ))

        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()

        assert len(notifications) == 1
        assert "approve_task t-1" in notifications[0]
        assert "Manual Review" in notifications[0]

    async def test_reminder_is_throttled(self, orch):
        """The same task should not trigger a reminder on every cycle."""
        notifications = []

        async def capture_notify(msg, project_id=None):
            notifications.append(msg)

        orch.set_notify_callback(capture_notify)

        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Manual Review",
            description="Needs manual review",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=True,
            pr_url=None,
        ))

        # First call sends a reminder
        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()
        assert len(notifications) == 1

        # Second call within the reminder interval should NOT send another
        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()
        assert len(notifications) == 1  # still 1

    async def test_escalation_after_threshold(self, orch):
        """After the escalation threshold, a stronger warning is sent."""
        notifications = []

        async def capture_notify(msg, project_id=None):
            notifications.append(msg)

        orch.set_notify_callback(capture_notify)

        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Old Task",
            description="Been here a while",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=True,
            pr_url=None,
        ))

        # Backdate so the task looks like it's been stuck for 25 hours
        await orch.db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (time.time() - 25 * 3600, "t-1"),
        )
        await orch.db._db.commit()

        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()

        assert len(notifications) == 1
        assert "Stuck Task" in notifications[0]
        assert "25h" in notifications[0]

    async def test_cleanup_reminder_tracking_on_completion(self, orch):
        """When a task leaves AWAITING_APPROVAL, its reminder entry is removed."""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Manual Review",
            description="Needs manual review",
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=True,
            pr_url=None,
        ))

        async def noop_notify(msg, project_id=None):
            pass
        orch.set_notify_callback(noop_notify)

        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()
        assert "t-1" in orch._no_pr_reminded_at

        # Simulate manual approval (task is now COMPLETED)
        await orch.db.update_task("t-1", status=TaskStatus.COMPLETED.value)

        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()
        assert "t-1" not in orch._no_pr_reminded_at

    async def test_pr_task_still_checked_normally(self, orch):
        """Tasks WITH a pr_url should still go through the PR-check path."""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_repo(RepoConfig(
            id="repo-1", project_id="p-1",
            source_type=RepoSourceType.INIT,
            url="", default_branch="main",
            source_path="/tmp/fake-checkout",
        ))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="PR Task",
            description="Has a PR",
            status=TaskStatus.AWAITING_APPROVAL,
            pr_url="https://github.com/org/repo/pull/1",
            repo_id="repo-1",
        ))

        # The git check will fail (no real checkout) but the task should not
        # be auto-completed or reminded — only the PR path runs.
        orch._last_approval_check = 0.0
        await orch._check_awaiting_approval()

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.AWAITING_APPROVAL
        assert "t-1" not in orch._no_pr_reminded_at
class TestPlanApprovalBlocking:
    """Tests that plan subtasks are NOT promoted until the plan is approved."""

    @pytest.fixture
    async def orch_with_workspace(self, tmp_path):
        workspace = tmp_path / "workspaces"
        workspace.mkdir()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
        )
        config.auto_task = AutoTaskConfig(enabled=True, chain_dependencies=True)
        o = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await o.initialize()
        yield o, workspace
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_subtasks_blocked_while_parent_awaiting_plan_approval(
        self, orch_with_workspace
    ):
        """Plan subtasks must stay DEFINED when parent is AWAITING_PLAN_APPROVAL."""
        orch, workspace = orch_with_workspace

        await _create_project_with_workspace(orch.db, workspace_path=str(workspace))

        # Create parent task in AWAITING_PLAN_APPROVAL
        parent = Task(
            id="t-plan", project_id="p-1", title="Plan Task",
            description="Create plan", status=TaskStatus.AWAITING_PLAN_APPROVAL,
        )
        await orch.db.create_task(parent)

        # Create subtasks that would normally be promoted
        sub1 = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="First subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-plan", is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2", project_id="p-1", title="Sub 2",
            description="Second subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-plan", is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        # First subtask has blocking dep on parent, second on first
        await orch.db.add_dependency("t-sub-1", depends_on="t-plan")
        await orch.db.add_dependency("t-sub-2", depends_on="t-sub-1")

        # Run _check_defined_tasks — subtasks should NOT be promoted
        await orch._check_defined_tasks()

        s1 = await orch.db.get_task("t-sub-1")
        s2 = await orch.db.get_task("t-sub-2")
        assert s1.status == TaskStatus.DEFINED, "Sub 1 should stay DEFINED"
        assert s2.status == TaskStatus.DEFINED, "Sub 2 should stay DEFINED"

    async def test_subtasks_promoted_after_plan_approved(
        self, orch_with_workspace
    ):
        """After parent transitions to COMPLETED, first subtask gets promoted."""
        orch, workspace = orch_with_workspace

        await _create_project_with_workspace(orch.db, workspace_path=str(workspace))

        # Create parent in AWAITING_PLAN_APPROVAL
        parent = Task(
            id="t-plan", project_id="p-1", title="Plan Task",
            description="Create plan", status=TaskStatus.AWAITING_PLAN_APPROVAL,
        )
        await orch.db.create_task(parent)

        # Create chained subtasks with blocking dep on parent
        sub1 = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="First subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-plan", is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2", project_id="p-1", title="Sub 2",
            description="Second subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-plan", is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        await orch.db.add_dependency("t-sub-1", depends_on="t-plan")
        await orch.db.add_dependency("t-sub-2", depends_on="t-sub-1")

        # Simulate plan approval: transition parent to COMPLETED
        await orch.db.transition_task("t-plan", TaskStatus.COMPLETED,
                                       context="plan_approved")

        # Now run _check_defined_tasks — first subtask should promote
        await orch._check_defined_tasks()

        s1 = await orch.db.get_task("t-sub-1")
        s2 = await orch.db.get_task("t-sub-2")
        assert s1.status == TaskStatus.READY, "Sub 1 should be READY after approval"
        assert s2.status == TaskStatus.DEFINED, "Sub 2 should stay DEFINED (deps not met)"

class TestIsLastSubtask:
    """Tests for the _is_last_subtask helper."""

    @pytest.fixture
    async def orch_with_workspace(self, tmp_path):
        workspace = tmp_path / "workspaces"
        workspace.mkdir()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
        )
        o = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await o.initialize()
        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_single_subtask_is_last(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-parent", project_id="p-1", title="Parent",
            description="Parent task", status=TaskStatus.COMPLETED,
        ))
        sub = Task(
            id="t-sub-1", project_id="p-1", title="Only Sub",
            description="The only subtask", status=TaskStatus.COMPLETED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(sub)
        assert await orch._is_last_subtask(sub) is True

    async def test_not_last_when_sibling_incomplete(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-parent", project_id="p-1", title="Parent",
            description="Parent task", status=TaskStatus.COMPLETED,
        ))
        sub1 = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="First subtask", status=TaskStatus.COMPLETED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2", project_id="p-1", title="Sub 2",
            description="Second subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        assert await orch._is_last_subtask(sub1) is False

    async def test_is_last_when_all_siblings_completed(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(Task(
            id="t-parent", project_id="p-1", title="Parent",
            description="Parent task", status=TaskStatus.COMPLETED,
        ))
        sub1 = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="First subtask", status=TaskStatus.COMPLETED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2", project_id="p-1", title="Sub 2",
            description="Second subtask", status=TaskStatus.COMPLETED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        assert await orch._is_last_subtask(sub2) is True


class TestPrepareWorkspaceRebase:
    """Tests for _prepare_workspace passing rebase + default_branch to switch_to_branch."""

    @pytest.fixture
    async def setup(self, tmp_path):
        """Create orchestrator, project, workspace, agent, parent task, and subtask.

        Returns a dict with all objects needed for _prepare_workspace tests.
        """
        workspace = tmp_path / "workspaces" / "p-1" / "checkout-1"
        workspace.mkdir(parents=True)

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            auto_task=AutoTaskConfig(rebase_between_subtasks=False),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(Project(
            id="p-1", name="alpha",
            repo_url="https://github.com/org/myrepo.git",
            repo_default_branch="develop",
        ))
        await orch.db.create_agent(Agent(
            id="a-1", name="agent-1", agent_type="claude",
        ))

        # Parent task with an existing branch
        parent = Task(
            id="t-parent", project_id="p-1", title="Parent Plan",
            description="Create plan", status=TaskStatus.COMPLETED,
            branch_name="task/t-parent/parent-plan",
        )
        await orch.db.create_task(parent)

        # Subtask that reuses parent's branch
        subtask = Task(
            id="t-sub-1", project_id="p-1", title="Step 1",
            description="First subtask step",
            status=TaskStatus.READY,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        await orch.db.create_task(subtask)

        # Create workspace (unlocked — acquire_workspace will lock it)
        await orch.db.create_workspace(Workspace(
            id="ws-1", project_id="p-1",
            workspace_path=str(workspace),
            source_type=RepoSourceType.CLONE,
        ))

        agent = await orch.db.get_agent("a-1")

        yield {
            "orch": orch,
            "subtask": subtask,
            "agent": agent,
            "workspace": str(workspace),
        }

        await _drain_running_tasks(orch)
        await orch.shutdown()

    async def test_subtask_passes_default_branch_and_rebase_false(self, setup):
        """When rebase_between_subtasks is False, switch_to_branch is called
        with the repo's default_branch and rebase=False."""
        orch = setup["orch"]
        subtask = setup["subtask"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git.aprepare_for_task = AsyncMock()
        orch.git = mock_git

        result = await orch._prepare_workspace(subtask, agent)

        assert result == workspace
        mock_git.aswitch_to_branch.assert_called_once_with(
            workspace, "task/t-parent/parent-plan",
            default_branch="develop",
            rebase=False,
        )
        # aprepare_for_task should NOT be called for subtask branch reuse
        mock_git.aprepare_for_task.assert_not_called()

    async def test_subtask_passes_default_branch_and_rebase_true(self, setup):
        """When rebase_between_subtasks is True, switch_to_branch is called
        with rebase=True so the branch is rebased onto origin/<default_branch>."""
        orch = setup["orch"]
        subtask = setup["subtask"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        # Enable rebase between subtasks
        orch.config.auto_task.rebase_between_subtasks = True

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git.aprepare_for_task = AsyncMock()
        orch.git = mock_git

        result = await orch._prepare_workspace(subtask, agent)

        assert result == workspace
        mock_git.aswitch_to_branch.assert_called_once_with(
            workspace, "task/t-parent/parent-plan",
            default_branch="develop",
            rebase=True,
        )

    async def test_non_subtask_uses_prepare_for_task(self, setup):
        """A non-subtask (is_plan_subtask=False) should use prepare_for_task
        instead of switch_to_branch, regardless of rebase_between_subtasks."""
        orch = setup["orch"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        # Enable rebase — should not affect non-subtask path
        orch.config.auto_task.rebase_between_subtasks = True

        regular_task = Task(
            id="t-regular", project_id="p-1", title="Regular Task",
            description="A normal task", status=TaskStatus.READY,
        )
        await orch.db.create_task(regular_task)

        # Release the lock acquired by the subtask test's _prepare_workspace call
        # (if any), so _prepare_workspace can acquire it for this task
        await orch.db.release_workspaces_for_agent("a-1")

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git.aprepare_for_task = AsyncMock()
        orch.git = mock_git

        result = await orch._prepare_workspace(regular_task, agent)

        assert result == workspace
        mock_git.aswitch_to_branch.assert_not_called()
        mock_git.aprepare_for_task.assert_called_once()
        # Verify default_branch was passed to aprepare_for_task
        call_args = mock_git.aprepare_for_task.call_args
        assert call_args[0][2] == "develop"  # third positional arg is default_branch

    async def test_link_repo_subtask_passes_default_branch(self, tmp_path):
        """For LINK workspaces, switch_to_branch should also receive default_branch."""
        workspace = tmp_path / "linked-repo"
        workspace.mkdir()
        # Initialize a git repo so validate_checkout succeeds
        os.system(f"git init {workspace}")

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            auto_task=AutoTaskConfig(rebase_between_subtasks=True),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(Project(
            id="p-1", name="beta",
            repo_default_branch="trunk",
        ))
        await orch.db.create_agent(Agent(
            id="a-1", name="agent-1", agent_type="claude",
        ))

        parent = Task(
            id="t-parent", project_id="p-1", title="Parent",
            description="Parent", status=TaskStatus.COMPLETED,
            branch_name="task/t-parent/parent",
        )
        await orch.db.create_task(parent)

        subtask = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="Subtask", status=TaskStatus.READY,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(subtask)

        # Create workspace (unlocked — acquire_workspace will lock it)
        await orch.db.create_workspace(Workspace(
            id="ws-link", project_id="p-1",
            workspace_path=str(workspace),
            source_type=RepoSourceType.LINK,
        ))

        agent = await orch.db.get_agent("a-1")

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git.aprepare_for_task = AsyncMock()
        orch.git = mock_git

        await orch._prepare_workspace(subtask, agent)

        mock_git.aswitch_to_branch.assert_called_once_with(
            str(workspace), "task/t-parent/parent",
            default_branch="trunk",
            rebase=True,
        )

        await orch.shutdown()

    async def test_init_repo_subtask_passes_default_branch(self, tmp_path):
        """For CLONE workspaces with custom default_branch, switch_to_branch should receive it."""
        workspace = tmp_path / "workspaces" / "p-1" / "checkout-1"
        workspace.mkdir(parents=True)

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            auto_task=AutoTaskConfig(rebase_between_subtasks=True),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(Project(
            id="p-1", name="gamma",
            repo_default_branch="master",
        ))
        await orch.db.create_agent(Agent(
            id="a-1", name="agent-1", agent_type="claude",
        ))

        parent = Task(
            id="t-parent", project_id="p-1", title="Parent",
            description="Parent", status=TaskStatus.COMPLETED,
            branch_name="task/t-parent/parent",
        )
        await orch.db.create_task(parent)

        subtask = Task(
            id="t-sub-1", project_id="p-1", title="Sub 1",
            description="Subtask", status=TaskStatus.READY,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(subtask)

        # Create workspace (unlocked — acquire_workspace will lock it)
        await orch.db.create_workspace(Workspace(
            id="ws-init", project_id="p-1",
            workspace_path=str(workspace),
            source_type=RepoSourceType.CLONE,
        ))

        agent = await orch.db.get_agent("a-1")

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git.aprepare_for_task = AsyncMock()
        orch.git = mock_git

        await orch._prepare_workspace(subtask, agent)

        mock_git.aswitch_to_branch.assert_called_once_with(
            str(workspace), "task/t-parent/parent",
            default_branch="master",
            rebase=True,
        )

        await orch.shutdown()


class TestMergeAndPushSyncWorkflow:
    """Tests for the orchestrator's _merge_and_push using sync_and_merge,
    including workspace recovery after failures."""

    @pytest.fixture
    async def setup(self, tmp_path):
        workspace = tmp_path / "workspaces" / "p-1" / "checkout-1"
        workspace.mkdir(parents=True)

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(Project(
            id="p-1", name="alpha",
            repo_url="https://github.com/org/myrepo.git",
            repo_default_branch="develop",
        ))
        repo = RepoConfig(
            id="p-1", project_id="p-1",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/org/myrepo.git",
            default_branch="develop",
        )
        await orch.db.create_agent(Agent(
            id="a-1", name="agent-1", agent_type="claude",
        ))

        task = Task(
            id="t-1", project_id="p-1", title="Test Task",
            description="Testing merge and push",
            status=TaskStatus.IN_PROGRESS,
            branch_name="t-1/test-task",
        )
        await orch.db.create_task(task)

        yield {
            "orch": orch,
            "task": task,
            "repo": repo,
            "workspace": str(workspace),
        }

        await _drain_running_tasks(orch)
        await orch.shutdown()

    async def test_merge_and_push_calls_sync_and_merge(self, setup):
        """_merge_and_push should delegate to git.sync_and_merge with the
        correct branch_name and default_branch."""
        orch = setup["orch"]
        task = setup["task"]
        repo = setup["repo"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.async_and_merge = AsyncMock(return_value=(True, ""))
        mock_git.adelete_branch = AsyncMock()
        orch.git = mock_git

        await orch._merge_and_push(task, repo, workspace)

        mock_git.async_and_merge.assert_called_once_with(
            workspace, "t-1/test-task", "develop",
            max_retries=2,
        )

    async def test_merge_and_push_deletes_branch_on_success(self, setup):
        """After successful sync_and_merge, the task branch should be cleaned up."""
        orch = setup["orch"]
        task = setup["task"]
        repo = setup["repo"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.async_and_merge = AsyncMock(return_value=(True, ""))
        mock_git.adelete_branch = AsyncMock()
        orch.git = mock_git

        await orch._merge_and_push(task, repo, workspace)

        mock_git.adelete_branch.assert_called_once_with(
            workspace, "t-1/test-task", delete_remote=True,
        )

    async def test_merge_and_push_recovers_on_merge_conflict(self, setup):
        """On merge_conflict, _merge_and_push should notify and call
        recover_workspace to reset the workspace."""
        orch = setup["orch"]
        task = setup["task"]
        repo = setup["repo"]
        workspace = setup["workspace"]

        notifications = []
        async def capture_notify(msg, project_id=None, embed=None):
            notifications.append(msg)
        orch.set_notify_callback(capture_notify)

        mock_git = MagicMock()
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))
        mock_git.arecover_workspace = AsyncMock()
        mock_git.adelete_branch = AsyncMock()
        orch.git = mock_git

        await orch._merge_and_push(task, repo, workspace)

        # arecover_workspace should have been called
        mock_git.arecover_workspace.assert_called_once_with(workspace, "develop")
        # Should NOT try to delete branch on failure
        mock_git.adelete_branch.assert_not_called()
        # Should have sent a notification
        assert len(notifications) >= 1

    async def test_merge_and_push_recovers_on_push_failure(self, setup):
        """On push failure, _merge_and_push should notify and call
        recover_workspace."""
        orch = setup["orch"]
        task = setup["task"]
        repo = setup["repo"]
        workspace = setup["workspace"]

        notifications = []
        async def capture_notify(msg, project_id=None, embed=None):
            notifications.append(msg)
        orch.set_notify_callback(capture_notify)

        mock_git = MagicMock()
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.async_and_merge = AsyncMock(return_value=(False, "push_failed: error"))
        mock_git.arecover_workspace = AsyncMock()
        mock_git.adelete_branch = AsyncMock()
        orch.git = mock_git

        await orch._merge_and_push(task, repo, workspace)

        mock_git.arecover_workspace.assert_called_once_with(workspace, "develop")
        mock_git.adelete_branch.assert_not_called()
        assert len(notifications) >= 1

    async def test_merge_and_push_tolerates_recover_failure(self, setup):
        """If recover_workspace raises, _merge_and_push should not crash."""
        orch = setup["orch"]
        task = setup["task"]
        repo = setup["repo"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))
        mock_git.arecover_workspace = AsyncMock(side_effect=Exception("git broken"))
        orch.git = mock_git

        # Should not raise
        await orch._merge_and_push(task, repo, workspace)


class TestCompleteWorkspaceMidChainSync:
    """Tests for _complete_workspace calling mid_chain_sync for non-final subtasks."""

    @pytest.fixture
    async def setup(self, tmp_path):
        workspace = tmp_path / "workspaces" / "p-1" / "checkout-1"
        workspace.mkdir(parents=True)

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            auto_task=AutoTaskConfig(rebase_between_subtasks=True),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(Project(
            id="p-1", name="alpha",
            repo_url="https://github.com/org/myrepo.git",
            repo_default_branch="main",
        ))
        await orch.db.create_agent(Agent(
            id="a-1", name="agent-1", agent_type="claude",
        ))

        # Parent task
        parent = Task(
            id="t-parent", project_id="p-1", title="Parent Plan",
            description="Create plan", status=TaskStatus.COMPLETED,
            branch_name="task/t-parent/parent-plan",
        )
        await orch.db.create_task(parent)

        # Two subtasks: first is completing, second is still pending
        sub1 = Task(
            id="t-sub-1", project_id="p-1", title="Step 1",
            description="First subtask", status=TaskStatus.IN_PROGRESS,
            parent_task_id="t-parent", is_plan_subtask=True,
            branch_name="task/t-parent/parent-plan",
        )
        sub2 = Task(
            id="t-sub-2", project_id="p-1", title="Step 2",
            description="Second subtask", status=TaskStatus.DEFINED,
            parent_task_id="t-parent", is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)

        # Create workspace and lock it for sub1 (simulating in-progress task)
        await orch.db.create_workspace(Workspace(
            id="ws-1", project_id="p-1",
            workspace_path=str(workspace),
            source_type=RepoSourceType.CLONE,
        ))
        await orch.db.acquire_workspace("p-1", "a-1", "t-sub-1")

        agent = await orch.db.get_agent("a-1")

        yield {
            "orch": orch,
            "sub1": sub1,
            "sub2": sub2,
            "agent": agent,
            "workspace": str(workspace),
        }

        await _drain_running_tasks(orch)
        await orch.shutdown()

    async def test_non_final_subtask_calls_mid_chain_sync(self, setup):
        """When a non-final subtask completes and rebase_between_subtasks is enabled,
        _complete_workspace should call mid_chain_sync."""
        orch = setup["orch"]
        sub1 = setup["sub1"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.mid_chain_sync.return_value = True
        mock_git.amid_chain_sync = AsyncMock(return_value=True)
        orch.git = mock_git

        result = await orch._complete_workspace(sub1, agent)

        assert result is None  # No PR for non-final subtask
        mock_git.amid_chain_sync.assert_called_once_with(
            workspace, "task/t-parent/parent-plan", "main",
        )

    async def test_non_final_subtask_skips_mid_chain_sync_when_disabled(self, setup):
        """When rebase_between_subtasks is disabled, mid_chain_sync should not be called."""
        orch = setup["orch"]
        sub1 = setup["sub1"]
        agent = setup["agent"]

        orch.config.auto_task.rebase_between_subtasks = False

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.amid_chain_sync = AsyncMock()
        orch.git = mock_git

        result = await orch._complete_workspace(sub1, agent)

        assert result is None
        mock_git.amid_chain_sync.assert_not_called()

    async def test_mid_chain_sync_failure_is_non_fatal(self, setup):
        """If mid_chain_sync raises, _complete_workspace should not crash."""
        orch = setup["orch"]
        sub1 = setup["sub1"]
        agent = setup["agent"]

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.mid_chain_sync.side_effect = Exception("rebase exploded")
        mock_git.amid_chain_sync = AsyncMock(side_effect=Exception("rebase exploded"))
        orch.git = mock_git

        # Should not raise
        result = await orch._complete_workspace(sub1, agent)
        assert result is None

    async def test_final_subtask_does_not_call_mid_chain_sync(self, setup):
        """When the final subtask completes, it should merge/PR, not mid_chain_sync."""
        orch = setup["orch"]
        sub2 = setup["sub2"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        # Mark sub1 as completed so sub2 is the last
        await orch.db.update_task("t-sub-1", status=TaskStatus.COMPLETED.value)
        await orch.db.update_task("t-sub-2", status=TaskStatus.IN_PROGRESS.value,
                                  branch_name="task/t-parent/parent-plan")
        # Re-lock workspace for sub2
        await orch.db.release_workspace("ws-1")
        await orch.db.acquire_workspace("p-1", "a-1", "t-sub-2")
        sub2_updated = await orch.db.get_task("t-sub-2")

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.sync_and_merge.return_value = (True, "")
        mock_git.async_and_merge = AsyncMock(return_value=(True, ""))
        mock_git.adelete_branch = AsyncMock()
        mock_git.amid_chain_sync = AsyncMock()
        orch.git = mock_git

        result = await orch._complete_workspace(sub2_updated, agent)

        # Should NOT call amid_chain_sync
        mock_git.amid_chain_sync.assert_not_called()
        # Should call async_and_merge (merge+push for last subtask)
        mock_git.async_and_merge.assert_called_once()

    async def test_final_subtask_with_approval_creates_pr(self, setup):
        """When the final subtask completes and parent requires approval,
        a PR should be created instead of merging directly."""
        orch = setup["orch"]
        sub2 = setup["sub2"]
        agent = setup["agent"]

        # Mark parent as requiring approval
        await orch.db.update_task("t-parent", requires_approval=True)
        # Mark sub1 as completed so sub2 is the last
        await orch.db.update_task("t-sub-1", status=TaskStatus.COMPLETED.value)
        await orch.db.update_task("t-sub-2", status=TaskStatus.IN_PROGRESS.value,
                                  branch_name="task/t-parent/parent-plan")
        # Re-lock workspace for sub2
        await orch.db.release_workspace("ws-1")
        await orch.db.acquire_workspace("p-1", "a-1", "t-sub-2")
        sub2_updated = await orch.db.get_task("t-sub-2")

        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.create_pr.return_value = "https://github.com/org/repo/pull/42"
        mock_git.acreate_pr = AsyncMock(return_value="https://github.com/org/repo/pull/42")
        mock_git.apush_branch = AsyncMock()
        mock_git.amid_chain_sync = AsyncMock()
        mock_git.async_and_merge = AsyncMock()
        orch.git = mock_git

        result = await orch._complete_workspace(sub2_updated, agent)

        assert result == "https://github.com/org/repo/pull/42"
        mock_git.apush_branch.assert_called_once()
        mock_git.acreate_pr.assert_called_once()
        mock_git.amid_chain_sync.assert_not_called()
        mock_git.async_and_merge.assert_not_called()


# ── Completion Pipeline Tests ──────────────────────────────────────────


class TestCompletionPipeline:
    """Tests for the completion pipeline infrastructure."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with mocked git for pipeline tests."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        o = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await o.initialize()

        # Set up project, workspace, agent
        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(Workspace(
            id="ws-1", project_id="p-1",
            workspace_path=ws_path,
            source_type=RepoSourceType.LINK,
        ))
        await o.db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))

        # Mock git
        mock_git = MagicMock()
        mock_git.validate_checkout.return_value = True
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.commit_all.return_value = True
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.has_remote.return_value = True
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.sync_and_merge.return_value = (True, "")
        mock_git.async_and_merge = AsyncMock(return_value=(True, ""))
        mock_git.delete_branch.return_value = None
        mock_git.adelete_branch = AsyncMock(return_value=None)
        mock_git.arecover_workspace = AsyncMock()
        mock_git.ahas_non_plan_changes = AsyncMock(return_value=False)
        mock_git.amid_chain_sync = AsyncMock()
        mock_git.apush_branch = AsyncMock()
        mock_git.acreate_pr = AsyncMock()
        o.git = mock_git

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_pipeline_phases_run_in_order(self, pipeline_orch):
        """Pipeline should run commit → merge → plan_generate in order."""
        orch = pipeline_orch
        from src.models import PipelineContext, PhaseResult

        task = Task(id="t-1", project_id="p-1", title="Test",
                    description="test", branch_name="feature-1",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-1")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-1")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is True
        assert pr_url is None
        # Commit should have been called
        orch.git.acommit_all.assert_called_once()
        # Merge should have been called
        orch.git.async_and_merge.assert_called_once()

    async def test_pipeline_stops_on_phase_failure(self, pipeline_orch):
        """When merge phase returns STOP, pipeline should stop."""
        orch = pipeline_orch
        from src.models import PipelineContext, PhaseResult

        # Make merge fail
        orch.git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))

        task = Task(id="t-2", project_id="p-1", title="Test",
                    description="test", branch_name="feature-2",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-2")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-2")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is False

    async def test_pipeline_error_handling(self, pipeline_orch):
        """Phase that raises should not crash pipeline."""
        orch = pipeline_orch
        from src.models import PipelineContext

        # Make commit_all raise
        orch.git.acommit_all = AsyncMock(side_effect=RuntimeError("git broken"))

        task = Task(id="t-3", project_id="p-1", title="Test",
                    description="test", branch_name="feature-3",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-3")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-3")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is False  # should not crash

    async def test_merge_failure_keeps_task_in_progress(self, pipeline_orch):
        """Merge failure should leave task in IN_PROGRESS status (pipeline doesn't change status)."""
        orch = pipeline_orch
        from src.models import PipelineContext

        orch.git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))

        task = Task(id="t-4", project_id="p-1", title="Test",
                    description="test", branch_name="feature-4",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-4")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-4")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is False

        # Task should still be IN_PROGRESS (pipeline doesn't change status)
        t = await orch.db.get_task("t-4")
        assert t.status == TaskStatus.IN_PROGRESS

    async def test_merge_success_returns_continue(self, pipeline_orch):
        """Successful merge should return completed_ok=True."""
        orch = pipeline_orch
        from src.models import PipelineContext

        task = Task(id="t-5", project_id="p-1", title="Test",
                    description="test", branch_name="feature-5",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-5")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-5")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is True

    async def test_merge_failure_sets_preferred_workspace(self, pipeline_orch):
        """Merge failure should set preferred_workspace_id on the task."""
        orch = pipeline_orch
        from src.models import PipelineContext

        orch.git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))

        task = Task(id="t-6", project_id="p-1", title="Test",
                    description="test", branch_name="feature-6",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-6")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-6")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        await orch._run_completion_pipeline(ctx)

        t = await orch.db.get_task("t-6")
        assert t.preferred_workspace_id == ws.id

    async def test_merge_failure_emits_event(self, pipeline_orch):
        """Merge failure should emit task.merge_failed on EventBus."""
        orch = pipeline_orch
        from src.models import PipelineContext

        orch.git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))

        events = []
        orch.bus.subscribe("task.merge_failed", lambda data: events.append(data))

        task = Task(id="t-7", project_id="p-1", title="Test",
                    description="test", branch_name="feature-7",
                    status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-7")

        output = AgentOutput(result=AgentResult.COMPLETED, tokens_used=100)
        ws = await orch.db.get_workspace_for_task("t-7")

        ctx = PipelineContext(
            task=task, agent=Agent(id="a-1", name="claude-1", agent_type="claude"),
            output=output,
            workspace_path=ws.workspace_path, workspace_id=ws.id,
            repo=RepoConfig(id="r-1", project_id="p-1",
                            source_type=RepoSourceType.LINK, default_branch="main"),
            default_branch="main",
        )

        await orch._run_completion_pipeline(ctx)
        await asyncio.sleep(0.1)  # let async event propagate

        assert len(events) >= 1
        assert events[0]["task_id"] == "t-7"
        assert events[0]["error"] == "merge_conflict"


# ── Workspace Affinity for Plan Subtasks ───────────────────────────────
