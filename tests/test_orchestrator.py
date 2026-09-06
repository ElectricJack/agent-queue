import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.orchestrator import Orchestrator
from src.models import (
    AgentProfile,
    DepType,
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
from src.config import AppConfig, AutoTaskConfig, GitHubAppConfig
from src.intelligence_classes import IntelligenceClass
from src.sessions.harness_parser import Harness
from src.git.manager import GitManager
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


async def test_orchestrator_owns_single_integration_service_loop(orch):
    service = orch.integration_service
    assert service is not None
    assert service._task is not None
    assert service._task.get_name() == "integration-reconciliation-service"
    assert orch.integration_scheduler is not None
    assert orch.integration_outbox is not None
    assert orch.integration_attestation_service is not None
    assert orch.integration_control_service is not None
    assert service._drain_handler.__self__ is orch.integration_control_service
    assert orch.integration_control_service.external_preflight is not None
    assert (
        service._candidate_ci_handler.__self__
        is orch.integration_attestation_service
    )
    assert (
        orch.integration_attestation_resolver.__self__
        is orch.integration_attestation_service
    )
    original_runtime = orch.playbook_manager
    runtime = MagicMock()
    runtime.accept_integration_event = AsyncMock(return_value=True)
    orch.playbook_manager = runtime
    try:
        assert await orch.integration_outbox._accept_event(
            "integration.sweep_due", {"project_id": "p"}, "event-1"
        )
        runtime.accept_integration_event.assert_awaited_once_with(
            "integration.sweep_due", {"project_id": "p"}, "event-1"
        )
    finally:
        orch.playbook_manager = original_runtime


async def test_configured_orchestrator_installs_repository_bound_candidate_transport(
    tmp_path, monkeypatch
):
    from src.git.github_app import GitHubAppClient, GitHubRepositoryBinding

    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    config.integration.github_app = GitHubAppConfig(
        client_id="Iv1.test",
        app_id=101,
        installation_id=202,
        private_key_path="/daemon/key.pem",
    )
    binding = GitHubRepositoryBinding(303, "acme/widgets")
    bound_client = MagicMock(repository=binding)
    bind_repository = AsyncMock(return_value=bound_client)
    monkeypatch.setattr(GitHubAppClient, "bind_repository", bind_repository)
    orchestrator = Orchestrator(config, runtimes=MockAdapterFactory())
    await orchestrator.initialize()
    try:
        repository = RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
        )
        resolved = await orchestrator.integration_repository_binding_resolver(repository)

        assert resolved == binding
        assert orchestrator.integration_app_client_factory(binding) is bound_client
        bind_repository.assert_awaited_once()
    finally:
        await orchestrator.shutdown()


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


_SESSION_CLASSES = {
    "standard-medium": IntelligenceClass(
        "standard-medium", "Standard", "", {"anthropic": {"model": "claude-sonnet-5"}}
    ),
}


@pytest.fixture
async def session_orch(tmp_path):
    """An orchestrator that dispatches the way production does: as a session.

    The plain ``orch`` fixture injects a ``MockAdapterFactory`` into
    ``_runtimes``, a seam ``_execute_task`` no longer consults — with the
    runtime subsystem removed it requires ``sessions.enabled`` plus a profile
    carrying a ``harness`` (``_is_session_routed``), and raises otherwise.
    Scheduling tests that need a task to actually leave READY use this
    fixture and the ``fake`` session provider instead.
    """
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    config.worktrees.enabled = False
    config.sessions.enabled = True
    config.sessions.provider = "fake"
    o = Orchestrator(config)
    await o.initialize()
    o.session_spec_builder._intelligence_classes = dict(_SESSION_CLASSES)
    # Branch setup is not what these tests assert on, and the workspaces
    # below are bare directories rather than real clones.
    o.git = AsyncMock()
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
    # ``_prepare_workspace`` resolves Git's exclude path and the verify phase
    # inspects the delivery diff.  A bare AsyncMock answers both with a mock:
    # truthy (read as a reserved-path finding) and, via ``__fspath__``, a
    # relative directory that ``ensure_git_exclude_path`` creates in the CWD.
    o.git.aget_git_path = AsyncMock(
        side_effect=lambda checkout, path: os.path.join(checkout, ".git", path)
    )
    o.git.areserved_paths_in_diff = AsyncMock(return_value=[])
    o.harness_registry.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    install_already_routed(o)
    yield o
    await _drain_running_tasks(o)
    await o.shutdown()


