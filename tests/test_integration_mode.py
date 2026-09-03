"""Integration-mode policy tests.

Started as characterization tests for the legacy ``requires_approval`` flag
(session close, ``_phase_verify``, ``_phase_integrate``, execution rules) and
now pin the replacement ``integration_mode`` contract: the git pipeline
branches on the *effective* integration mode (task override → project policy
→ config default), PR-mode work is never auto-merged, and direct integration
is available only through explicit policy.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentProfile,
    AgentOutput,
    AgentResult,
    PhaseResult,
    PipelineContext,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator


class _NullRuntimeFactory:
    def create(self, agent_type, profile=None, llm_logger=None):
        raise AssertionError("no runtime should be created in these tests")


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    config.worktrees.enabled = False
    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
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
    mock_git.ais_ancestor = AsyncMock(return_value=False)
    # Default: the task branch carries work, so the PR gate applies.
    mock_git.acount_commits_ahead = AsyncMock(return_value=1)
    mock_git._arun = AsyncMock(return_value="0")
    mock_git.acommit_all = AsyncMock(return_value=True)
    mock_git.apush_branch = AsyncMock(return_value=None)
    mock_git.amerge_branch = AsyncMock(return_value=True)
    mock_git.aabort_in_progress_operations = AsyncMock()
    mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
    mock_git.aget_remote_url = AsyncMock(return_value="")
    o.git = mock_git

    yield o
    if o._running_tasks:
        import asyncio

        await asyncio.gather(*o._running_tasks.values(), return_exceptions=True)
        o._running_tasks.clear()
    await o.shutdown()


def _pr_task(task_id: str = "t-pr", **kw) -> Task:
    kw.setdefault("branch_name", "feature-1")
    kw.setdefault("status", TaskStatus.IN_PROGRESS)
    return Task(
        id=task_id,
        project_id="p-1",
        title="PR task",
        description="pr characterization",
        integration_mode="pull_request",
        **kw,
    )


def _direct_task(task_id: str = "t-direct", **kw) -> Task:
    kw.setdefault("branch_name", "feature-1")
    kw.setdefault("status", TaskStatus.IN_PROGRESS)
    return Task(
        id=task_id,
        project_id="p-1",
        title="direct task",
        description="direct characterization",
        integration_mode="direct",
        **kw,
    )


def _ctx(orch, task, ws_path) -> PipelineContext:
    return PipelineContext(
        task=task,
        agent=Agent(id="a-1", name="claude-1", profile_id="claude"),
        output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=10),
        workspace_path=ws_path,
        workspace_id="ws-1",
        repo=RepoConfig(
            id="r-1", project_id="p-1", source_type=RepoSourceType.LINK, default_branch="main"
        ),
        default_branch="main",
    )


class TestExecutionRulesByMode:
    """_get_execution_rules varies the git instructions with the mode."""

    def test_orchestrator_constructs_without_a_session_command_import_cycle(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "x.db"),
            workspace_dir=str(tmp_path / "w"),
            data_dir=str(tmp_path / "d"),
        )

        orchestrator = Orchestrator(config, runtimes=_NullRuntimeFactory())

        assert orchestrator.session_reconciler is not None

    def test_pr_mode_instructs_push_and_pr_never_merge(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "x.db"),
            workspace_dir=str(tmp_path / "w"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(config, runtimes=_NullRuntimeFactory())
        rules = o._get_execution_rules(
            task=_pr_task(),
            branch_name="feature-1",
            default_branch="main",
            has_remote=True,
            is_final_subtask=True,
            integration_mode="pull_request",
        )
        assert "gh pr create" in rules
        assert "do NOT merge" in rules

    def test_direct_mode_instructs_merge_to_default(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "x.db"),
            workspace_dir=str(tmp_path / "w"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(config, runtimes=_NullRuntimeFactory())
        rules = o._get_execution_rules(
            task=_direct_task(),
            branch_name="feature-1",
            default_branch="main",
            has_remote=True,
            is_final_subtask=True,
            integration_mode="direct",
        )
        assert "git merge feature-1" in rules
        assert "gh pr create" not in rules


class TestPhaseVerifyByMode:
    async def test_pr_mode_requires_open_pr_and_never_merges(self, orch):
        task = _pr_task()
        await orch.db.create_task(task)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        assert ctx.pr_url == "https://github.com/org/repo/pull/42"
        # The PR-mode branch must never be merged into the default branch.
        merge_calls = [
            c for c in orch.git._arun.await_args_list if c.args and "merge" in c.args[0]
        ]
        assert merge_calls == []

    async def test_pr_mode_stops_without_open_pr(self, orch):
        task = _pr_task("t-pr-nopr", branch_name="feature-2")
        await orch.db.create_task(task)
        orch.git.aget_current_branch = AsyncMock(return_value="feature-2")
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP
        orch.git.ais_ancestor.assert_awaited_once_with(
            ws.workspace_path,
            "feature-2",
            "origin/main",
        )

    async def test_direct_mode_auto_merges_to_default(self, orch):
        task = _direct_task()
        await orch.db.create_task(task)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.CONTINUE
        merge_calls = [
            c for c in orch.git._arun.await_args_list if c.args and "merge" in c.args[0]
        ]
        assert merge_calls, "direct mode should auto-merge the task branch into default"


class TestPhaseIntegrateByMode:
    """_phase_integrate merges into default only in direct mode."""

    async def _run_integrate(self, orch, task, monkeypatch):
        from src.orchestrator import git_ops

        monkeypatch.setattr(git_ops, "acquire_merge_slot", AsyncMock(return_value=True))
        monkeypatch.setattr(git_ops, "renew_merge_slot", AsyncMock(return_value=True))
        monkeypatch.setattr(git_ops, "release_merge_slot", AsyncMock(return_value=None))
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)
        orch.git.aget_current_branch = AsyncMock(return_value=task.branch_name)
        return await orch._phase_integrate(ctx)

    async def test_pr_mode_skips_base_merge(self, orch, monkeypatch):
        task = _pr_task("t-int-pr")
        await orch.db.create_task(task)
        result = await self._run_integrate(orch, task, monkeypatch)
        assert result == PhaseResult.CONTINUE
        orch.git.amerge_branch.assert_not_awaited()

    async def test_direct_mode_merges_into_default(self, orch, monkeypatch):
        task = _direct_task("t-int-direct")
        await orch.db.create_task(task)
        result = await self._run_integrate(orch, task, monkeypatch)
        assert result == PhaseResult.CONTINUE
        orch.git.amerge_branch.assert_awaited_once()


class TestEmptyBranchSkipsIntegration:
    """A zero-commit branch has nothing to integrate either.

    ``_phase_verify`` already lets a review-only task close without a PR.
    ``_phase_integrate`` used to take the project's merge slot anyway and
    force-push the empty branch, littering the remote with branches that
    carry no commits and serialising real integrations behind a no-op.
    """

    async def _run_integrate(self, orch, task, monkeypatch, ahead):
        from src.orchestrator import git_ops

        acquire = AsyncMock(return_value=True)
        monkeypatch.setattr(git_ops, "acquire_merge_slot", acquire)
        monkeypatch.setattr(git_ops, "renew_merge_slot", AsyncMock(return_value=True))
        monkeypatch.setattr(git_ops, "release_merge_slot", AsyncMock(return_value=None))
        orch.git.acount_commits_ahead = AsyncMock(return_value=ahead)
        orch.git.aget_current_branch = AsyncMock(return_value=task.branch_name)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)
        return await orch._phase_integrate(ctx), acquire

    async def test_empty_branch_never_takes_the_merge_slot(self, orch, monkeypatch):
        task = _pr_task("t-int-empty")
        await orch.db.create_task(task)

        result, acquire = await self._run_integrate(orch, task, monkeypatch, ahead=0)

        assert result == PhaseResult.CONTINUE
        acquire.assert_not_awaited()
        orch.git.apush_branch.assert_not_awaited()
        orch.git.amerge_branch.assert_not_awaited()

    async def test_direct_mode_empty_branch_is_not_merged(self, orch, monkeypatch):
        task = _direct_task("t-int-empty-direct")
        await orch.db.create_task(task)

        result, acquire = await self._run_integrate(orch, task, monkeypatch, ahead=0)

        assert result == PhaseResult.CONTINUE
        acquire.assert_not_awaited()
        orch.git.amerge_branch.assert_not_awaited()

    async def test_a_branch_with_commits_still_integrates(self, orch, monkeypatch):
        task = _direct_task("t-int-ahead")
        await orch.db.create_task(task)

        result, acquire = await self._run_integrate(orch, task, monkeypatch, ahead=3)

        assert result == PhaseResult.CONTINUE
        acquire.assert_awaited_once()
        orch.git.amerge_branch.assert_awaited_once()

    async def test_an_unanswerable_count_still_integrates(self, orch, monkeypatch):
        """Unknown is not empty — keep integrating rather than skip blind."""
        task = _direct_task("t-int-unknown")
        await orch.db.create_task(task)

        result, acquire = await self._run_integrate(orch, task, monkeypatch, ahead=None)

        assert result == PhaseResult.CONTINUE
        acquire.assert_awaited_once()


class TestSessionCloseCompletion:
    """Session close marks the worker task COMPLETED; PR mode carries pr_url."""

    async def test_pass_with_pr_completes_and_reports_pr_url(self, orch):
        task = _pr_task("t-close-pr", status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        orch._run_completion_pipeline = AsyncMock(
            return_value=("https://github.com/org/repo/pull/7", True)
        )

        result = await orch.complete_session_task(task, outcome="pass", notes="done")
        assert result["status"] == TaskStatus.COMPLETED.value
        assert result["pr_url"] == "https://github.com/org/repo/pull/7"
        refreshed = await orch.db.get_task(task.id)
        assert refreshed.status == TaskStatus.COMPLETED

    async def test_pass_with_pipeline_stop_blocks(self, orch):
        task = _pr_task("t-close-stop", status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(task)
        orch._run_completion_pipeline = AsyncMock(return_value=(None, False))

        result = await orch.complete_session_task(task, outcome="pass", notes="done")
        assert result["status"] == TaskStatus.BLOCKED.value


class TestResolveIntegrationMode:
    """The pure policy chain: parent → task → project → config default."""

    def test_default_is_pull_request(self):
        from src.models import resolve_integration_mode

        assert resolve_integration_mode(None) == "pull_request"

    def test_chain_precedence(self):
        from src.models import resolve_integration_mode_with_source

        assert resolve_integration_mode_with_source(
            "direct", parent_task_mode="pull_request", project_mode="direct"
        ) == ("pull_request", "parent")
        assert resolve_integration_mode_with_source(
            "direct", project_mode="pull_request"
        ) == ("direct", "task")
        assert resolve_integration_mode_with_source(
            None, project_mode="direct"
        ) == ("direct", "project")
        assert resolve_integration_mode_with_source(
            None, default_mode="direct"
        ) == ("direct", "default")

    def test_unknown_values_fall_through(self):
        from src.models import resolve_integration_mode

        assert (
            resolve_integration_mode("bogus", project_mode="???", default_mode="junk")
            == "pull_request"
        )

    async def test_effective_mode_uses_project_policy(self, orch):
        """A task with no override inherits the project's mode."""
        await orch.db.update_project("p-1", integration_mode="direct")
        task = Task(
            id="t-inherit",
            project_id="p-1",
            title="inherit",
            description="",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        assert await orch._effective_integration_mode(task) == "direct"

    async def test_effective_mode_defaults_to_pull_request(self, orch):
        """No task/project policy → config default (pull_request): worker
        output is never auto-merged unless direct mode was chosen somewhere
        explicitly."""
        task = Task(
            id="t-default",
            project_id="p-1",
            title="default",
            description="",
            status=TaskStatus.IN_PROGRESS,
        )
        await orch.db.create_task(task)
        assert await orch._effective_integration_mode(task) == "pull_request"


class TestVerificationRetryKeepsTheSessionAlive:
    """Regression cover for the close/verification loop killing its own worker.

    Timeline that produced this (noble-pinnacle, agent-queue.log 2026-09-02):
    ``01:35:40 git verification failed (1 fixable issues): No open PR found``
    → ``01:35:41 reopened for verification (attempt 1/2)`` (task set READY)
    → ``01:35:46 Session ... is live but task ... is READY — draining``.

    The close path asked the worker to push and open the PR, and five
    seconds later the session reconciler's orphan rule ("live session, task
    not IN_PROGRESS") killed that same worker.  Since the PR-mode cutover
    that fired for every task whose first close lacked a PR.

    The fix is that a close issued *by a live session* keeps the task
    IN_PROGRESS under its claim and hands the fixable issues back through
    the ``task_close`` response instead of reopening.
    """

    async def _pr_missing_ctx(self, orch, task_id, branch):
        task = _pr_task(task_id, branch_name=branch)
        await orch.db.create_task(task)
        await orch.db.transition_task(task.id, TaskStatus.IN_PROGRESS)
        orch.git.aget_current_branch = AsyncMock(return_value=branch)
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        ws = await orch.db.get_workspace("ws-1")
        return task, _ctx(orch, task, ws.workspace_path)

    async def test_live_session_keeps_the_task_in_progress(self, orch):
        task, ctx = await self._pr_missing_ctx(orch, "t-pr-live", "feature-live")
        ctx.close_session_live = True

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert ctx.verification_retry_in_session is True
        assert ctx.verification_reopened is False
        assert any("No open PR" in msg for msg in ctx.verification_issues)
        assert "close" in ctx.verification_feedback

        row = await orch.db.get_task(task.id)
        # The whole point: the reconciler's orphan rule only drains a live
        # session whose task left IN_PROGRESS/ASSIGNED.
        assert row.status is TaskStatus.IN_PROGRESS
        assert "Git Verification Feedback" in row.description

    async def test_without_a_live_session_it_still_reopens_to_ready(self, orch):
        """A local/elevated close has no agent to hand the issues to."""
        task, ctx = await self._pr_missing_ctx(orch, "t-pr-nolive", "feature-nolive")
        ctx.close_session_live = False

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert ctx.verification_reopened is True
        assert ctx.verification_retry_in_session is False
        assert (await orch.db.get_task(task.id)).status is TaskStatus.READY

    async def test_in_session_retries_are_bounded_by_the_same_budget(self, orch):
        """Exhausting the budget still ends in the terminal (blocking) branch."""
        task, ctx = await self._pr_missing_ctx(orch, "t-pr-spent", "feature-spent")
        for _ in range(orch.config.auto_task.max_verification_retries):
            await orch.db.add_task_context(
                task.id,
                type="verification_feedback",
                label="Git Verification Feedback",
                content="previous attempt",
            )
        ctx.close_session_live = True

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert ctx.verification_retry_in_session is False
        assert ctx.verification_reopened is False
        # Left IN_PROGRESS for the caller to block — _phase_verify itself
        # never transitions on the exhausted leg.
        assert (await orch.db.get_task(task.id)).status is TaskStatus.IN_PROGRESS


class TestNoCodeTasksSkipThePrGate:
    """Regression cover for read-only tasks tripping the require-a-PR gate.

    Timeline (review task keen-willow, reviewing PR #76): the reviewer
    profile says "Never edit code. Your workspace is read-only." — it makes
    no commits and opens no PR on its own ``aq/<id>`` branch.  Yet
    ``aq task close --outcome pass`` was refused twice with ``No open PR
    found for branch 'aq/keen-willow'``, burning both verification retries
    and appending misleading feedback to the review task, and the third
    close landed BLOCKED / ``pipeline_ok=false``.

    A task that produces no code has nothing to push, PR or merge: the
    reviewer / final-reviewer stage profiles, and any task closed with
    ``--work-outcome no-op``, pass git verification (and skip integration)
    outright.
    """

    async def _no_pr_ctx(self, orch, task_id, branch, **task_kw):
        task = _pr_task(task_id, branch_name=branch, **task_kw)
        await orch.db.create_task(task)
        await orch.db.transition_task(task.id, TaskStatus.IN_PROGRESS)
        orch.git.aget_current_branch = AsyncMock(return_value=branch)
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)
        ctx.close_session_live = True
        return task, ctx

    async def _assert_clean_pass(self, orch, task, ctx):
        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        assert ctx.verification_retry_in_session is False
        assert ctx.verification_reopened is False
        assert ctx.verification_issues == []
        # The PR lookup is the whole gate — a no-code task never consults it.
        orch.git.afind_open_pr.assert_not_awaited()
        row = await orch.db.get_task(task.id)
        assert row.status is TaskStatus.IN_PROGRESS
        assert "Git Verification Feedback" not in row.description
        contexts = await orch.db.get_task_contexts(task.id)
        assert [c for c in contexts if c.get("type") == "verification_feedback"] == []

    async def test_reviewer_profile_closes_without_a_pr(self, orch):
        task, ctx = await self._no_pr_ctx(
            orch, "t-review", "aq/t-review", profile_id="reviewer"
        )
        await self._assert_clean_pass(orch, task, ctx)

    async def test_final_reviewer_profile_closes_without_a_pr(self, orch):
        task, ctx = await self._no_pr_ctx(
            orch, "t-final", "aq/t-final", profile_id="final-reviewer"
        )
        await self._assert_clean_pass(orch, task, ctx)

    async def test_no_op_work_outcome_closes_without_a_pr(self, orch):
        """Any profile: ``aq task close --work-outcome no-op`` means no code."""
        task, ctx = await self._no_pr_ctx(orch, "t-noop", "aq/t-noop")
        ctx.work_outcome = "no-op"
        await self._assert_clean_pass(orch, task, ctx)

    async def test_custom_read_only_profile_closes_without_a_pr(self, orch):
        """A custom profile's declarative read-only flag skips the gate."""
        await orch.db.create_profile(
            AgentProfile(id="custom-review", name="custom-review", read_only=True)
        )
        task, ctx = await self._no_pr_ctx(
            orch, "t-custom-review", "aq/t-custom-review", profile_id="custom-review"
        )
        await self._assert_clean_pass(orch, task, ctx)

    async def test_shipped_worker_still_needs_a_pr(self, orch):
        """The gate itself is unchanged for tasks that claim to have shipped."""
        _task, ctx = await self._no_pr_ctx(
            orch, "t-shipped", "aq/t-shipped"
        )
        ctx.work_outcome = "shipped"
        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert ctx.verification_retry_in_session is True
        assert any("No open PR" in msg for msg in ctx.verification_issues)

    async def test_no_code_task_still_sweeps_uncommitted_changes(self, orch, monkeypatch):
        """Skipping the gate must not leave a dirty slot for the next task."""
        task, ctx = await self._no_pr_ctx(orch, "t-dirty", "aq/t-dirty", profile_id="reviewer")
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        sweep = AsyncMock(return_value=False)
        monkeypatch.setattr(orch, "_auto_remediate_uncommitted", sweep)

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        sweep.assert_awaited_once()
        assert sweep.await_args.args[1] == task.id
        assert sweep.await_args.args[2] == "aq/t-dirty"

    async def test_no_code_task_skips_integration(self, orch, monkeypatch):
        """Nothing to rebase, push or merge — integrate is not run either.

        Without this the worktree integrate phase force-pushes an empty
        ``aq/<id>`` branch to origin for every review.
        """
        _task, ctx = await self._no_pr_ctx(orch, "t-int", "aq/t-int", profile_id="reviewer")
        monkeypatch.setattr(orch, "_task_is_worktree_mode", AsyncMock(return_value=True))
        integrate = AsyncMock(return_value=PhaseResult.CONTINUE)
        monkeypatch.setattr(orch, "_phase_integrate", integrate)

        assert await orch._run_completion_pipeline(ctx) == (None, True)
        integrate.assert_not_awaited()

    async def test_shipped_worktree_task_still_integrates(self, orch, monkeypatch):
        _task, ctx = await self._no_pr_ctx(orch, "t-int-ship", "aq/t-int-ship")
        orch.git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/9")
        monkeypatch.setattr(orch, "_task_is_worktree_mode", AsyncMock(return_value=True))
        integrate = AsyncMock(return_value=PhaseResult.CONTINUE)
        monkeypatch.setattr(orch, "_phase_integrate", integrate)

        assert await orch._run_completion_pipeline(ctx) == (
            "https://github.com/org/repo/pull/9",
            True,
        )
        integrate.assert_awaited_once()


