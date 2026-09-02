import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.orchestrator import Orchestrator
from src.models import (
    AgentProfile,
    Project,
    Task,
    Agent,
    TaskStatus,
    AgentResult,
    AgentOutput,
    RepoConfig,
    RepoSourceType,
    Workspace,
)
from src.runtimes.base import Runtime
from src.config import AppConfig, AutoTaskConfig
from tests.assignment_routing_helpers import install_already_routed


class MockAdapter(Runtime):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000, on_wait=None):
        self._result = result
        self._tokens = tokens
        self._on_wait = on_wait
        self._ctx = None

    async def start(self, task):
        self._ctx = task  # TaskContext

    async def wait(self, on_message=None):
        if self._on_wait:
            self._on_wait(self._ctx)
        return AgentOutput(result=self._result, summary="Done", tokens_used=self._tokens)

    async def stop(self):
        pass

    async def is_alive(self):
        return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000, on_wait=None):
        self.result = result
        self.tokens = tokens
        self.on_wait = on_wait
        self.last_profile = None
        self.create_calls = []

    def create(self, agent_type: str, profile=None, llm_logger=None) -> Runtime:
        self.last_profile = profile
        self.create_calls.append({"agent_type": agent_type, "profile": profile})
        return MockAdapter(result=self.result, tokens=self.tokens, on_wait=self.on_wait)


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
        data_dir=str(tmp_path / "data"),
    )
    # These tests predate the worktrees P6 flag flip and use MockAdapter
    # git plus LINK paths that are not real git repos.  Keep the legacy
    # exclusive-clone provisioning path here; worktree-specific tests
    # live in test_worktree_prepare.py / test_worktree_reaper.py /
    # test_merge_slot.py.
    config.worktrees.enabled = False
    o = Orchestrator(config, runtimes=MockAdapterFactory())
    await o.initialize()
    install_already_routed(o)
    yield o
    # Drain any remaining background tasks before closing DB
    await _drain_running_tasks(o)
    await o.shutdown()


async def _create_project_with_workspace(
    db,
    project_id: str = "p-1",
    name: str = "alpha",
    workspace_path: str = "/tmp/test-workspace",
) -> None:
    """Create a project and an associated workspace so task execution succeeds."""
    await db.create_project(Project(id=project_id, name=name))
    await db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=RepoSourceType.LINK,
        )
    )


async def _run_cycle_and_wait(orch):
    """Run one scheduling cycle and wait for all background task executions."""
    await orch.run_one_cycle()
    await orch.wait_for_running_tasks()



async def test_conditional_completion_cascades_contingency_to_noop_and_emits_event(
    orchestrator_factory,
):
    orch = await orchestrator_factory()
    try:
        await orch.db.create_project(Project(id="conditional", name="conditional"))
        await orch.db.create_task(
            Task(
                id="done",
                project_id="conditional",
                title="done",
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
        await orch.db.create_task(
            Task(
                id="contingency",
                project_id="conditional",
                title="contingency",
                description="",
                status=TaskStatus.DEFINED,
            )
        )
        await orch.db.add_dependency("contingency", "done", "conditional-blocks")
        await orch._close_dead_conditional_tasks()
        assert (await orch.db.get_task("contingency")).status == TaskStatus.COMPLETED
        events = await orch.db.get_recent_events(task_id="contingency")
        assert any(event["event_type"] == "task.skipped_conditional" for event in events)
    finally:
        await orch.db.close()


async def test_conditional_autoclose_disabled_leaves_contingency_defined(orchestrator_factory):
    orch = await orchestrator_factory()
    try:
        orch.config.work_graph.conditional_autoclose = False
        await orch.db.create_project(Project(id="conditional", name="conditional"))
        await orch.db.create_task(
            Task(
                id="done",
                project_id="conditional",
                title="done",
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
        await orch.db.create_task(
            Task(
                id="contingency",
                project_id="conditional",
                title="contingency",
                description="",
                status=TaskStatus.DEFINED,
            )
        )
        await orch.db.add_dependency("contingency", "done", "conditional-blocks")
        await orch._close_dead_conditional_tasks()
        assert (await orch.db.get_task("contingency")).status == TaskStatus.DEFINED
    finally:
        await orch.db.close()


async def test_child_completion_settles_all_terminal_container_ancestors_once(
    orchestrator_factory,
):
    """Spec §7: one leaf completion settles every eligible container ancestor
    in the same transition, each exactly once — the sweep backstop then finds
    nothing left, and a container with an open child is never touched."""
    orch = await orchestrator_factory()
    try:
        orch.register_settlement_listener()
        await orch.db.create_project(Project(id="p-settle", name="p-settle"))

        async def mktask(tid, status):
            await orch.db.create_task(
                Task(
                    id=tid,
                    project_id="p-settle",
                    title=tid,
                    description="",
                    status=status,
                )
            )

        # grand ── mid ── {last-leaf (IN_PROGRESS), done-sib (COMPLETED)}
        #      └── side-sib (COMPLETED)
        await mktask("grand", TaskStatus.IN_PROGRESS)
        await mktask("mid", TaskStatus.IN_PROGRESS)
        await mktask("last-leaf", TaskStatus.IN_PROGRESS)
        await mktask("done-sib", TaskStatus.COMPLETED)
        await mktask("side-sib", TaskStatus.COMPLETED)
        # Open edges first so no ancestor settles during setup.
        await orch.db.add_dependency("mid", "grand", "parent-child")
        await orch.db.add_dependency("last-leaf", "mid", "parent-child")
        await orch.db.add_dependency("done-sib", "mid", "parent-child")
        await orch.db.add_dependency("side-sib", "grand", "parent-child")
        # A container that still has an open child must never settle.
        await mktask("open-container", TaskStatus.IN_PROGRESS)
        await mktask("open-child", TaskStatus.READY)
        await orch.db.add_dependency("open-child", "open-container", "parent-child")

        def completed_events(task_id):
            return [
                call
                for call in orch.bus.emit.await_args_list
                if call.args[0] == "task.completed" and call.args[1]["task_id"] == task_id
            ]

        # The normal transition path: completing the last open leaf.
        await orch.db.transition_task("last-leaf", TaskStatus.COMPLETED, context="test")

        assert (await orch.db.get_task("mid")).status == TaskStatus.COMPLETED
        assert (await orch.db.get_task("grand")).status == TaskStatus.COMPLETED
        assert (await orch.db.get_task("open-container")).status == TaskStatus.IN_PROGRESS
        assert len(completed_events("mid")) == 1
        assert len(completed_events("grand")) == 1
        assert completed_events("open-container") == []

        # The low-cadence sweep backstop finds nothing left to settle and
        # emits no duplicate completion events.
        orch._last_container_sweep = 0.0
        assert await orch.db.settle_candidates() == []
        await orch._sweep_container_completion()
        assert len(completed_events("mid")) == 1
        assert len(completed_events("grand")) == 1
        assert (await orch.db.get_task("open-container")).status == TaskStatus.IN_PROGRESS
    finally:
        await orch.db.close()


class TestOrchestratorLifecycle:
    async def test_full_task_lifecycle(self, orch):
        """DEFINED → READY → ASSIGNED → IN_PROGRESS → COMPLETED"""
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Do it",
                status=TaskStatus.READY,
            )
        )

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

    async def test_failed_task_retries(self, orch):
        orch._runtimes = MockAdapterFactory(result=AgentResult.FAILED)
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Do it",
                status=TaskStatus.READY,
                max_retries=2,
            )
        )

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        # Should be READY for retry (failed once, max 2)
        assert task.status == TaskStatus.READY
        assert task.retry_count == 1

    async def test_paused_on_token_exhaustion(self, orch):
        orch._runtimes = MockAdapterFactory(result=AgentResult.PAUSED_TOKENS)
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Do it",
                status=TaskStatus.READY,
            )
        )

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.PAUSED
        assert task.resume_after is not None

    async def test_dependencies_block_scheduling(self, orch):
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="First",
                description="Do first",
                status=TaskStatus.DEFINED,
            )
        )
        await orch.db.create_task(
            Task(
                id="t-2",
                project_id="p-1",
                title="Second",
                description="Do second",
                status=TaskStatus.DEFINED,
            )
        )
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


