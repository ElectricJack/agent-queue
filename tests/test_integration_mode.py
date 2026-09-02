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


def _arun_with_commits_ahead(count: int) -> AsyncMock:
    """Mock ``git._arun`` whose ``rev-list <default>..HEAD`` reports *count*.

    Every other invocation (auto-push's ``rev-list origin/<branch>..HEAD``,
    merges, pushes) keeps answering ``"0"`` like the fixture default.
    """

    async def _run(args, cwd=None, **_kw):
        if args and args[0] == "rev-list" and any(
            a in ("origin/main..HEAD", "main..HEAD") for a in args
        ):
            return str(count)
        return "0"

    return AsyncMock(side_effect=_run)


class TestExecutionRulesByMode:
    """_get_execution_rules varies the git instructions with the mode."""

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
        orch.git._arun = _arun_with_commits_ahead(2)
        ws = await orch.db.get_workspace("ws-1")
        ctx = _ctx(orch, task, ws.workspace_path)

        result = await orch._phase_verify(ctx)
        assert result == PhaseResult.STOP


class TestPhaseVerifyEmptyBranchInSlot:
    """A task that produced no commits can close under ``pull_request``.

    Regression for quick-meadow-14 / smart-quest-24: review, research and
    triage tasks never commit, so ``gh pr create`` refuses to open a PR for
    their branch (``No commits between main and <branch>``).  The old
    escape hatch -- "the workspace is on the default branch" -- cannot fire
    from a worktree slot, because the default branch is already checked out
    in the primary worktree (``fatal: 'main' is already used by worktree``).
    The gate now checks the condition that hatch approximated: the task
    branch has no commits ahead of the default branch.
    """

    async def _slot_ctx(self, orch, task_id, branch):
        task = _pr_task(task_id, branch_name=branch)
        await orch.db.create_task(task)
        await orch.db.transition_task(task.id, TaskStatus.IN_PROGRESS)
        # The slot stays on its task branch; it cannot check out ``main``.
        orch.git.aget_current_branch = AsyncMock(return_value=branch)
        orch.git.afind_open_pr = AsyncMock(return_value=None)
        ws = await orch.db.get_workspace("ws-1")
        return task, _ctx(orch, task, ws.workspace_path)

    async def test_no_commits_ahead_passes_without_a_pr(self, orch):
        task, ctx = await self._slot_ctx(orch, "t-review", "aq/t-review")
        orch.git._arun = _arun_with_commits_ahead(0)
        ctx.close_session_live = True

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        # The PR was looked up (a real PR would still be recorded) ...
        orch.git.afind_open_pr.assert_awaited_once()
        # ... and its absence is not a failure for an empty branch.
        assert ctx.verification_issues == []
        assert ctx.verification_retry_in_session is False
        assert ctx.pr_url is None
        assert (await orch.db.get_task(task.id)).status is TaskStatus.IN_PROGRESS

    async def test_commits_ahead_still_require_an_open_pr(self, orch):
        """No regression: real work on the branch keeps the PR requirement."""
        _task, ctx = await self._slot_ctx(orch, "t-work", "aq/t-work")
        orch.git._arun = _arun_with_commits_ahead(3)
        ctx.close_session_live = True

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert ctx.verification_retry_in_session is True
        assert any("No open PR" in msg for msg in ctx.verification_issues)
        orch.git.afind_open_pr.assert_awaited_once()

    async def test_commits_ahead_with_open_pr_passes(self, orch):
        _task, ctx = await self._slot_ctx(orch, "t-work-pr", "aq/t-work-pr")
        orch.git._arun = _arun_with_commits_ahead(3)
        orch.git.afind_open_pr = AsyncMock(return_value="https://github.com/org/repo/pull/9")

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE
        assert ctx.pr_url == "https://github.com/org/repo/pull/9"

    async def test_unknown_ahead_count_keeps_the_pr_requirement(self, orch):
        """If git cannot resolve the default branch, stay on the strict path."""
        from src.git.manager import GitError

        _task, ctx = await self._slot_ctx(orch, "t-unknown", "aq/t-unknown")

        async def _run(args, cwd=None, **_kw):
            if args and args[0] == "rev-list" and any("main..HEAD" in a for a in args):
                raise GitError("unknown revision")
            return "0"

        orch.git._arun = AsyncMock(side_effect=_run)
        ctx.close_session_live = True

        assert await orch._phase_verify(ctx) == PhaseResult.STOP
        assert any("No open PR" in msg for msg in ctx.verification_issues)

    async def test_falls_back_to_local_default_without_remote_ref(self, orch):
        """``origin/main`` missing locally is not a reason to demand a PR."""
        from src.git.manager import GitError

        _task, ctx = await self._slot_ctx(orch, "t-local", "aq/t-local")

        async def _run(args, cwd=None, **_kw):
            if args and args[0] == "rev-list":
                if "origin/main..HEAD" in args:
                    raise GitError("unknown revision origin/main")
                if "main..HEAD" in args:
                    return "0"
            return "0"

        orch.git._arun = AsyncMock(side_effect=_run)

        assert await orch._phase_verify(ctx) == PhaseResult.CONTINUE

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

    async def test_pushes_a_branch_with_commits(self, orch, monkeypatch):
        task = _pr_task("t-int-push")
        await orch.db.create_task(task)
        orch.git._arun = _arun_with_commits_ahead(2)
        result = await self._run_integrate(orch, task, monkeypatch)
        assert result == PhaseResult.CONTINUE
        orch.git.apush_branch.assert_awaited_once()

    async def test_empty_branch_is_not_pushed(self, orch, monkeypatch):
        """A no-commit task branch has nothing to publish; do not litter origin."""
        task = _pr_task("t-int-empty")
        await orch.db.create_task(task)
        orch.git._arun = _arun_with_commits_ahead(0)
        result = await self._run_integrate(orch, task, monkeypatch)
        assert result == PhaseResult.CONTINUE
        orch.git.apush_branch.assert_not_awaited()

    async def test_direct_mode_merges_into_default(self, orch, monkeypatch):
        task = _direct_task("t-int-direct")
        await orch.db.create_task(task)
        result = await self._run_integrate(orch, task, monkeypatch)
        assert result == PhaseResult.CONTINUE
        orch.git.amerge_branch.assert_awaited_once()


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
        # The branch carries real work -- a PR is genuinely required.
        orch.git._arun = _arun_with_commits_ahead(1)
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