class TestEmptyBranchSkipsThePrGate:
    """Regression cover for prime-beacon: a task branch with zero commits.

    A review-only task runs in a worktree pinned to its own ``aq/<id>``
    branch, so ``current_branch != default_branch`` and the pr_mode leg of
    ``_phase_verify`` demanded a PR — ``No open PR found for branch
    aq/stark-grove`` — even though the agent committed nothing.  The only
    ways out were pushing an empty PR or manually checking out main.

    Nothing to push means nothing to PR: a clean tree on a branch with no
    commits ahead of the default branch passes verification.  The check is
    independent of the profile/work-outcome escape hatches in
    :class:`TestNoCodeTasksSkipThePrGate` — it catches a *shipped* worker
    that turned out to change nothing too.
    """

    async def _empty_branch_ctx(self, orch, task_id, branch, ahead=0):
        task = _pr_task(task_id, branch_name=branch)
        await orch.db.create_task(task)
        await orch.db.transition_task(task.id, TaskStatus.IN_PROGRESS)
        orch.git.aget_current_branch = AsyncMock(return_value=branch)
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        orch.git.acount_commits_ahead = AsyncMock(return_value=ahead)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)
        ctx.close_session_live = True
        ctx.work_outcome = "shipped"
        return task, ctx

    async def test_clean_empty_branch_closes_without_a_pr(self, orch):
        task, ctx = await self._empty_branch_ctx(orch, "t-empty", "aq/t-empty")

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        assert ctx.verification_issues == []
        assert ctx.verification_reopened is False
        # The gate is skipped before the PR lookup — no gh call at all.
        orch.git.afind_open_pr.assert_not_awaited()
        assert (await orch.db.get_task(task.id)).status is TaskStatus.IN_PROGRESS

    async def test_the_remote_default_is_asked_first(self, orch):
        """A stale local default must not make an empty branch look busy."""
        await self._empty_branch_ctx(orch, "t-empty-base", "aq/t-empty-base")
        ctx = _ctx(orch, await orch.db.get_task("t-empty-base"), "/tmp")

        assert await orch._abranch_has_no_commits(ctx.workspace_path, "aq/x", "main")
        orch.git.acount_commits_ahead.assert_awaited_once_with(
            ctx.workspace_path, "aq/x", "origin/main"
        )

    async def test_it_falls_back_to_the_local_default(self, orch):
        """No ``origin/main`` ref (count unknown) → ask the local default."""
        orch.git.acount_commits_ahead = AsyncMock(side_effect=[None, 0])

        assert await orch._abranch_has_no_commits("/tmp", "aq/x", "main")
        assert [c.args[2] for c in orch.git.acount_commits_ahead.await_args_list] == [
            "origin/main",
            "main",
        ]

    async def test_an_unanswerable_count_keeps_the_gate(self, orch):
        """Both lookups failing means "unknown", not "empty"."""
        orch.git.acount_commits_ahead = AsyncMock(return_value=None)

        assert await orch._abranch_has_no_commits("/tmp", "aq/x", "main") is False

    async def test_a_branch_with_commits_still_needs_a_pr(self, orch):
        _task, ctx = await self._empty_branch_ctx(
            orch, "t-ahead", "aq/t-ahead", ahead=2
        )

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert any("No open PR" in msg for msg in ctx.verification_issues)

    async def test_a_dirty_empty_branch_still_needs_a_pr(self, orch, monkeypatch):
        """Uncommitted work is work — the agent has to commit and PR it."""
        _task, ctx = await self._empty_branch_ctx(orch, "t-dirty-empty", "aq/t-dirty-empty")
        orch.git.ahas_uncommitted_changes = AsyncMock(return_value=True)
        # Auto-remediation cannot rescue it either.
        monkeypatch.setattr(orch, "_auto_remediate_uncommitted", AsyncMock(return_value=True))

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert any("uncommitted" in msg.lower() for msg in ctx.verification_issues)