class TestRecoverStaleState:
    """Restart recovery must not resurrect container tasks.

    A plan parent or graph parent is IN_PROGRESS *because* its children are
    running.  Resetting one to READY makes the scheduler dispatch it: it takes
    the project's exclusive `project-repo` lock and launches an agent on a
    prompt whose whole content is the parent title, blocking its own children.
    A graph parent is IN_PROGRESS for the graph's entire lifetime, so the
    window is days.
    """

    async def test_container_task_with_subtasks_is_left_in_progress(self, orch):
        # A project only, no workspace: these tests never schedule anything,
        # and `_create_project_with_workspace` hands every caller the same
        # hard-coded `/tmp/test-workspace`, which races under `-n auto`.
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_task(
            Task(
                id="parent-1",
                project_id="p-1",
                title="Messages table + delivery engine",
                description="Messages table + delivery engine",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await orch.db.create_task(
            Task(
                id="child-1",
                project_id="p-1",
                title="Schema",
                description="Schema",
                status=TaskStatus.IN_PROGRESS,
                parent_task_id="parent-1",
            )
        )

        await orch._recover_stale_state()

        parent = await orch.db.get_task("parent-1")
        child = await orch.db.get_task("child-1")
        assert parent.status == TaskStatus.IN_PROGRESS
        # The leaf is genuinely stale and must still be recovered.
        assert child.status == TaskStatus.READY

    async def test_a_plain_stale_task_is_still_recovered(self, orch):
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-dead", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Do it",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="a-dead",
            )
        )

        await orch._recover_stale_state()

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.READY
        assert task.assigned_agent_id is None

    async def test_container_task_still_auto_completes(self, orch):
        """Event-driven settlement (spec §7) keys off the container flag, so
        the parent left IN_PROGRESS by recovery is not stranded — it settles
        the instant its last child transitions to COMPLETED."""
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_task(
            Task(
                id="parent-1",
                project_id="p-1",
                title="Parent",
                description="Parent",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await orch.db.create_task(
            Task(
                id="child-1",
                project_id="p-1",
                title="Child",
                description="Child",
                status=TaskStatus.READY,
            )
        )
        await orch.db.add_dependency("child-1", "parent-1", "parent-child")

        await orch._recover_stale_state()
        await orch.db.transition_task("child-1", TaskStatus.COMPLETED, context="test")

        assert (await orch.db.get_task("parent-1")).status == TaskStatus.COMPLETED


def _make_plan_toucher(workspace):
    """Create an on_wait callback that touches pre-created plan files.

    Tests pre-create plan files before the orchestration cycle to simulate
    agent-written plans.  This callback runs during adapter.wait() to
    refresh the mtime, simulating the agent writing the file during
    execution.
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


class TestAgentReconcilerWiring:
    """Regression: ensures the AgentReconciler runs at the top of each
    scheduling tick so READY tasks dispatch without manual `aq agent create`.
    See docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §7.
    """

    async def test_ready_task_dispatches_with_only_workspace_and_default_profile(self, orch):
        """The original quick-ember bug: project with workspace +
        default_profile_id + READY task should dispatch within one cycle —
        no manual agent creation. Tests the full reconciler → scheduler →
        executor chain.
        """
        # Profile must exist before project references it.
        await orch.db.create_profile(AgentProfile(id="claude", name="claude", harness="claude"))
        # Project with default_profile_id and a workspace.
        await orch.db.create_project(Project(id="p-1", name="alpha", default_profile_id="claude"))
        await orch.db.create_workspace(
            Workspace(
                id="ws-p-1",
                project_id="p-1",
                workspace_path=str(orch.config.workspace_dir + "/p-1"),
                source_type=RepoSourceType.LINK,
            )
        )
        # READY task with no profile_id (falls back to project default).
        await orch.db.create_task(
            Task(
                id="regression-task",
                project_id="p-1",
                title="Test reconciler dispatch",
                description="Should auto-dispatch via the reconciler.",
                status=TaskStatus.READY,
            )
        )

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("regression-task")
        # Real dispatch path may pass through ASSIGNED → IN_PROGRESS;
        # accept any non-READY state.
        assert task.status != TaskStatus.READY
        agents = await orch.db.list_agents()
        assert len(agents) >= 1



class TestPlanApprovalBlocking:
    """Tests that plan subtasks are NOT promoted until the plan is approved."""

    @pytest.fixture
    async def orch_with_workspace(self, tmp_path):
        workspace = tmp_path / "workspaces"
        workspace.mkdir()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
            data_dir=str(tmp_path / "data"),
        )
        config.auto_task = AutoTaskConfig()
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()
        yield o, workspace
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_subtasks_blocked_while_parent_unreleased(self, orch_with_workspace):
        """Plan subtasks must stay DEFINED while the parent container is DEFINED."""
        orch, workspace = orch_with_workspace

        await _create_project_with_workspace(orch.db, workspace_path=str(workspace))

        # Create parent task still unreleased (DEFINED)
        parent = Task(
            id="t-plan",
            project_id="p-1",
            title="Plan Task",
            description="Create plan",
            status=TaskStatus.DEFINED,
        )
        await orch.db.create_task(parent)

        # Create subtasks that would normally be promoted
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Sub 1",
            description="First subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-plan",
            is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Sub 2",
            description="Second subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-plan",
            is_plan_subtask=True,
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

    async def test_subtasks_promoted_after_parent_released(self, orch_with_workspace):
        """After parent transitions to IN_PROGRESS, first subtask gets promoted."""
        orch, workspace = orch_with_workspace

        await _create_project_with_workspace(orch.db, workspace_path=str(workspace))

        # Create parent still unreleased (DEFINED)
        parent = Task(
            id="t-plan",
            project_id="p-1",
            title="Plan Task",
            description="Create plan",
            status=TaskStatus.DEFINED,
        )
        await orch.db.create_task(parent)

        # Create chained subtasks with blocking dep on parent
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Sub 1",
            description="First subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-plan",
            is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Sub 2",
            description="Second subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-plan",
            is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        await orch.db.add_dependency("t-sub-1", depends_on="t-plan")
        await orch.db.add_dependency("t-sub-2", depends_on="t-sub-1")

        # Release the container: transition parent to IN_PROGRESS
        await orch.db.transition_task("t-plan", TaskStatus.IN_PROGRESS, context="released")

        plan = await orch.db.get_task("t-plan")
        assert plan.status == TaskStatus.IN_PROGRESS

        # Now run _check_defined_tasks — first subtask should promote
        await orch._check_defined_tasks()

        s1 = await orch.db.get_task("t-sub-1")
        s2 = await orch.db.get_task("t-sub-2")
        assert s1.status == TaskStatus.READY, "Sub 1 should be READY after release"
        assert s2.status == TaskStatus.DEFINED, "Sub 2 should stay DEFINED (deps not met)"

    async def test_plan_parent_auto_completes_when_subtasks_done(self, orch_with_workspace):
        """Plan parent settles to COMPLETED the instant its last subtask
        transitions to COMPLETED (event-driven settlement, spec §7) — no
        per-cycle scan involved."""
        orch, workspace = orch_with_workspace

        await _create_project_with_workspace(orch.db, workspace_path=str(workspace))

        # Create parent in IN_PROGRESS (plan approved)
        parent = Task(
            id="t-plan",
            project_id="p-1",
            title="Plan Task",
            description="Create plan",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(parent)

        # Create subtasks, linked as real parent-child edges so the container
        # flag is set and settlement can see them.
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Sub 1",
            description="First subtask",
            status=TaskStatus.READY,
            is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Sub 2",
            description="Second subtask",
            status=TaskStatus.READY,
            is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        await orch.db.add_dependency("t-sub-1", "t-plan", "parent-child")
        await orch.db.add_dependency("t-sub-2", "t-plan", "parent-child")

        # Complete the first subtask — parent should stay IN_PROGRESS.
        await orch.db.transition_task("t-sub-1", TaskStatus.COMPLETED, context="test")
        plan = await orch.db.get_task("t-plan")
        assert plan.status == TaskStatus.IN_PROGRESS, "Parent should stay IN_PROGRESS"

        # Complete the last subtask — parent auto-completes in the same call.
        orch._emit_text_notify = AsyncMock()
        await orch.db.transition_task("t-sub-2", TaskStatus.COMPLETED, context="test")
        plan = await orch.db.get_task("t-plan")
        assert plan.status == TaskStatus.COMPLETED, "Parent should auto-complete"


class TestIsLastSubtask:
    """Tests for the _is_last_subtask helper."""

    @pytest.fixture
    async def orch_with_workspace(self, tmp_path):
        workspace = tmp_path / "workspaces"
        workspace.mkdir()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()
        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_single_subtask_is_last(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(
            Task(
                id="t-parent",
                project_id="p-1",
                title="Parent",
                description="Parent task",
                status=TaskStatus.COMPLETED,
            )
        )
        sub = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Only Sub",
            description="The only subtask",
            status=TaskStatus.COMPLETED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        await orch.db.create_task(sub)
        assert await orch._is_last_subtask(sub) is True

    async def test_not_last_when_sibling_incomplete(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(
            Task(
                id="t-parent",
                project_id="p-1",
                title="Parent",
                description="Parent task",
                status=TaskStatus.COMPLETED,
            )
        )
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Sub 1",
            description="First subtask",
            status=TaskStatus.COMPLETED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Sub 2",
            description="Second subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        assert await orch._is_last_subtask(sub1) is False

    async def test_is_last_when_all_siblings_completed(self, orch_with_workspace):
        orch = orch_with_workspace
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(
            Task(
                id="t-parent",
                project_id="p-1",
                title="Parent",
                description="Parent task",
                status=TaskStatus.COMPLETED,
            )
        )
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Sub 1",
            description="First subtask",
            status=TaskStatus.COMPLETED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Sub 2",
            description="Second subtask",
            status=TaskStatus.COMPLETED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        await orch.db.create_task(sub1)
        await orch.db.create_task(sub2)
        assert await orch._is_last_subtask(sub2) is True


class TestPrepareWorkspaceCleanDefault:
    """Tests for _prepare_workspace ensuring clean default branch via fetch/checkout/reset."""

    @pytest.fixture
    async def setup(self, tmp_path):
        """Create orchestrator, project, workspace, agent, and a task.

        Returns a dict with all objects needed for _prepare_workspace tests.
        """
        workspace = tmp_path / "workspaces" / "p-1" / "checkout-1"
        workspace.mkdir(parents=True)

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        # Legacy clone-mode tests — worktrees P6 default flipped to True.
        config.worktrees.enabled = False
        orch = Orchestrator(config, runtimes=MockAdapterFactory())
        await orch.initialize()

        await orch.db.create_project(
            Project(
                id="p-1",
                name="alpha",
                repo_url="https://github.com/org/myrepo.git",
                repo_default_branch="develop",
            )
        )
        await orch.db.create_agent(
            Agent(
                id="a-1",
                name="agent-1",
                profile_id="claude",
            )
        )

        task = Task(
            id="t-1",
            project_id="p-1",
            title="Regular Task",
            description="A normal task",
            status=TaskStatus.READY,
        )
        await orch.db.create_task(task)

        await orch.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=str(workspace),
                source_type=RepoSourceType.CLONE,
            )
        )

        agent = await orch.db.get_agent("a-1")

        yield {
            "orch": orch,
            "task": task,
            "agent": agent,
            "workspace": str(workspace),
        }

        await _drain_running_tasks(orch)
        await orch.shutdown()

    async def test_clone_validates_fetches_checkouts_resets(self, setup):
        """For CLONE workspace: validates checkout, fetches origin, checks out
        default branch, and resets to origin/default."""
        orch = setup["orch"]
        task = setup["task"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        orch.git = mock_git

        result = await orch._prepare_workspace(task, agent)

        assert result == workspace
        # Should validate checkout
        mock_git.avalidate_checkout.assert_called()
        # Should fetch origin, checkout default, and hard-reset
        calls = [str(c) for c in mock_git._arun.call_args_list]
        fetch_called = any("fetch" in c and "origin" in c for c in calls)
        checkout_called = any("checkout" in c and "develop" in c for c in calls)
        reset_called = any("reset" in c and "origin/develop" in c for c in calls)
        assert fetch_called, f"Expected fetch origin call, got: {calls}"
        assert checkout_called, f"Expected checkout develop call, got: {calls}"
        assert reset_called, f"Expected reset --hard origin/develop call, got: {calls}"

    async def test_does_not_call_aprepare_for_task_or_aswitch_to_branch(self, setup):
        """_prepare_workspace should NOT call aprepare_for_task or aswitch_to_branch."""
        orch = setup["orch"]
        task = setup["task"]
        agent = setup["agent"]

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.aprepare_for_task = AsyncMock()
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git._arun = AsyncMock(return_value="")
        orch.git = mock_git

        await orch._prepare_workspace(task, agent)

        mock_git.aprepare_for_task.assert_not_called()
        mock_git.aswitch_to_branch.assert_not_called()

    async def test_returns_workspace_path_and_sets_branch_name(self, setup):
        """_prepare_workspace returns the workspace path and sets branch_name on the task."""
        orch = setup["orch"]
        task = setup["task"]
        agent = setup["agent"]
        workspace = setup["workspace"]

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        orch.git = mock_git

        result = await orch._prepare_workspace(task, agent)

        assert result == workspace
        # branch_name should be set on the task in the DB
        updated = await orch.db.get_task("t-1")
        assert updated.branch_name is not None
        assert len(updated.branch_name) > 0


class TestPhaseVerifyNormalTask:
    """Tests for _phase_verify with a normal task (no approval, not a subtask)."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with mocked git for verification tests."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        # These tests cover the exclusive-clone verify path (auto-merge
        # remediations); with worktrees enabled (the default since P6) the
        # project-repo kind routes to _phase_integrate instead.
        config.worktrees.enabled = False
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await o.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="main")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value=None)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_branch = AsyncMock(return_value=None)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        o.git = mock_git

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    def _make_ctx(self, orch, task, ws_path):
        from src.models import PipelineContext

        return PipelineContext(
            task=task,
            agent=Agent(id="a-1", name="claude-1", profile_id="claude"),
            output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=100),
            workspace_path=ws_path,
            workspace_id="ws-1",
            repo=RepoConfig(
                id="r-1", project_id="p-1", source_type=RepoSourceType.LINK, default_branch="main"
            ),
            default_branch="main",
        )

    async def test_nonzero_exit_code_auto_remediates(self, pipeline_orch):
        """Non-zero exit code skips verification but still auto-remediates dirty workspace."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-exit",
            project_id="p-1",
            title="Test exit",
            description="test",
            branch_name="feature-exit",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Agent left uncommitted changes and exited with error
        orch.git.ahas_uncommitted_changes = AsyncMock(side_effect=[True, False])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)
        ctx.output.exit_code = 1  # Non-zero exit code

        result = await orch._phase_verify(ctx)
        # Should still CONTINUE (skip verification) but auto-remediate
        assert result == PhaseResult.CONTINUE
        # Should have attempted to commit the uncommitted changes
        orch.git.acommit_all.assert_awaited_once()

    async def test_nonzero_exit_code_skips_when_clean(self, pipeline_orch):
        """Non-zero exit code with clean workspace skips without remediation."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-exit2",
            project_id="p-1",
            title="Test exit clean",
            description="test",
            branch_name="feature-exit2",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Workspace is clean
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)
        ctx.output.exit_code = 1  # Non-zero exit code

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        # No commit attempt because workspace is clean
        orch.git.acommit_all.assert_not_awaited()

    async def test_passes_on_default_branch_clean_synced(self, pipeline_orch):
        """Normal task passes when on default branch, no uncommitted, synced."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-1",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE

    async def test_auto_merges_when_on_task_branch(self, pipeline_orch):
        """Normal task auto-merges task branch to default when agent forgot to merge."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Agent left workspace on task branch instead of default
        orch.git.aget_current_branch = AsyncMock(return_value="feature-2")

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Auto-merge should handle the branch switch + merge automatically
        assert result == PhaseResult.CONTINUE
        # Verify that checkout and merge were called
        calls = [str(c) for c in orch.git._arun.call_args_list]
        assert any("checkout" in c and "main" in c for c in calls)
        assert any("merge" in c and "feature-2" in c for c in calls)

    async def test_fails_when_auto_merge_fails(self, pipeline_orch):
        """Falls back to failure when auto-merge raises an exception (e.g. conflict)."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-2b",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2b",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Agent left workspace on task branch instead of default
        orch.git.aget_current_branch = AsyncMock(return_value="feature-2b")

        # Checkout default succeeds, but merge fails (conflict)
        async def mock_arun(args, cwd=None):
            if args[0] == "merge":
                raise Exception("merge conflict")
            if args[0] == "checkout" and args[1] == "feature-2b":
                return ""  # Recovery checkout back to task branch
            return "0"

        orch.git._arun = AsyncMock(side_effect=mock_arun)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Auto-merge failed, should fall through to verification failure
        assert result == PhaseResult.STOP

    async def test_auto_commits_uncommitted_changes(self, pipeline_orch):
        """Uncommitted changes on default branch are auto-committed."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-3",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # First call returns True (initial check), second returns False (re-check after commit)
        orch.git.ahas_uncommitted_changes = AsyncMock(side_effect=[True, False])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Auto-commit should fix the uncommitted changes
        assert result == PhaseResult.CONTINUE
        orch.git.acommit_all.assert_awaited_once()

    async def test_fails_when_all_remediation_fails(self, pipeline_orch):
        """Falls back to failure when all auto-remediation attempts fail."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-3b",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3b",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))
        # Force-clean also fails to clean the workspace
        orch.git.aforce_clean_workspace = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP

    async def test_force_cleans_when_commit_fails(self, pipeline_orch):
        """Force-clean recovers the workspace when auto-commit fails."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-3b2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3b2",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))
        # Force-clean succeeds — workspace is clean after reset+clean
        orch.git.aforce_clean_workspace = AsyncMock(return_value=True)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Force-clean should have recovered the workspace
        assert result == PhaseResult.CONTINUE
        # force_clean may be called more than once (initial + final safety-net sweep)
        assert orch.git.aforce_clean_workspace.await_count >= 1

    async def test_auto_commit_and_merge_when_on_task_branch(self, pipeline_orch):
        """Uncommitted changes on task branch are auto-committed, then auto-merged."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-3c",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3c",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Agent left uncommitted changes on task branch
        orch.git.aget_current_branch = AsyncMock(return_value="feature-3c")
        # First call True (initial), second False (after auto-commit)
        orch.git.ahas_uncommitted_changes = AsyncMock(side_effect=[True, False])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Auto-commit cleans up changes, then auto-merge merges to default
        assert result == PhaseResult.CONTINUE
        orch.git.acommit_all.assert_awaited_once()
        # Verify merge happened
        calls = [str(c) for c in orch.git._arun.call_args_list]
        assert any("merge" in c and "feature-3c" in c for c in calls)

    async def test_auto_pushes_unpushed_commits(self, pipeline_orch):
        """Unpushed commits on default branch are auto-pushed."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-4",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-4",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Auto-push rev-list returns "3" (ahead), then scenario behind
        # check returns "0", then scenario ahead check returns "0"
        # (pushed successfully — mock won't change state but verification
        # re-checks via _arun which we feed with subsequent values).
        orch.git._arun = AsyncMock(side_effect=["3", "0", "0"])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        orch.git.apush_branch.assert_awaited_once()

    async def test_fails_when_ahead_and_auto_push_fails(self, pipeline_orch):
        """Falls back to failure when auto-push raises an exception."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-4b",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-4b",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Auto-push rev-list returns "3" (ahead) — triggers push
        # Push fails, so scenario behind check gets "0", ahead check gets "3"
        orch.git._arun = AsyncMock(side_effect=["3", "0", "3"])
        orch.git.apush_branch = AsyncMock(side_effect=Exception("push failed"))

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP


class TestPhaseVerifyApprovalTask:
    """Tests for _phase_verify with pull_request-mode tasks."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with mocked git for approval verification tests."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await o.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="feature-1")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/42")
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_branch = AsyncMock(return_value=None)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        o.git = mock_git

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    def _make_ctx(self, orch, task, ws_path):
        from src.models import PipelineContext

        return PipelineContext(
            task=task,
            agent=Agent(id="a-1", name="claude-1", profile_id="claude"),
            output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=100),
            workspace_path=ws_path,
            workspace_id="ws-1",
            repo=RepoConfig(
                id="r-1", project_id="p-1", source_type=RepoSourceType.LINK, default_branch="main"
            ),
            default_branch="main",
        )

    async def test_passes_on_task_branch_with_pr(self, pipeline_orch):
        """Approval task passes when on task branch and PR is found; ctx.pr_url is set."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-1",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="pull_request",
        )
        await orch.db.create_task(task)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        assert ctx.pr_url == "https://github.com/org/repo/pull/42"

    async def test_fails_when_no_pr_found(self, pipeline_orch):
        """Approval task fails when no PR is found for the branch."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="pull_request",
        )
        await orch.db.create_task(task)

        # On task branch but no PR
        orch.git.aget_current_branch = AsyncMock(return_value="feature-2")
        orch.git.afind_open_pr = AsyncMock(return_value=None)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP


