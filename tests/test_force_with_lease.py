"""Tests for --force-with-lease support in push_branch and _create_pr_for_task.

Phase 5 of the workspace-sync plan: task branches (used for PRs) should be
pushed with ``--force-with-lease`` so that retried tasks and subtask chains
that previously pushed intermediate results don't fail with non-fast-forward
errors.

Coverage:
  - ``push_branch(force_with_lease=False)`` (default) uses plain push.
  - ``push_branch(force_with_lease=True)`` uses ``--force-with-lease``.
  - ``_create_pr_for_task()`` calls ``push_branch`` with ``force_with_lease=True``.
  - Integration test: push succeeds on a previously-pushed branch when
    force_with_lease is enabled, and fails without it.
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.git.manager import GitError, GitManager
from src.models import RepoConfig, RepoSourceType, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_commit(cwd: str, filename: str, content: str, message: str) -> str:
    pathlib.Path(cwd, filename).write_text(content)
    _git(["add", filename], cwd=cwd)
    _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message], cwd=cwd)
    return _git(["rev-parse", "HEAD"], cwd=cwd)


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote + working clone for testing."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    (clone / "README.md").write_text("init")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "init"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    return {"remote": str(remote), "clone": str(clone)}


# ---------------------------------------------------------------------------
# Unit tests: push_branch argument handling
# ---------------------------------------------------------------------------


class TestPushBranchForceWithLease:
    """Verify push_branch constructs the correct git command."""

    def test_default_push_no_force_flag(self, git_repo):
        """Without force_with_lease, push_branch uses plain 'git push origin <branch>'."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create and push a branch so we have something valid
        _git(["checkout", "-b", "feature/plain-push"], cwd=clone)
        _git_commit(clone, "file.txt", "content", "add file")

        mgr.push_branch(clone, "feature/plain-push")

        # Verify branch exists on remote
        remote_branches = _git(["branch", "-r"], cwd=clone)
        assert "origin/feature/plain-push" in remote_branches

    def test_force_with_lease_push(self, git_repo):
        """With force_with_lease=True, push_branch uses --force-with-lease."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create, commit, and push a branch
        _git(["checkout", "-b", "feature/force-lease"], cwd=clone)
        _git_commit(clone, "file.txt", "v1", "first push")
        mgr.push_branch(clone, "feature/force-lease")

        # Amend the commit (creates a non-fast-forward divergence)
        pathlib.Path(clone, "file.txt").write_text("v2")
        _git(["add", "file.txt"], cwd=clone)
        _git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@t.com",
                "commit",
                "--amend",
                "-m",
                "amended push",
            ],
            cwd=clone,
        )

        # Plain push would fail; force-with-lease should succeed
        mgr.push_branch(clone, "feature/force-lease", force_with_lease=True)

        # Verify remote has the updated content
        remote_sha = _git(
            ["rev-parse", "origin/feature/force-lease"],
            cwd=clone,
        )
        local_sha = _git(["rev-parse", "HEAD"], cwd=clone)
        assert remote_sha == local_sha

    def test_plain_push_fails_on_rewritten_history(self, git_repo):
        """Plain push (no force) should fail after history rewrite."""
        mgr = GitManager()
        clone = git_repo["clone"]

        _git(["checkout", "-b", "feature/no-force"], cwd=clone)
        _git_commit(clone, "file.txt", "v1", "first push")
        mgr.push_branch(clone, "feature/no-force")

        # Rewrite history via --amend so local and remote diverge
        pathlib.Path(clone, "file.txt").write_text("v2")
        _git(["add", "file.txt"], cwd=clone)
        _git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@t.com",
                "commit",
                "--amend",
                "-m",
                "amended commit",
            ],
            cwd=clone,
        )

        with pytest.raises(GitError):
            mgr.push_branch(clone, "feature/no-force")

    def test_force_with_lease_fails_if_remote_updated_by_other(
        self,
        git_repo,
        tmp_path,
    ):
        """force-with-lease should fail if another clone pushed to the branch."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Push a branch from clone 1
        _git(["checkout", "-b", "feature/contested"], cwd=clone)
        _git_commit(clone, "file.txt", "v1", "clone1 push")
        mgr.push_branch(clone, "feature/contested")

        # Clone 2 pushes a different commit to the same branch
        clone2 = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True,
            capture_output=True,
        )
        _git(["checkout", "feature/contested"], cwd=clone2)
        _git_commit(clone2, "file.txt", "v2-from-clone2", "clone2 push")
        _git(["push", "origin", "feature/contested"], cwd=clone2)

        # Now clone 1 rewrites its history and tries force-with-lease
        _git_commit(clone, "file.txt", "v2-from-clone1", "clone1 rewrite")

        # Should fail because the remote ref was updated by clone2
        with pytest.raises(GitError):
            mgr.push_branch(
                clone,
                "feature/contested",
                force_with_lease=True,
            )

    def test_force_with_lease_keyword_only(self):
        """force_with_lease must be a keyword argument."""
        mgr = GitManager()
        # This should not be callable as a positional arg — verify the
        # signature by checking the parameter is keyword-only.
        import inspect

        sig = inspect.signature(mgr.push_branch)
        param = sig.parameters["force_with_lease"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# Orchestrator: _create_pr_for_task uses force_with_lease
# ---------------------------------------------------------------------------


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
    """Minimal stand-in for testing _create_pr_for_task in isolation."""

    def __init__(self, git: GitManager):
        self.git = git
        self.bus = MagicMock()
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    async def _emit_notify(self, event_type: str, event) -> None:
        self._notifications.append(event_type)

    async def _emit_text_notify(self, message: str, project_id: str | None = None) -> None:
        self._notifications.append(message)

    from src.orchestrator import Orchestrator as _Orch

    _create_pr_for_task = _Orch._create_pr_for_task


class TestCreatePrForTaskForceWithLease:
    """Verify _create_pr_for_task pushes with force_with_lease=True."""

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
    async def test_push_uses_force_with_lease(self, orch, git):
        """_create_pr_for_task should push with force_with_lease=True."""
        git.acreate_pr = AsyncMock(return_value="https://github.com/test/repo/pull/42")
        git.apush_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo()

        result = await orch._create_pr_for_task(task, repo, "/workspace")

        git.apush_branch.assert_called_once()
        call_args = git.apush_branch.call_args
        assert call_args[0] == ("/workspace", "task/test-branch")
        assert call_args[1].get("force_with_lease") is True
        assert result == "https://github.com/test/repo/pull/42"

    @pytest.mark.asyncio
    async def test_push_failure_notifies(self, orch, git):
        """Push failure should send notification and return None."""
        git.apush_branch = AsyncMock(side_effect=GitError("push rejected"))
        task = _make_task()
        repo = _make_repo()

        result = await orch._create_pr_for_task(task, repo, "/workspace")

        assert result is None
        assert len(orch._notifications) == 1
        assert "notify.push_failed" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_link_repo_skips_push(self, orch, git):
        """LINK repos should not push at all — just notify for manual review."""
        git.ahas_remote = AsyncMock(return_value=False)
        git.apush_branch = AsyncMock()
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        result = await orch._create_pr_for_task(task, repo, "/workspace")

        git.apush_branch.assert_not_called()
        assert result is None
        assert len(orch._notifications) == 1
        assert "Approval Required" in orch._notifications[0]  # plain text via _emit_text_notify

    @pytest.mark.asyncio
    async def test_pr_creation_after_push(self, orch, git):
        """After successful push, create_pr should be called with correct args."""
        git.apush_branch = AsyncMock()
        git.acreate_pr = AsyncMock(return_value="https://github.com/test/repo/pull/99")
        task = _make_task(title="My Feature", description="A long description " * 50)
        repo = _make_repo(default_branch="develop")

        result = await orch._create_pr_for_task(task, repo, "/workspace")

        git.apush_branch.assert_called_once()
        call_args = git.apush_branch.call_args
        assert call_args[0] == ("/workspace", "task/test-branch")
        assert call_args[1].get("force_with_lease") is True
        git.acreate_pr.assert_called_once()
        call_kwargs = git.acreate_pr.call_args
        assert call_kwargs[1]["base"] == "develop" or call_kwargs[0][3] == "develop"
        assert result == "https://github.com/test/repo/pull/99"

    @pytest.mark.asyncio
    async def test_pr_creation_failure_notifies(self, orch, git):
        """PR creation failure should notify but push already succeeded."""
        git.apush_branch = AsyncMock()
        git.acreate_pr = AsyncMock(side_effect=GitError("gh not found"))
        task = _make_task()
        repo = _make_repo()

        result = await orch._create_pr_for_task(task, repo, "/workspace")

        # Push should have been called (and succeeded, since no side_effect)
        git.apush_branch.assert_called_once()
        assert result is None
        assert len(orch._notifications) == 1
        assert "PR Creation Failed" in orch._notifications[0]  # plain text via _emit_text_notify