class TestEmptyBranchIsFlaggedNoCode:
    """The completion pipeline records whether the branch carried any work.

    Third guard against the review pipeline reviewing nothing (task
    bright-forge-78): ``_task_produces_no_code`` asks about intent and the
    pipeline's ``review_task`` key asks about provenance, but a reviewer with
    a rewritten profile and a custom dedup key dodges both.  The branch
    itself cannot lie — zero commits ahead of the base means an empty diff.

    The verdict has to be taken *before* integration, which merges the branch
    into the default and would then make real work look empty.
    """

    async def _run_pipeline(self, orch, task, monkeypatch, *, ahead, worktree=False):
        await orch.db.create_task(task)
        orch.git.acount_commits_ahead = AsyncMock(return_value=ahead)
        orch.git.aget_current_branch = AsyncMock(return_value=task.branch_name)
        orch.git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/1")
        monkeypatch.setattr(orch, "_task_is_worktree_mode", AsyncMock(return_value=worktree))
        monkeypatch.setattr(
            orch, "_phase_integrate", AsyncMock(return_value=PhaseResult.CONTINUE)
        )
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)
        ctx.work_outcome = "shipped"
        assert await orch._run_completion_pipeline(ctx) == (ctx.pr_url, True)
        return ctx

    async def test_pr_mode_empty_branch_is_flagged(self, orch, monkeypatch):
        ctx = await self._run_pipeline(orch, _pr_task("t-flag-empty"), monkeypatch, ahead=0)
        assert ctx.branch_no_commits is True

    async def test_pr_mode_branch_with_commits_is_not_flagged(self, orch, monkeypatch):
        ctx = await self._run_pipeline(orch, _pr_task("t-flag-ahead"), monkeypatch, ahead=4)
        assert ctx.branch_no_commits is False

    async def test_worktree_mode_empty_branch_is_flagged(self, orch, monkeypatch):
        """Non-PR worktree slots are asked too — integration merges later."""
        ctx = await self._run_pipeline(
            orch, _direct_task("t-flag-wt"), monkeypatch, ahead=0, worktree=True
        )
        assert ctx.branch_no_commits is True

    async def test_the_legacy_direct_path_is_never_asked(self, orch, monkeypatch):
        """Verification already merged the branch into the default there.

        Counting commits ahead after that auto-merge returns zero for a
        branch that shipped real work, so the question is not asked at all
        and the review keeps firing.
        """
        ctx = await self._run_pipeline(
            orch, _direct_task("t-flag-direct"), monkeypatch, ahead=0, worktree=False
        )
        assert ctx.branch_no_commits is False

    async def test_an_unanswerable_count_is_not_flagged(self, orch, monkeypatch):
        """Unknown is not empty — never disarm a review on a failed lookup."""
        ctx = await self._run_pipeline(
            orch, _pr_task("t-flag-unknown"), monkeypatch, ahead=None
        )
        assert ctx.branch_no_commits is False

    async def test_a_git_failure_is_not_flagged(self, orch, monkeypatch):
        task = _pr_task("t-flag-raises")
        await orch.db.create_task(task)
        orch.git.acount_commits_ahead = AsyncMock(side_effect=RuntimeError("git exploded"))
        orch.git.aget_current_branch = AsyncMock(return_value=task.branch_name)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)

        assert await orch._branch_left_no_commits(ctx) is False

    async def test_a_task_without_a_branch_is_not_asked(self, orch):
        task = _pr_task("t-flag-nobranch", branch_name=None)
        ctx = _ctx(orch, task, "/tmp")

        assert await orch._branch_left_no_commits(ctx) is False
        orch.git.acount_commits_ahead.assert_not_awaited()