class TestPhaseVerifyIntermediateSubtask:
    """Tests for _phase_verify with intermediate (non-final) subtasks."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with parent + 2 subtasks for intermediate verification."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await o.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))

        # Parent task
        parent = Task(
            id="t-parent",
            project_id="p-1",
            title="Parent Plan",
            description="Plan",
            status=TaskStatus.COMPLETED,
            branch_name="task/t-parent/parent-plan",
        )
        await o.db.create_task(parent)

        # Two subtasks: sub1 is completing (intermediate), sub2 is pending
        sub1 = Task(
            id="t-sub-1",
            project_id="p-1",
            title="Step 1",
            description="First subtask",
            status=TaskStatus.IN_PROGRESS,
            parent_task_id="t-parent",
            is_plan_subtask=True,
            branch_name="task/t-parent/parent-plan",
        )
        sub2 = Task(
            id="t-sub-2",
            project_id="p-1",
            title="Step 2",
            description="Second subtask",
            status=TaskStatus.DEFINED,
            parent_task_id="t-parent",
            is_plan_subtask=True,
        )
        await o.db.create_task(sub1)
        await o.db.create_task(sub2)

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="task/t-parent/parent-plan")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value=None)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_branch = AsyncMock(return_value=None)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        o.git = mock_git

        yield o, sub1
        await _drain_running_tasks(o)
        await o.shutdown()

    def _make_ctx(self, orch, task, ws_path):
        from src.models import PipelineContext

        return PipelineContext(
            task=task,
            agent=Agent(id="a-1", name="claude-1", profile_id="claude"),
            output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=100),
            workspace_path=ws_path,
            workspace_id="ws-1",
            repo=RepoConfig(
                id="r-1", project_id="p-1", source_type=RepoSourceType.LINK, default_branch="main"
            ),
            default_branch="main",
        )

    async def test_passes_on_task_branch_no_uncommitted(self, pipeline_orch):
        """Intermediate subtask passes when on task branch with no uncommitted changes."""
        orch, sub1 = pipeline_orch
        from src.models import PhaseResult

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, sub1, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE

    async def test_auto_commits_uncommitted_changes(self, pipeline_orch):
        """Intermediate subtask auto-commits uncommitted changes."""
        orch, sub1 = pipeline_orch
        from src.models import PhaseResult

        # First call returns True (initial check), second returns False (re-check after commit)
        orch.git.ahas_uncommitted_changes = AsyncMock(side_effect=[True, False])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, sub1, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        # Auto-commit should fix the uncommitted changes
        assert result == PhaseResult.CONTINUE
        orch.git.acommit_all.assert_awaited_once()

    async def test_fails_when_all_remediation_fails(self, pipeline_orch):
        """Intermediate subtask fails when all auto-remediation attempts fail."""
        orch, sub1 = pipeline_orch
        from src.models import PhaseResult

        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))
        # Force-clean also fails to clean the workspace
        orch.git.aforce_clean_workspace = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, sub1, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP


class TestCleanupWorkspaceForNextTask:
    """Tests for _cleanup_workspace_for_next_task."""

    @pytest.fixture
    async def cleanup_orch(self, tmp_path):
        """Orchestrator with mocked git for workspace cleanup tests."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.aget_current_branch = AsyncMock(return_value="main")
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git._arun = AsyncMock(return_value=None)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        o.git = mock_git

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_noop_when_workspace_is_none(self, cleanup_orch):
        """Does nothing when workspace is None."""
        orch = cleanup_orch
        await orch._cleanup_workspace_for_next_task(None, "main", "t-1")
        orch.git.avalidate_checkout.assert_not_awaited()

    async def test_noop_when_no_uncommitted_on_default(self, cleanup_orch):
        """Does nothing when workspace is clean and on default branch."""
        orch = cleanup_orch
        await orch._cleanup_workspace_for_next_task("/fake/path", "main", "t-1")
        orch.git.acommit_all.assert_not_awaited()

    async def test_commits_uncommitted_changes(self, cleanup_orch):
        """Commits uncommitted changes during cleanup."""
        orch = cleanup_orch
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)

        await orch._cleanup_workspace_for_next_task("/fake/path", "main", "t-1")
        orch.git.acommit_all.assert_awaited_once()

    async def test_stashes_when_commit_fails(self, cleanup_orch):
        """Falls back to git stash when auto-commit fails."""
        orch = cleanup_orch
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))

        await orch._cleanup_workspace_for_next_task("/fake/path", "main", "t-1")
        # Should have tried stash via _arun
        orch.git._arun.assert_awaited()
        stash_call = orch.git._arun.call_args_list[0]
        assert stash_call[0][0][0] == "stash"

    async def test_switches_to_default_branch(self, cleanup_orch):
        """Switches to default branch when on a different branch."""
        orch = cleanup_orch
        orch.git.aget_current_branch = AsyncMock(return_value="feature-branch")

        await orch._cleanup_workspace_for_next_task("/fake/path", "main", "t-1")
        # Should checkout default branch
        checkout_call = orch.git._arun.call_args_list[0]
        assert checkout_call[0][0] == ["checkout", "main"]


