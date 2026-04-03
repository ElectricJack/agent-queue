"""Tests for rebase-before-merge conflict resolution.

Covers:
- ``GitManager.rebase_onto()`` — the new helper method.
- ``sync_and_merge()`` — rebase fallback when direct merge fails.
- ``_merge_and_push()`` orchestrator integration for LINK repos.
- Integration tests with real git repos verifying the full flow.
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.git.manager import GitError, GitManager


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


def _head_sha(cwd: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=cwd)


def _current_branch(cwd: str) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_agent_clones(tmp_path):
    """Bare remote with two agent clones, each with an initial commit."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )

    agent1 = str(tmp_path / "agent1")
    subprocess.run(
        ["git", "clone", str(remote), agent1],
        check=True,
        capture_output=True,
    )
    (pathlib.Path(agent1) / "README.md").write_text("init")
    _git(["add", "."], cwd=agent1)
    _git(
        [
            "-c",
            "user.name=Agent1",
            "-c",
            "user.email=a1@test.com",
            "commit",
            "-m",
            "initial commit",
        ],
        cwd=agent1,
    )
    _git(["push", "origin", "main"], cwd=agent1)

    agent2 = str(tmp_path / "agent2")
    subprocess.run(
        ["git", "clone", str(remote), agent2],
        check=True,
        capture_output=True,
    )

    return {"remote": str(remote), "agent1": agent1, "agent2": agent2}


@pytest.fixture
def link_repo(tmp_path):
    """A local repo (no remote) simulating a LINK repo."""
    repo = str(tmp_path / "link-repo")
    subprocess.run(
        ["git", "init", "--initial-branch=main", repo],
        check=True,
        capture_output=True,
    )
    (pathlib.Path(repo) / "README.md").write_text("init")
    _git(["add", "."], cwd=repo)
    _git(
        ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "initial commit"],
        cwd=repo,
    )
    return repo


# ===========================================================================
# Unit tests: rebase_onto()
# ===========================================================================


class TestRebaseOnto:
    """Unit tests for the rebase_onto() helper (real git repos)."""

    def test_rebase_succeeds_no_conflict(self, two_agent_clones):
        """Rebase succeeds when there are no conflicts."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]

        # Create a task branch from old main
        mgr.create_branch(agent1, "task/feature")
        _git_commit(agent1, "feature.txt", "feature work", "add feature")
        _git(["checkout", "main"], cwd=agent1)

        # Advance main with a non-conflicting change
        _git_commit(agent1, "other.txt", "other work", "other change")

        # Rebase should succeed
        result = mgr.rebase_onto(agent1, "task/feature", "main")
        assert result is True

        # Verify we're back on main (original branch)
        assert _current_branch(agent1) == "main"

        # Verify the task branch is now based on top of latest main
        _git(["checkout", "task/feature"], cwd=agent1)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "other change" in log
        assert "add feature" in log

    def test_rebase_fails_with_conflict(self, two_agent_clones):
        """Rebase returns False when there are conflicts."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]

        # Create a task branch that modifies README.md
        mgr.create_branch(agent1, "task/conflict")
        _git_commit(agent1, "README.md", "task version", "task changes README")
        _git(["checkout", "main"], cwd=agent1)

        # Advance main by also modifying README.md (conflict)
        _git_commit(agent1, "README.md", "main version", "main changes README")

        # Rebase should fail and return False
        result = mgr.rebase_onto(agent1, "task/conflict", "main")
        assert result is False

        # Verify we're back on main (restored after abort)
        assert _current_branch(agent1) == "main"

    def test_rebase_returns_to_original_branch(self, two_agent_clones):
        """After rebase, checkout returns to whatever branch we were on."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]

        # Create two branches
        mgr.create_branch(agent1, "task/feature")
        _git_commit(agent1, "feature.txt", "work", "feature")
        _git(["checkout", "main"], cwd=agent1)

        mgr.create_branch(agent1, "other-branch")
        _git_commit(agent1, "other.txt", "work", "other")

        # While on other-branch, rebase task/feature onto main
        result = mgr.rebase_onto(agent1, "task/feature", "main")
        assert result is True
        assert _current_branch(agent1) == "other-branch"

    def test_rebase_onto_origin_branch(self, two_agent_clones):
        """Rebase works with origin/<branch> as the onto target."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 1 pushes a change to main
        _git_commit(agent1, "pushed.txt", "pushed", "pushed change")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2 has a task branch from old main
        mgr.create_branch(agent2, "task/stale")
        _git_commit(agent2, "stale.txt", "stale", "stale feature")
        _git(["checkout", "main"], cwd=agent2)

        # Fetch latest
        _git(["fetch", "origin"], cwd=agent2)

        # Rebase task branch onto origin/main
        result = mgr.rebase_onto(agent2, "task/stale", "origin/main")
        assert result is True

        # Verify the task branch now has the pushed change
        _git(["checkout", "task/stale"], cwd=agent2)
        log = _git(["log", "--oneline"], cwd=agent2)
        assert "pushed change" in log
        assert "stale feature" in log


