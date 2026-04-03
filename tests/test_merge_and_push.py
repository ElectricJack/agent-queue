"""Tests for _merge_and_push in the Orchestrator.

Now that CLONE repos delegate to ``sync_and_merge()``, these tests verify:
- The structured ``(success, error)`` return value is handled correctly.
- Merge-conflict and push-failure notifications include the right details.
- LINK repos still use the simpler ``merge_branch()`` path.
- Branch cleanup happens only on success.
- Workspace recovery resets the default branch after failures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.git.manager import GitManager
from src.models import RepoConfig, RepoSourceType, Task


def _make_task(**overrides) -> Task:
    defaults = dict(
        id="test-task",
        project_id="proj-1",
        title="Test Task",
        description="desc",
        branch_name="task/test-branch",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_repo(source_type=RepoSourceType.CLONE, **overrides) -> RepoConfig:
    defaults = dict(
        id="repo-1",
        project_id="proj-1",
        source_type=source_type,
        url="https://github.com/test/repo.git",
        default_branch="main",
    )
    defaults.update(overrides)
    return RepoConfig(**defaults)


class _FakeOrchestrator:
    """Minimal stand-in for Orchestrator to test _merge_and_push in isolation.

    We import the real method and bind it, rather than instantiating the full
    Orchestrator (which requires DB, config, adapters, etc.).
    """

    def __init__(self, git: GitManager):
        self.git = git
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    # Bind the real method from Orchestrator
    from src.orchestrator import Orchestrator as _Orch

    _merge_and_push = _Orch._merge_and_push


class TestMergeAndPushClone:
    """Tests for CLONE repos — delegates to sync_and_merge()."""

    @pytest.fixture
    def git(self):
        g = MagicMock(spec=GitManager)
        g.has_remote.return_value = True
        g.ahas_remote = AsyncMock(return_value=True)
        return g

    @pytest.fixture
    def orch(self, git):
        return _FakeOrchestrator(git)

    @pytest.mark.asyncio
    async def test_successful_merge_and_push(self, orch, git):
        """Happy path: sync_and_merge succeeds, branch is cleaned up."""
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        # Default _max_retries=3 → max_retries=2 passed to sync_and_merge
        git.async_and_merge.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
            max_retries=2,
        )
        git.adelete_branch.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            delete_remote=True,
        )
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_merge_conflict_notifies(self, orch, git):
        """Merge conflict should send notification suggesting manual resolution."""
        git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))
        git.arecover_workspace = AsyncMock()
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        git.adelete_branch.assert_not_called()
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]
        assert "task/test-branch" in orch._notifications[0]
        assert "Manual resolution" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_push_failed_notifies_with_details(self, orch, git):
        """Push failure should notify with details and divergence warning."""
        git.async_and_merge = AsyncMock(return_value=(False, "push_failed: rejected by remote"))
        git.arecover_workspace = AsyncMock()
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        git.adelete_branch.assert_not_called()
        assert len(orch._notifications) == 1
        msg = orch._notifications[0]
        assert "Push Failed" in msg
        assert "diverged" in msg
        assert "push_failed: rejected by remote" in msg

    @pytest.mark.asyncio
    async def test_push_failed_includes_attempt_count(self, orch, git):
        """Notification should include total attempt count from _max_retries."""
        git.async_and_merge = AsyncMock(return_value=(False, "push_failed: timeout"))
        git.arecover_workspace = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=5)

        assert len(orch._notifications) == 1
        assert "5 attempts" in orch._notifications[0]
        # max_retries = 5 - 1 = 4
        git.async_and_merge.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
            max_retries=4,
        )

    @pytest.mark.asyncio
    async def test_max_retries_maps_correctly(self, orch, git):
        """_max_retries=N should pass max_retries=N-1 to sync_and_merge."""
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=1)

        git.async_and_merge.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
            max_retries=0,
        )

    @pytest.mark.asyncio
    async def test_max_retries_zero_clamps(self, orch, git):
        """_max_retries=0 should clamp max_retries to 0 (no negative)."""
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=0)

        git.async_and_merge.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
            max_retries=0,
        )

    @pytest.mark.asyncio
    async def test_sync_and_merge_not_called_for_link(self, orch, git):
        """LINK repos should never call sync_and_merge."""
        git.ahas_remote = AsyncMock(return_value=False)
        git.amerge_branch = AsyncMock(return_value=True)
        git.adelete_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.async_and_merge = AsyncMock()
        git.async_and_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_conflict_resets_workspace(self, orch, git):
        """After merge conflict, workspace should be reset to origin state."""
        git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))
        git.arecover_workspace = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        # Recovery via recover_workspace
        git.arecover_workspace.assert_called_once_with("/workspace", "main")

    @pytest.mark.asyncio
    async def test_push_failed_resets_workspace(self, orch, git):
        """After push failure, workspace should be reset to origin state."""
        git.async_and_merge = AsyncMock(return_value=(False, "push_failed: rejected"))
        git.arecover_workspace = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        # Recovery via recover_workspace
        git.arecover_workspace.assert_called_once_with("/workspace", "main")

    @pytest.mark.asyncio
    async def test_recovery_uses_repo_default_branch(self, orch, git):
        """Recovery should use the repo's configured default branch, not 'main'."""
        git.async_and_merge = AsyncMock(return_value=(False, "push_failed: rejected"))
        git.arecover_workspace = AsyncMock()
        task = _make_task()
        repo = _make_repo(default_branch="develop")

        await orch._merge_and_push(task, repo, "/workspace")

        git.arecover_workspace.assert_called_once_with("/workspace", "develop")

    @pytest.mark.asyncio
    async def test_recovery_failure_silently_ignored(self, orch, git):
        """Recovery errors should be silently swallowed (best-effort)."""
        git.async_and_merge = AsyncMock(return_value=(False, "push_failed: rejected"))
        git.arecover_workspace = AsyncMock(side_effect=Exception("recovery failed"))
        task = _make_task()
        repo = _make_repo()

        # Should not raise despite recovery failure
        await orch._merge_and_push(task, repo, "/workspace")

        assert len(orch._notifications) == 1
        assert "Push Failed" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_success_does_not_trigger_recovery(self, orch, git):
        """Successful merge-and-push should NOT invoke recovery."""
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        git._arun = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        # _arun should not be called (recovery is only on failure)
        git._arun.assert_not_called()