class TestVerificationReopen:
    """Tests for _reopen_with_verification_feedback."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with a task for reopen testing."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
            auto_task=AutoTaskConfig(max_verification_retries=2),
        )
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await o.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_reopens_task_to_ready_with_feedback(self, pipeline_orch):
        """First failure reopens task to READY and adds verification_feedback context."""
        orch = pipeline_orch

        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="Original description",
            status=TaskStatus.IN_PROGRESS,
            branch_name="feature-1",
        )
        await orch.db.create_task(task)

        failures = [("You left uncommitted changes.", True)]
        result = await orch._reopen_with_verification_feedback(task, failures)

        assert result is True

        # Task should be READY
        updated = await orch.db.get_task("t-1")
        assert updated.status == TaskStatus.READY
        # Description should contain feedback
        assert "Git Verification Feedback" in updated.description
        assert "uncommitted changes" in updated.description

        # task_context should have a verification_feedback entry
        contexts = await orch.db.get_task_contexts("t-1")
        vf_contexts = [c for c in contexts if c["type"] == "verification_feedback"]
        assert len(vf_contexts) == 1

    async def test_blocks_after_max_retries(self, pipeline_orch):
        """Returns False after max_verification_retries are exhausted."""
        orch = pipeline_orch

        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="Original description",
            status=TaskStatus.IN_PROGRESS,
            branch_name="feature-2",
        )
        await orch.db.create_task(task)

        # Simulate 2 previous verification_feedback entries (max is 2)
        await orch.db.add_task_context(
            "t-2",
            type="verification_feedback",
            label="Git Verification Feedback",
            content="attempt 1",
        )
        await orch.db.add_task_context(
            "t-2",
            type="verification_feedback",
            label="Git Verification Feedback",
            content="attempt 2",
        )

        failures = [("Still has uncommitted changes.", True)]
        result = await orch._reopen_with_verification_feedback(task, failures)

        assert result is False

        # Task should NOT have been transitioned to READY
        updated = await orch.db.get_task("t-2")
        assert updated.status == TaskStatus.IN_PROGRESS


# ── Completion Pipeline Tests ──────────────────────────────────────────


class TestCompletionPipelineVerify:
    """Tests for the completion pipeline's verify phase."""

    @pytest.fixture
    async def pipeline_orch(self, tmp_path):
        """Orchestrator with mocked git for pipeline tests."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        # Exclusive-clone pipeline semantics — see TestPhaseVerifyNormalTask.
        config.worktrees.enabled = False
        o = Orchestrator(config, runtimes=MockAdapterFactory())
        await o.initialize()

        # Set up project, workspace, agent
        await o.db.create_project(Project(id="p-1", name="alpha"))
        ws_path = str(tmp_path / "workspaces" / "ws1")
        os.makedirs(ws_path, exist_ok=True)
        await o.db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await o.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))

        # Mock git — default: everything passes verification
        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="main")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value=None)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.ahas_non_plan_changes = AsyncMock(return_value=False)
        o.git = mock_git

        yield o
        await _drain_running_tasks(o)
        await o.shutdown()

    def _make_ctx(self, orch, task, ws_path):
        from src.models import PipelineContext

        return PipelineContext(
            task=task,
            agent=Agent(id="a-1", name="claude-1", profile_id="claude"),
            output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=100),
            workspace_path=ws_path,
            workspace_id="ws-1",
            repo=RepoConfig(
                id="r-1", project_id="p-1", source_type=RepoSourceType.LINK, default_branch="main"
            ),
            default_branch="main",
        )

    async def test_pipeline_stops_when_verify_returns_stop(self, pipeline_orch):
        """Pipeline returns completed_ok=False when verify phase returns STOP."""
        orch = pipeline_orch

        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-2")

        # Agent left uncommitted changes that can't be remediated —
        # all auto-remediation attempts fail, forcing verification STOP.
        orch.git.aget_current_branch = AsyncMock(return_value="feature-2")
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))
        orch.git.aforce_clean_workspace = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace_for_task("t-2")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is False

    async def test_completed_ok_true_when_verify_passes(self, pipeline_orch):
        """Pipeline returns completed_ok=True when verify phase passes."""
        orch = pipeline_orch

        task = Task(
            id="t-3",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-3")

        ws = await orch.db.get_workspace_for_task("t-3")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is True
        assert pr_url is None

    async def test_pipeline_error_handling(self, pipeline_orch):
        """Phase that raises should not crash pipeline, returns ok=False."""
        orch = pipeline_orch

        # Make verify phase raise an exception
        orch._phase_verify = AsyncMock(side_effect=RuntimeError("verify exploded"))

        task = Task(
            id="t-4",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-4",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        await orch.db.acquire_workspace("p-1", "a-1", "t-4")

        ws = await orch.db.get_workspace_for_task("t-4")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        pr_url, ok = await orch._run_completion_pipeline(ctx)
        assert ok is False  # should not crash


# ── Workspace Affinity for Plan Subtasks ────────────────���──────────────


# ── Failed/Blocked Report ──────────��───────────────────────────────────


@pytest.mark.asyncio
class TestFailedBlockedReport:
    """Test the periodic failed/blocked task report in the orchestrator."""

    async def test_report_sent_when_tasks_exist(self, orch):
        """Report should be sent when there are FAILED or BLOCKED tasks."""
        await _create_project_with_workspace(orch.db)
        orch._emit_text_notify = AsyncMock()

        # Create a failed and a blocked task
        await orch.db.create_task(
            Task(
                id="t-fail",
                project_id="p-1",
                title="Failed task",
                description="D",
                status=TaskStatus.FAILED,
                retry_count=2,
                max_retries=3,
            )
        )
        await orch.db.create_task(
            Task(
                id="t-block",
                project_id="p-1",
                title="Blocked task",
                description="D",
                status=TaskStatus.BLOCKED,
            )
        )

        # Ensure the rate-limiter allows the report
        orch._last_failed_blocked_report = 0.0
        await orch._check_failed_blocked_tasks()

        # Should have sent a notification
        assert orch._emit_text_notify.call_count >= 1
        # Check the plain-text message contains key info
        call_msg = orch._emit_text_notify.call_args_list[0][0][0]
        assert "Attention Required" in call_msg
        assert "t-fail" in call_msg
        assert "t-block" in call_msg

    async def test_no_report_when_no_failed_blocked(self, orch):
        """Report should NOT be sent when there are no FAILED/BLOCKED tasks."""
        await _create_project_with_workspace(orch.db)
        orch._emit_text_notify = AsyncMock()

        # Create only a READY task
        await orch.db.create_task(
            Task(
                id="t-ready",
                project_id="p-1",
                title="Ready task",
                description="D",
                status=TaskStatus.READY,
            )
        )

        orch._last_failed_blocked_report = 0.0
        await orch._check_failed_blocked_tasks()

        orch._emit_text_notify.assert_not_called()

    async def test_report_rate_limited(self, orch):
        """Report should be rate-limited by the configured interval."""
        await _create_project_with_workspace(orch.db)
        orch._emit_text_notify = AsyncMock()

        await orch.db.create_task(
            Task(
                id="t-fail",
                project_id="p-1",
                title="Failed",
                description="D",
                status=TaskStatus.FAILED,
            )
        )

        # First call — should send
        orch._last_failed_blocked_report = 0.0
        await orch._check_failed_blocked_tasks()
        assert orch._emit_text_notify.call_count == 1

        # Second call immediately — should NOT send (rate-limited)
        await orch._check_failed_blocked_tasks()
        assert orch._emit_text_notify.call_count == 1  # still 1

    async def test_report_disabled_when_interval_zero(self, orch):
        """Report should be disabled when interval is 0."""
        await _create_project_with_workspace(orch.db)
        orch._emit_text_notify = AsyncMock()

        await orch.db.create_task(
            Task(
                id="t-fail",
                project_id="p-1",
                title="Failed",
                description="D",
                status=TaskStatus.FAILED,
            )
        )

        orch.config.monitoring.failed_blocked_report_interval_seconds = 0
        orch._last_failed_blocked_report = 0.0
        await orch._check_failed_blocked_tasks()

        orch._emit_text_notify.assert_not_called()

    async def test_report_groups_by_project(self, orch):
        """Report should send separate notifications for each project."""
        await _create_project_with_workspace(orch.db, project_id="p-1", name="alpha")
        await _create_project_with_workspace(
            orch.db, project_id="p-2", name="beta", workspace_path="/tmp/ws-2"
        )
        orch._emit_text_notify = AsyncMock()

        await orch.db.create_task(
            Task(
                id="t-f1",
                project_id="p-1",
                title="Fail in alpha",
                description="D",
                status=TaskStatus.FAILED,
            )
        )
        await orch.db.create_task(
            Task(
                id="t-b2",
                project_id="p-2",
                title="Blocked in beta",
                description="D",
                status=TaskStatus.BLOCKED,
            )
        )

        orch._last_failed_blocked_report = 0.0
        await orch._check_failed_blocked_tasks()

        # Should have notified twice — once per project
        assert orch._emit_text_notify.call_count == 2