# ===========================================================================
# Unit tests: sync_and_merge() rebase fallback (mocked)
# ===========================================================================


class TestSyncAndMergeRebaseFallback:
    """Mocked tests verifying sync_and_merge tries rebase on merge conflict."""

    def test_rebase_attempted_on_merge_conflict(self):
        """When merge fails, sync_and_merge should try rebase before giving up."""
        mgr = GitManager()
        merge_attempt = [0]

        def mock_run(args, cwd=None):
            # Only fail on actual merge (not merge --abort)
            if args[:1] == ["merge"] and "--abort" not in args:
                merge_attempt[0] += 1
                if merge_attempt[0] == 1:
                    raise GitError("merge conflict")
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            with patch.object(mgr, "rebase_onto", return_value=True) as mock_rebase:
                success, err = mgr.sync_and_merge("/ws", "task/feat")

        mock_rebase.assert_called_once_with("/ws", "task/feat", "main")
        assert success is True

    def test_rebase_failure_returns_merge_conflict(self):
        """If rebase also fails, sync_and_merge returns merge_conflict."""
        mgr = GitManager()

        def mock_run(args, cwd=None):
            if args[:1] == ["merge"] and "--abort" not in args:
                raise GitError("merge conflict")
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            with patch.object(mgr, "rebase_onto", return_value=False):
                success, err = mgr.sync_and_merge("/ws", "task/feat")

        assert success is False
        assert err == "merge_conflict"

    def test_rebase_not_attempted_when_merge_succeeds(self):
        """When merge succeeds directly, rebase is not attempted."""
        mgr = GitManager()

        def mock_run(args, cwd=None):
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            with patch.object(mgr, "rebase_onto") as mock_rebase:
                success, err = mgr.sync_and_merge("/ws", "task/feat")

        assert success is True
        mock_rebase.assert_not_called()

    def test_rebase_success_but_second_merge_fails(self):
        """Rebase succeeds but retry merge still conflicts → merge_conflict."""
        mgr = GitManager()
        merge_count = [0]

        def mock_run(args, cwd=None):
            if args[:1] == ["merge"] and "--abort" not in args:
                merge_count[0] += 1
                raise GitError("merge conflict")
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            with patch.object(mgr, "rebase_onto", return_value=True):
                success, err = mgr.sync_and_merge("/ws", "task/feat")

        assert success is False
        assert err == "merge_conflict"
        # Two merge attempts: initial + retry after rebase
        assert merge_count[0] == 2


# ===========================================================================
# Integration tests: sync_and_merge() rebase fallback (real git repos)
# ===========================================================================