async def _create_session_project(orch, *, project_id: str = "p-1") -> None:
    """A profile with a harness, a project defaulting to it, and a workspace."""
    await orch.db.create_profile(
        AgentProfile(
            id="claude",
            name="claude",
            harness="claude",
            default_class="standard-medium",
        )
    )
    await orch.db.create_project(Project(id=project_id, name="alpha", default_profile_id="claude"))
    path = os.path.join(orch.config.workspace_dir, project_id)
    os.makedirs(path, exist_ok=True)
    await orch.db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path=path,
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )


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
    """The orchestrator cycle's half of a task's life: promotion and dispatch.

    These once drove a task all the way to COMPLETED through a
    ``MockAdapterFactory`` that returned an ``AgentOutput`` from
    ``Runtime.wait()``.  The runtime subsystem is gone: dispatch now launches
    a session and returns, and the terminal transition arrives later from the
    agent's own ``aq task close``.  Two of the old cases are therefore no
    longer expressible here and are not restated below:

    * ``test_failed_task_retries`` — retry-on-failure now runs off session
      close and session death; covered by
      ``test_session_commands.py::TestEndToEndOnFakeProvider::test_transient_failure_retries_instead_of_going_terminal``
      and
      ``test_session_reconciler.py::TestExitHandling::test_productive_death_pauses_with_a_backoff_never_silently_ready``.
    * ``test_paused_on_token_exhaustion`` — the behaviour itself was removed
      with the runtime pipeline.  ``AgentResult.PAUSED_TOKENS`` survives only
      as an enum member in ``src/models.py``; nothing in ``src/`` consumes it,
      so there is no PAUSED-on-token-exhaustion path left to assert.

    The completion half of the lifecycle lives with the close protocol:
    ``test_session_commands.py::TestEndToEndOnFakeProvider::test_full_lifecycle``
    drives launch → ``task_close`` → COMPLETED, and
    ``...::test_disabled_sessions_fail_instead_of_using_a_runtime`` pins the
    "no session harness" error these cases used to trip over.
    """

    async def test_full_task_lifecycle(self, session_orch):
        """DEFINED → READY → IN_PROGRESS, with a session actually launched."""
        orch = session_orch
        await _create_session_project(orch)
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Do it",
                status=TaskStatus.DEFINED,
            )
        )

        await _run_cycle_and_wait(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id is not None
        session = await orch.db.get_session_for_task("t-1")
        assert session is not None
        assert session.harness == "claude"

    async def test_dependencies_block_scheduling(self, session_orch):
        """A dependent task is not promoted until its blocker is COMPLETED."""
        orch = session_orch
        await _create_session_project(orch)
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

        # t-1 has no deps, so it is promoted and dispatched.  t-2 must stay
        # DEFINED: its blocker is running, not done.
        await _run_cycle_and_wait(orch)

        assert (await orch.db.get_task("t-1")).status == TaskStatus.IN_PROGRESS
        assert (await orch.db.get_task("t-2")).status == TaskStatus.DEFINED

        # Once the blocker completes the way a real agent completes it — via
        # a terminal transition — the next cycle promotes t-2.
        await orch.db.transition_task("t-1", TaskStatus.COMPLETED, context="test_close")

        await _run_cycle_and_wait(orch)

        assert (await orch.db.get_task("t-2")).status != TaskStatus.DEFINED


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


class TestContainerRelease:
    """A flagged container is released, never dispatched (spec §7).

    A supervisor-built container (``aq task create`` + children under it via
    ``--parent``) starts DEFINED and carries ``task_metadata.container``.  Its
    only job is to settle once its children finish.  Handing it to a worker
    session is self-defeating: Invariant 6 refuses the worker's close while a
    child is open, and the worker's own live session is exactly what blocks
    settlement, so it idle-heartbeats holding an agent slot and the
    project-repo lock until the last child completes (calm-ember-48).

    Release therefore lands the container at IN_PROGRESS with no agent — the
    same shape ``creator.PARENT_STATUS`` births graph containers in and
    recovery preserves — so settlement can close it.
    """

    async def _container_with_child(self, orch, *, container_status, child_status):
        await orch.db.create_task(
            Task(
                id="c-1",
                project_id="p-1",
                title="Medium findings",
                description="settle when the children finish",
                status=container_status,
            )
        )
        await orch.db.create_task(
            Task(
                id="c-1.1",
                project_id="p-1",
                title="Fix one finding",
                description="a real deliverable",
                status=child_status,
            )
        )
        # ``set_parent`` (any path) is what writes the container flag.
        await orch.db.add_dependency("c-1.1", "c-1", DepType.PARENT_CHILD.value)
        assert await orch.db.get_task_meta("c-1", "container") is True

    async def test_promotion_releases_container_to_in_progress_without_agent(self, orch):
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await self._container_with_child(
            orch, container_status=TaskStatus.DEFINED, child_status=TaskStatus.DEFINED
        )
        # Withheld while the container is DEFINED.
        assert (await orch.db.get_task("c-1.1")).is_blocked is True

        await orch._check_defined_tasks()

        container = await orch.db.get_task("c-1")
        assert container.status == TaskStatus.IN_PROGRESS
        assert container.assigned_agent_id is None
        # Releasing the container releases its children.
        await orch._check_defined_tasks()
        assert (await orch.db.get_task("c-1.1")).status == TaskStatus.READY

    async def test_container_with_open_children_is_never_in_routed_ready(self, session_orch):
        orch = session_orch
        await _create_session_project(orch)
        # Already READY when it became a container: the shape ``set_parent``
        # produces when children are attached after promotion.
        await self._container_with_child(
            orch, container_status=TaskStatus.READY, child_status=TaskStatus.READY
        )

        seen: list[list[str]] = []
        real_reconcile = orch._agent_reconciler.reconcile

        async def spy(**kwargs):
            seen.append([t.id for t in kwargs.get("ready_tasks") or []])
            return await real_reconcile(**kwargs)

        orch._agent_reconciler.reconcile = spy

        await _run_cycle_and_wait(orch)

        assert seen, "the scheduler never consulted the reconciler"
        assert all("c-1" not in ready for ready in seen)
        container = await orch.db.get_task("c-1")
        assert container.status == TaskStatus.IN_PROGRESS
        assert container.assigned_agent_id is None
        assert await orch.db.get_session_for_task("c-1") is None
        # The child — the task with a deliverable — is what gets the session.
        child = await orch.db.get_task("c-1.1")
        assert child.status == TaskStatus.IN_PROGRESS
        assert await orch.db.get_session_for_task("c-1.1") is not None

    async def test_released_container_whose_children_are_done_settles_at_once(self, orch):
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await self._container_with_child(
            orch, container_status=TaskStatus.READY, child_status=TaskStatus.COMPLETED
        )

        await _run_cycle_and_wait(orch)

        assert (await orch.db.get_task("c-1")).status == TaskStatus.COMPLETED


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

    async def test_ready_task_dispatches_with_only_workspace_and_default_profile(
        self, session_orch
    ):
        """The original quick-ember bug: project with workspace +
        default_profile_id + READY task should dispatch within one cycle —
        no manual agent creation. Tests the full reconciler → scheduler →
        executor chain, now terminating in a session launch rather than in
        the removed runtime adapter.
        """
        orch = session_orch
        await _create_session_project(orch)
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
        assert task.status == TaskStatus.IN_PROGRESS
        # The agent the reconciler supplied, not one the test created.
        assert task.assigned_agent_id is not None
        worker = await orch.db.get_agent(task.assigned_agent_id)
        assert worker.profile_id == "claude"


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
        # These tests isolate branch preparation; managed-exclude handoff has
        # dedicated real-repository coverage.
        orch._ensure_control_files_excluded = AsyncMock(return_value=True)

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
        # ``_prepare_workspace`` installs the managed ``info/exclude``
        # block and fails closed when Git's path for it cannot be
        # resolved, so an awaitable answer is mandatory here.
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda cwd, path: os.path.join(cwd, ".git", path)
        )
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda checkout, path: os.path.join(checkout, ".git", path)
        )
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
        # ``_prepare_workspace`` installs the managed ``info/exclude``
        # block and fails closed when Git's path for it cannot be
        # resolved, so an awaitable answer is mandatory here.
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda cwd, path: os.path.join(cwd, ".git", path)
        )
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.aprepare_for_task = AsyncMock()
        mock_git.aswitch_to_branch = AsyncMock()
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda checkout, path: os.path.join(checkout, ".git", path)
        )
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
        # ``_prepare_workspace`` installs the managed ``info/exclude``
        # block and fails closed when Git's path for it cannot be
        # resolved, so an awaitable answer is mandatory here.
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda cwd, path: os.path.join(cwd, ".git", path)
        )
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aget_git_path = AsyncMock(
            side_effect=lambda checkout, path: os.path.join(checkout, ".git", path)
        )
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

        # spec=GitManager — see TestCompletionPipelineVerify: an unstubbed
        # a-prefixed method must be an awaitable AsyncMock, not a MagicMock
        # that blows up inside the phase under test.
        mock_git = MagicMock(spec=GitManager)
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="main")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value=None)
        mock_git.acount_commits_ahead = AsyncMock(return_value=1)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.areserved_paths_in_index = AsyncMock(return_value=set())
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=set())
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_validated_delivery = AsyncMock(return_value="a" * 40)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        # The delivery guard runs before every merge and push in
        # ``_phase_verify`` and fails closed.  Unstubbed, a spec'd
        # AsyncMock answers with a truthy MagicMock, which the guard
        # reads as "this delivery changes reserved daemon paths".
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=[])
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
        orch.git.acount_commits_ahead = AsyncMock(return_value=0)

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

        # Initial dirty status, then clean after commit and on strict re-check.
        orch.git.ahas_uncommitted_changes = AsyncMock(
            side_effect=[True, *([False] * 6)]
        )
        orch.git.acount_commits_ahead = AsyncMock(
            side_effect=lambda _workspace, branch, _base: (
                1 if branch == "main" else 0
            )
        )

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

        # Dirty through failed commit/stash, then clean after force-clean.
        orch.git.ahas_uncommitted_changes = AsyncMock(
            side_effect=[True, True, *([False] * 6)]
        )
        orch.git.acommit_all = AsyncMock(side_effect=Exception("commit failed"))
        # Force-clean succeeds — workspace is clean after reset+clean
        orch.git.aforce_clean_workspace = AsyncMock(return_value=True)
        orch.git.acount_commits_ahead = AsyncMock(return_value=0)

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
        # Initial dirty status, then clean after commit and on strict re-check.
        orch.git.ahas_uncommitted_changes = AsyncMock(
            side_effect=[True, *([False] * 6)]
        )

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
            branch_name="main",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Auto-push rev-list returns "3" (ahead), then the final behind and
        # ahead checks return "0".  The mock push cannot update the refs, so
        # these three values model the complete successful verification.
        orch.git._arun = AsyncMock(side_effect=["3", "0", "0"])

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        orch.git.apush_validated_delivery.assert_awaited_once()
        assert [call.args[0] for call in orch.git._arun.await_args_list] == [
            ["rev-list", "refs/remotes/origin/main..HEAD", "--count"],
            ["rev-list", "HEAD..refs/remotes/origin/main", "--count"],
            ["rev-list", "refs/remotes/origin/main..HEAD", "--count"],
        ]
        # The auto-push goes through the delivery guard, which inspects the
        # tip against origin/<default> before pushing that exact OID.
        assert orch.git.apush_validated_delivery.await_args.args[1:] == (
            "refs/remotes/origin/main",
            "HEAD",
            "main",
        )

    async def test_fails_when_ahead_and_auto_push_fails(self, pipeline_orch):
        """Falls back to failure when auto-push raises an exception."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-4b",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="main",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="direct",
        )
        await orch.db.create_task(task)

        # Auto-push rev-list returns "3" (ahead) — triggers push
        # Push fails, so scenario behind check gets "0", ahead check gets "3"
        orch.git._arun = AsyncMock(side_effect=["3", "0", "3"])
        orch.git.apush_validated_delivery = AsyncMock(side_effect=Exception("push failed"))

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

        # spec=GitManager — see TestCompletionPipelineVerify.
        mock_git = MagicMock(spec=GitManager)
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="feature-1")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/42")
        mock_git.ais_ancestor = AsyncMock(return_value=False)
        # Default: the task branch carries work, so the PR gate applies.
        mock_git.acount_commits_ahead = AsyncMock(return_value=1)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.areserved_paths_in_index = AsyncMock(return_value=set())
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=set())
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_validated_delivery = AsyncMock(return_value="a" * 40)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        # The delivery guard runs before every merge and push in
        # ``_phase_verify`` and fails closed.  Unstubbed, a spec'd
        # AsyncMock answers with a truthy MagicMock, which the guard
        # reads as "this delivery changes reserved daemon paths".
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=[])
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
        orch.git.ais_ancestor = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP

    async def test_passes_when_merged_pr_is_found(self, pipeline_orch):
        """A merged PR proves the task's branch has already shipped."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-merged-pr",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-merged",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="pull_request",
        )
        await orch.db.create_task(task)
        orch.git.aget_current_branch = AsyncMock(return_value="feature-merged")
        orch.git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/99")

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        assert ctx.pr_url == "https://github.com/org/repo/pull/99"

    async def test_closed_unmerged_pr_still_fails(self, pipeline_orch):
        """The lookup excludes CLOSED PRs, so an unmerged branch still fails."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-closed-pr",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-closed",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="pull_request",
        )
        await orch.db.create_task(task)
        orch.git.aget_current_branch = AsyncMock(return_value="feature-closed")
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        orch.git.ais_ancestor = AsyncMock(return_value=False)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        assert await orch._phase_verify(ctx) == PhaseResult.STOP

    async def test_passes_when_branch_is_already_merged_without_pr(self, pipeline_orch):
        """Direct or squash integration is sufficient even without a PR record."""
        orch = pipeline_orch
        from src.models import PhaseResult

        task = Task(
            id="t-merged-branch",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-integrated",
            status=TaskStatus.IN_PROGRESS,
            integration_mode="pull_request",
        )
        await orch.db.create_task(task)
        orch.git.aget_current_branch = AsyncMock(return_value="feature-integrated")
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        orch.git.ais_ancestor = AsyncMock(return_value=True)

        ws = await orch.db.get_workspace("ws-1")
        ctx = self._make_ctx(orch, task, ws.workspace_path)

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE


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
        mock_git.acount_commits_ahead = AsyncMock(return_value=1)
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.areserved_paths_in_index = AsyncMock(return_value=set())
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=set())
        mock_git.acommit_all = AsyncMock(return_value=True)
        mock_git.apush_validated_delivery = AsyncMock(return_value="a" * 40)
        mock_git.aabort_in_progress_operations = AsyncMock()
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        # The delivery guard runs before every merge and push in
        # ``_phase_verify`` and fails closed.  Unstubbed, a spec'd
        # AsyncMock answers with a truthy MagicMock, which the guard
        # reads as "this delivery changes reserved daemon paths".
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=[])
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

        # Initial dirty status, then clean after commit and on strict re-check.
        orch.git.ahas_uncommitted_changes = AsyncMock(
            side_effect=[True, *([False] * 6)]
        )

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

        # Mock git — default: everything passes verification.
        # spec=GitManager so a method _phase_verify starts calling is an
        # awaitable AsyncMock (and a typo is an AttributeError) instead of a
        # bare MagicMock that raises inside the phase and silently turns
        # every completed_ok assertion in this class into False.
        mock_git = MagicMock(spec=GitManager)
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.aget_current_branch = AsyncMock(return_value="main")
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git.afind_open_pr = AsyncMock(return_value=None)
        # These tasks carry no integration_mode, so they run in the config
        # default (pull_request) with the checkout left on the default
        # branch. The passing shape there is "the task branch is already
        # integrated into origin/main": no PR is expected and ctx.pr_url
        # stays None.
        mock_git.ais_ancestor = AsyncMock(return_value=True)
        mock_git.acount_commits_ahead = AsyncMock(
            side_effect=lambda _workspace, branch, _base: (
                0 if branch == "refs/heads/main" else 1
            )
        )
        mock_git._arun = AsyncMock(return_value="0")
        mock_git.areserved_paths_in_index = AsyncMock(return_value=set())
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=set())
        mock_git.ahas_non_plan_changes = AsyncMock(return_value=False)
        # The delivery guard runs before every merge and push in
        # ``_phase_verify`` and fails closed.  Unstubbed, a spec'd
        # AsyncMock answers with a truthy MagicMock, which the guard
        # reads as "this delivery changes reserved daemon paths".
        mock_git.areserved_paths_in_diff = AsyncMock(return_value=[])
        mock_git.apush_validated_delivery = AsyncMock(return_value="a" * 40)
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


class TestTerminalBlockedIsNotRecovered:
    """A terminal close must stay BLOCKED through the promotion cascade.

    Regression for crisp-pinnacle-54: the BLOCKED-recovery rule (design
    §4.4) promoted ``BLOCKED ∧ is_blocked = 0`` whenever the task carried
    *any* blocking edge.  Every child of a container carries a
    ``parent-child`` edge, so a child closed ``--failure-class hard`` was
    re-dispatched on the next cycle, forever.
    """

    async def _hard_close_child_of_released_container(self, orch):
        from src.models import AgentState, DepType

        await _create_project_with_workspace(orch.db)
        await orch.db.create_profile(AgentProfile(id="claude", name="Claude", harness="claude"))
        await orch.db.create_agent(
            Agent(id="a-hard", name="agent-hard", profile_id="claude", state=AgentState.BUSY)
        )
        await orch.db.create_task(
            Task(
                id="t-epic",
                project_id="p-1",
                title="Container",
                description="released container",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        child = Task(
            id="t-epic.1",
            project_id="p-1",
            title="Child",
            description="child of a released container",
            status=TaskStatus.IN_PROGRESS,
            parent_task_id="t-epic",
            assigned_agent_id="a-hard",
            profile_id="claude",
        )
        await orch.db.create_task(child)
        await orch.db.add_dependency("t-epic.1", "t-epic", DepType.PARENT_CHILD.value)

        result = await orch.complete_session_task(
            child, outcome="fail", failure_class="hard", notes="cannot be done"
        )
        assert result["status"] == TaskStatus.BLOCKED.value
        refreshed = await orch.db.get_task("t-epic.1")
        assert refreshed.status == TaskStatus.BLOCKED
        # The container is released, so the projection is clear: this is
        # exactly the shape the recovery rule used to misread.
        assert refreshed.is_blocked is False
        return refreshed

    @pytest.mark.parametrize("authoritative", [False, True])
    async def test_hard_failed_child_stays_blocked_across_cycles(self, orch, authoritative):
        orch.config.work_graph.blocked_state_authoritative = authoritative
        await self._hard_close_child_of_released_container(orch)

        for _ in range(3):
            await orch._check_defined_tasks()
            assert (await orch.db.get_task("t-epic.1")).status == TaskStatus.BLOCKED

        await _run_cycle_and_wait(orch)
        assert (await orch.db.get_task("t-epic.1")).status == TaskStatus.BLOCKED
        assert await orch.db.get_task_meta("t-epic.1", "blocked_terminal") == (
            "session_close_hard_failure"
        )

    async def test_restart_clears_the_terminal_mark(self, orch):
        """An operator restart is the sanctioned way back; the mark goes with it."""
        await self._hard_close_child_of_released_container(orch)

        await orch.db.transition_task("t-epic.1", TaskStatus.READY, context="restart_task")
        assert (await orch.db.get_task("t-epic.1")).status == TaskStatus.READY
        assert await orch.db.get_task_meta("t-epic.1", "blocked_terminal") is None

    async def test_graph_blocked_task_still_recovers(self, orch):
        """The recovery rule keeps working for a BLOCKED task with a real graph reason."""
        from src.models import DepType

        orch.config.work_graph.blocked_state_authoritative = True
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(
            Task(id="t-dep", project_id="p-1", title="Dep", description="d", status=TaskStatus.READY)
        )
        await orch.db.create_task(
            Task(
                id="t-waiter",
                project_id="p-1",
                title="Waiter",
                description="d",
                status=TaskStatus.BLOCKED,
            )
        )
        await orch.db.add_dependency("t-waiter", "t-dep", DepType.BLOCKS.value)
        assert (await orch.db.get_task("t-waiter")).is_blocked is True

        await orch._check_defined_tasks()
        assert (await orch.db.get_task("t-waiter")).status == TaskStatus.BLOCKED

        await orch.db.transition_task("t-dep", TaskStatus.COMPLETED)
        await orch._check_defined_tasks()
        assert (await orch.db.get_task("t-waiter")).status == TaskStatus.READY


class TestMergeConflictBlockedIsNotRecovered:
    """A merge-conflict BLOCKED child must not re-enter the ready frontier."""

    @pytest.mark.parametrize("authoritative", [False, True])
    async def test_merge_conflict_child_stays_blocked_across_cycles(self, orch, authoritative):
        from src.models import DepType

        orch.config.work_graph.blocked_state_authoritative = authoritative
        await _create_project_with_workspace(orch.db)
        await orch.db.create_task(
            Task(
                id="t-epic",
                project_id="p-1",
                title="Container",
                description="released container",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await orch.db.create_task(
            Task(
                id="t-epic.1",
                project_id="p-1",
                title="Conflicted child",
                description="child with a merge conflict",
                status=TaskStatus.IN_PROGRESS,
                parent_task_id="t-epic",
            )
        )
        await orch.db.add_dependency("t-epic.1", "t-epic", DepType.PARENT_CHILD.value)

        await orch.db.transition_task(
            "t-epic.1", TaskStatus.BLOCKED, context="merge_conflict"
        )
        child = await orch.db.get_task("t-epic.1")
        assert child.is_blocked is False

        await orch._check_defined_tasks()

        assert (await orch.db.get_task("t-epic.1")).status == TaskStatus.BLOCKED