class TestMergeAndPushLink:
    """Tests for LINK repos — uses merge_branch() directly, no push."""

    @pytest.fixture
    def git(self):
        g = MagicMock(spec=GitManager)
        g.has_remote.return_value = False
        g.ahas_remote = AsyncMock(return_value=False)
        return g

    @pytest.fixture
    def orch(self, git):
        return _FakeOrchestrator(git)

    @pytest.mark.asyncio
    async def test_link_repo_merges_locally(self, orch, git):
        """LINK repos should merge locally without pushing or retrying."""
        git.amerge_branch = AsyncMock(return_value=True)
        git.adelete_branch = AsyncMock()
        git.async_and_merge = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.amerge_branch.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
        )
        git.async_and_merge.assert_not_called()
        # Branch cleanup with delete_remote=False for LINK repos
        git.adelete_branch.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            delete_remote=False,
        )
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_link_repo_merge_conflict_notifies(self, orch, git):
        """LINK repo merge conflict should send notification."""
        git.amerge_branch = AsyncMock(return_value=False)
        git.arebase_onto = AsyncMock(return_value=False)
        git.adelete_branch = AsyncMock()
        git._arun = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.adelete_branch.assert_not_called()
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]
        assert "task/test-branch" in orch._notifications[0]
        assert "Manual resolution" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_link_repo_delete_branch_failure_ignored(self, orch, git):
        """Branch cleanup failure on LINK repos should be silently ignored."""
        git.amerge_branch = AsyncMock(return_value=True)
        git.adelete_branch = AsyncMock(side_effect=Exception("branch not found"))
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        # Should not raise
        await orch._merge_and_push(task, repo, "/workspace")

        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_link_repo_conflict_recovery_checks_out_default(self, orch, git):
        """LINK repo merge conflict recovery should checkout default branch."""
        git.amerge_branch = AsyncMock(return_value=False)
        git.arebase_onto = AsyncMock(return_value=False)
        git._arun = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        # Recovery: checkout default branch (no hard reset — LINK has no origin)
        git._arun.assert_any_call(["checkout", "main"], cwd="/workspace")
        # Should NOT attempt hard reset to origin (LINK repos have no remote)
        reset_calls = [c for c in git._arun.call_args_list if c[0][0][:2] == ["reset", "--hard"]]
        assert len(reset_calls) == 0

    @pytest.mark.asyncio
    async def test_link_repo_conflict_recovery_failure_ignored(self, orch, git):
        """LINK repo recovery errors should be silently swallowed."""
        git.amerge_branch = AsyncMock(return_value=False)
        git.arebase_onto = AsyncMock(return_value=False)
        git._arun = AsyncMock(side_effect=Exception("checkout failed"))
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        # Should not raise
        await orch._merge_and_push(task, repo, "/workspace")

        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_link_repo_success_does_not_trigger_recovery(self, orch, git):
        """Successful LINK merge should NOT invoke recovery."""
        git.amerge_branch = AsyncMock(return_value=True)
        git.adelete_branch = AsyncMock()
        git._arun = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        # _arun should not be called (no recovery needed on success)
        git._arun.assert_not_called()