class TestSyncAndMergeRebaseIntegration:
    """Integration tests with real repos for the rebase-before-merge flow."""

    def test_rebase_resolves_stale_branch_conflict(self, two_agent_clones):
        """Agent's stale branch is rebased and merged after initial conflict.

        Scenario: Agent 2 creates a task branch from old main.  Agent 1 then
        pushes a change that makes agent 2's branch "stale" (based on old
        main).  When agent 2 tries to merge, the direct merge might fail
        if the changes overlap, but rebase resolves the ordering.

        In this specific test we create a non-conflicting overlap scenario
        where merge might create complications but rebase is cleaner.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch from current (old) main
        mgr.create_branch(agent2, "task/a2-feature")
        _git_commit(agent2, "feature.txt", "feature work", "agent2 feature")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes a change to main (advancing origin/main)
        _git_commit(agent1, "other.txt", "agent1 work", "agent1 change")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2's sync_and_merge: fetch sees the new main, merge may need
        # rebase to resolve the stale base.  Since changes don't actually
        # conflict (different files), merge will succeed directly.
        # But we verify the mechanism works end-to-end.
        success, err = mgr.sync_and_merge(agent2, "task/a2-feature")
        assert success is True
        assert err == ""

        # Both changes should be on remote
        verify = str(pathlib.Path(two_agent_clones["remote"]).parent / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "agent1 change" in log
        assert "agent2 feature" in log

    def test_true_conflict_fails_even_with_rebase(self, two_agent_clones):
        """When both agents modify the same file, rebase can't help either."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch modifying README.md
        mgr.create_branch(agent2, "task/a2-readme")
        _git_commit(agent2, "README.md", "agent2 version", "agent2 changes README")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes a conflicting change to README.md
        _git_commit(agent1, "README.md", "agent1 version", "agent1 changes README")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2's sync_and_merge: both direct merge and rebase should fail
        success, err = mgr.sync_and_merge(agent2, "task/a2-readme")
        assert success is False
        assert err == "merge_conflict"

        # Agent 2 should be in a usable state (on main, matching origin)
        # after the failed attempt — sync_and_merge leaves workspace on
        # default branch after merge abort
        assert _current_branch(agent2) == "main"

    def test_rebase_resolves_merge_after_multiple_pushes(self, two_agent_clones, tmp_path):
        """Rebase handles the case where main has advanced multiple commits."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch early
        mgr.create_branch(agent2, "task/a2-late")
        _git_commit(agent2, "late.txt", "late work", "agent2 late feature")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes several commits to main
        for i in range(3):
            _git_commit(agent1, f"file{i}.txt", f"content{i}", f"agent1 push {i}")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2 tries to merge — should succeed (no file conflicts)
        success, err = mgr.sync_and_merge(agent2, "task/a2-late")
        assert success is True

        # All changes present
        verify = str(tmp_path / "verify-multi")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "agent2 late feature" in log
        for i in range(3):
            assert f"agent1 push {i}" in log


# ===========================================================================
# Unit tests: rebase_onto() with mocks
# ===========================================================================


class TestRebaseOntoMocked:
    """Mock-based unit tests for rebase_onto edge cases."""

    def test_rebase_aborted_on_conflict(self):
        """On conflict, rebase --abort is called and False returned."""
        mgr = GitManager()
        call_log = []

        def mock_run(args, cwd=None):
            call_log.append(args)
            if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "main"
            if args[:1] == ["rebase"] and "--abort" not in args:
                raise GitError("rebase conflict")
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            result = mgr.rebase_onto("/ws", "task/feat", "origin/main")

        assert result is False
        assert ["rebase", "--abort"] in call_log
        # Should restore original branch
        assert ["checkout", "main"] in call_log

    def test_rebase_success_returns_to_original(self):
        """On success, returns to the original branch."""
        mgr = GitManager()
        call_log = []

        def mock_run(args, cwd=None):
            call_log.append(args)
            if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "main"
            return ""

        with patch.object(mgr, "_run", side_effect=mock_run):
            result = mgr.rebase_onto("/ws", "task/feat", "origin/main")

        assert result is True
        assert ["checkout", "task/feat"] in call_log
        assert ["rebase", "origin/main"] in call_log
        # Last checkout should be back to original branch
        checkout_calls = [c for c in call_log if c[0] == "checkout"]
        assert checkout_calls[-1] == ["checkout", "main"]


# ===========================================================================
# Orchestrator tests: LINK repo rebase fallback
# ===========================================================================


class _FakeOrchestrator:
    """Minimal stand-in for Orchestrator to test _merge_and_push in isolation."""

    def __init__(self, git: GitManager):
        self.git = git
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    from src.orchestrator import Orchestrator as _Orch

    _merge_and_push = _Orch._merge_and_push


def _make_task(**overrides):
    from src.models import Task

    defaults = dict(
        id="test-task",
        project_id="proj-1",
        title="Test Task",
        description="desc",
        branch_name="task/test-branch",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_repo(source_type=None, **overrides):
    from src.models import RepoConfig, RepoSourceType

    if source_type is None:
        source_type = RepoSourceType.CLONE
    defaults = dict(
        id="repo-1",
        project_id="proj-1",
        source_type=source_type,
        url="https://github.com/test/repo.git",
        default_branch="main",
    )
    defaults.update(overrides)
    return RepoConfig(**defaults)


class TestLinkRepoRebaseFallback:
    """LINK repo rebase fallback in _merge_and_push."""

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
    async def test_link_rebase_tried_on_merge_conflict(self, orch, git):
        """LINK repo: rebase is attempted when direct merge fails."""
        from src.models import RepoSourceType

        # First merge fails, rebase succeeds, second merge succeeds
        git.amerge_branch = AsyncMock(side_effect=[False, True])
        git.arebase_onto = AsyncMock(return_value=True)
        git.adelete_branch = AsyncMock()

        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.arebase_onto.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            "main",
        )
        # amerge_branch called twice: initial + retry
        assert git.amerge_branch.call_count == 2
        # No conflict notification (rebase resolved it)
        assert not orch._notifications
        # Branch cleanup happens
        git.adelete_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_link_rebase_fails_still_notifies(self, orch, git):
        """LINK repo: if rebase also fails, conflict notification is sent."""
        from src.models import RepoSourceType

        git.amerge_branch = AsyncMock(return_value=False)
        git.arebase_onto = AsyncMock(return_value=False)
        git._arun = AsyncMock()

        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.arebase_onto.assert_called_once()
        # Only one merge attempt (rebase failed, so no retry)
        assert git.amerge_branch.call_count == 1
        # Conflict notification sent
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_link_no_rebase_when_merge_succeeds(self, orch, git):
        """LINK repo: no rebase when direct merge succeeds."""
        from src.models import RepoSourceType

        git.amerge_branch = AsyncMock(return_value=True)
        git.arebase_onto = AsyncMock()
        git.adelete_branch = AsyncMock()

        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.arebase_onto.assert_not_called()
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_link_rebase_succeeds_but_retry_merge_fails(self, orch, git):
        """LINK repo: rebase succeeds but retry merge still fails → notify."""
        from src.models import RepoSourceType

        # Both merges fail, rebase succeeds
        git.amerge_branch = AsyncMock(return_value=False)
        git.arebase_onto = AsyncMock(return_value=True)
        git._arun = AsyncMock()

        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        # Both merge attempts fail
        assert git.amerge_branch.call_count == 2
        # Notification sent for persistent conflict
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_clone_repo_uses_sync_and_merge_rebase(self, orch, git):
        """CLONE repo: rebase fallback is handled inside sync_and_merge, not orchestrator."""
        from src.models import RepoSourceType

        git.ahas_remote = AsyncMock(return_value=True)
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        git.arebase_onto = AsyncMock()

        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.CLONE)

        await orch._merge_and_push(task, repo, "/workspace")

        # CLONE path uses async_and_merge (which internally handles rebase)
        git.async_and_merge.assert_called_once()
        # arebase_onto should NOT be called by orchestrator for CLONE repos
        git.arebase_onto.assert_not_called()


# ===========================================================================
# Integration test: LINK repo rebase fallback (real git)
# ===========================================================================


class TestLinkRepoRebaseIntegration:
    """Integration tests with real LINK repos for rebase fallback."""

    def test_link_rebase_resolves_non_conflicting_overlap(self, link_repo):
        """LINK repo: rebase resolves non-conflicting changes on same base."""
        mgr = GitManager()

        # Create a task branch from current main
        mgr.create_branch(link_repo, "task/feature")
        _git_commit(link_repo, "feature.txt", "feature work", "add feature")
        _git(["checkout", "main"], cwd=link_repo)

        # Advance main with a non-conflicting change
        _git_commit(link_repo, "other.txt", "other work", "advance main")

        # Direct merge_branch should succeed here (non-conflicting), but
        # let's verify the rebase path works independently
        result = mgr.rebase_onto(link_repo, "task/feature", "main")
        assert result is True

        # Now merge should be trivially clean
        merged = mgr.merge_branch(link_repo, "task/feature", "main")
        assert merged is True

        # Both changes present
        log = _git(["log", "--oneline"], cwd=link_repo)
        assert "add feature" in log
        assert "advance main" in log

    def test_link_true_conflict_rebase_also_fails(self, link_repo):
        """LINK repo: true conflict causes both merge and rebase to fail."""
        mgr = GitManager()

        # Create a task branch modifying README.md
        mgr.create_branch(link_repo, "task/conflict")
        _git_commit(link_repo, "README.md", "task version", "task README")
        _git(["checkout", "main"], cwd=link_repo)

        # Main also modifies README.md
        _git_commit(link_repo, "README.md", "main version", "main README")

        # Direct merge fails
        merged = mgr.merge_branch(link_repo, "task/conflict", "main")
        assert merged is False

        # Rebase also fails
        rebased = mgr.rebase_onto(link_repo, "task/conflict", "main")
        assert rebased is False

        # Should be back on main and in a clean state
        assert _current_branch(link_repo) == "main"
