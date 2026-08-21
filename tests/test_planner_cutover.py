"""Tests for Task 8: planner cutover gates (P5).

Verifies that `config.planner.legacy_plan_discovery` gates the legacy
plan.md pipeline at the three sites called out in supervisor-agent.md
§9 rows 4-5:

- ``src/orchestrator/execution.py`` — AWAITING_PLAN_APPROVAL transition +
  ``break_plan_into_tasks`` call.
- ``src/orchestrator/approval.py`` — ``_phase_plan_discover`` +
  ``_phase_plan_generate``.
- ``src/orchestrator/git_ops.py`` — caller of ``_phase_plan_discover``
  inside ``_run_completion_pipeline``.

Flag semantics:
- True (default) → today's behaviour, byte-for-byte.
- False → legacy region is skipped ("skip + log at info" — the spec text
  in §9 leaves the concrete replacement undefined; per the Task 8 brief,
  the fallback is skip+log rather than invented behaviour).

Drain: a task already in AWAITING_PLAN_APPROVAL must always be able to
finish via the legacy path regardless of the flag, so flipping the flag
mid-flight strands nothing (spec §11 P5).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    PipelineContext,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator

from tests.test_orchestrator import MockAdapterFactory, _drain_running_tasks  # type: ignore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def orch(tmp_path):
    """Orchestrator with mocked git + auto_task enabled — mirrors the
    ``TestCompletionPipelineVerify.pipeline_orch`` fixture in
    ``tests/test_orchestrator.py`` (kept in sync with harness cribbed from
    that file per the Task 8 brief)."""
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    config.worktrees.enabled = False
    # Default is True; individual tests flip as needed.
    assert config.planner.legacy_plan_discovery is True

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
    mock_git.ahas_non_plan_changes = AsyncMock(return_value=False)
    mock_git.acommit_all = AsyncMock(return_value=None)
    o.git = mock_git

    yield o
    await _drain_running_tasks(o)
    await o.shutdown()


def _make_ctx(task, ws_path):
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


# ---------------------------------------------------------------------------
# _phase_plan_generate (approval.py :282-ish)
# ---------------------------------------------------------------------------


class TestPhasePlanGenerateGate:
    async def test_flag_true_invokes_discover_and_store(self, orch):
        """Flag True (default): legacy ``_discover_and_store_plan`` is called."""
        task = Task(
            id="t-gen-1",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-1",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        orch._discover_and_store_plan = AsyncMock(return_value=False)
        ctx = _make_ctx(task, ws_path)
        await orch._phase_plan_generate(ctx)
        assert orch._discover_and_store_plan.await_count == 1

    async def test_flag_false_skips_discover_and_store(self, orch):
        """Flag False: legacy ``_discover_and_store_plan`` is NOT called."""
        orch.config.planner.legacy_plan_discovery = False

        task = Task(
            id="t-gen-2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        orch._discover_and_store_plan = AsyncMock(return_value=False)
        ctx = _make_ctx(task, ws_path)
        result = await orch._phase_plan_generate(ctx)
        # PhaseResult.CONTINUE, and legacy was skipped
        assert orch._discover_and_store_plan.await_count == 0
        assert ctx.plan_needs_approval is False
        from src.models import PhaseResult

        assert result == PhaseResult.CONTINUE

    async def test_flag_false_but_drain_awaiting_plan_approval(self, orch):
        """Drain: a task already in AWAITING_PLAN_APPROVAL still hits legacy."""
        orch.config.planner.legacy_plan_discovery = False

        task = Task(
            id="t-gen-3",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3",
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        orch._discover_and_store_plan = AsyncMock(return_value=False)
        ctx = _make_ctx(task, ws_path)
        await orch._phase_plan_generate(ctx)
        # Drain: mid-flight AWAITING_PLAN_APPROVAL still runs legacy
        assert orch._discover_and_store_plan.await_count == 1


# ---------------------------------------------------------------------------
# _phase_plan_discover (approval.py :224-ish)
# ---------------------------------------------------------------------------


class TestPhasePlanDiscoverGate:
    async def test_flag_true_delegates_to_supervisor(self, orch):
        """Flag True: supervisor.on_task_completed is invoked."""
        task = Task(
            id="t-disc-1",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-1",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        supervisor = MagicMock()
        supervisor.on_task_completed = AsyncMock(return_value={"plan_found": False})
        orch._supervisor = supervisor

        ctx = _make_ctx(task, ws_path)
        await orch._phase_plan_discover(ctx)
        assert supervisor.on_task_completed.await_count == 1

    async def test_flag_false_skips_supervisor_call(self, orch):
        """Flag False: supervisor.on_task_completed NOT called, phase returns CONTINUE."""
        orch.config.planner.legacy_plan_discovery = False

        task = Task(
            id="t-disc-2",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-2",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        supervisor = MagicMock()
        supervisor.on_task_completed = AsyncMock(return_value={"plan_found": True})
        orch._supervisor = supervisor

        ctx = _make_ctx(task, ws_path)
        from src.models import PhaseResult

        result = await orch._phase_plan_discover(ctx)
        assert supervisor.on_task_completed.await_count == 0
        assert ctx.plan_needs_approval is False
        assert result == PhaseResult.CONTINUE

    async def test_flag_false_but_drain_awaiting_plan_approval(self, orch):
        """Drain: a task already in AWAITING_PLAN_APPROVAL still hits legacy."""
        orch.config.planner.legacy_plan_discovery = False

        task = Task(
            id="t-disc-3",
            project_id="p-1",
            title="Test",
            description="test",
            branch_name="feature-3",
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
        )
        await orch.db.create_task(task)
        ws_path = str(orch.config.workspace_dir) + "/ws1"

        supervisor = MagicMock()
        supervisor.on_task_completed = AsyncMock(return_value={"plan_found": False})
        orch._supervisor = supervisor

        ctx = _make_ctx(task, ws_path)
        await orch._phase_plan_discover(ctx)
        # Drain semantics: legacy fires
        assert supervisor.on_task_completed.await_count == 1


# ---------------------------------------------------------------------------
# execution.py :977-1160 — AWAITING_PLAN_APPROVAL region + break_plan_into_tasks
# ---------------------------------------------------------------------------


class TestExecutionPlanApprovalRegionGate:
    """The region only runs when ``ctx.plan_needs_approval`` is True. When
    the flag is False AND the task is not already AWAITING_PLAN_APPROVAL,
    the region must be skipped so ``break_plan_into_tasks`` is not called.

    We test the guard directly via a small helper the implementation
    exposes: ``_should_run_legacy_plan_region(task)``.
    """

    async def test_flag_true_allows_region(self, orch):
        task = Task(
            id="t-exec-1",
            project_id="p-1",
            title="Test",
            description="test",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        # True means legacy path allowed
        assert orch._should_run_legacy_plan_region(task) is True

    async def test_flag_false_blocks_region_for_in_progress(self, orch):
        orch.config.planner.legacy_plan_discovery = False
        task = Task(
            id="t-exec-2",
            project_id="p-1",
            title="Test",
            description="test",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        assert orch._should_run_legacy_plan_region(task) is False

    async def test_flag_false_but_drain_allows_region(self, orch):
        """Drain: AWAITING_PLAN_APPROVAL task always allowed on legacy."""
        orch.config.planner.legacy_plan_discovery = False
        task = Task(
            id="t-exec-3",
            project_id="p-1",
            title="Test",
            description="test",
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
        )
        await orch.db.create_task(task)
        assert orch._should_run_legacy_plan_region(task) is True


# ---------------------------------------------------------------------------
# Default preserved
# ---------------------------------------------------------------------------


def test_default_flag_is_true_do_not_flip():
    """Task 8 must NOT flip the config default — that's Task 9."""
    cfg = AppConfig()
    assert cfg.planner.legacy_plan_discovery is True
