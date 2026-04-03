"""Tests for GitManager.sync_and_merge().

Verifies the full sync-merge-push flow:
  - Happy path: fetch, reset, merge, push all succeed.
  - Merge conflict: detected and reported without attempting push.
  - Push rejection with retry: pull --rebase then push again.
  - Push retries exhausted: returns failure with message.
  - Default branch is synced to origin before merging.
  - Rebase fallback: when merge fails, rebase is attempted before giving up.
"""

import pathlib
import subprocess
from unittest.mock import patch

import pytest

from src.git.manager import GitError, GitManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> str:
    """Run a git command, returning stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_commit(cwd: str, filename: str, content: str, message: str) -> str:
    """Create/update a file, commit it, and return the commit SHA."""
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
def git_repo(tmp_path):
    """Create a bare remote + working clone with an initial commit."""
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
    # Initial commit
    (clone / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(clone), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "init"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=str(clone), check=True, capture_output=True)
    return {"remote": str(remote), "clone": str(clone)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSyncAndMerge:
    """Integration tests for the sync_and_merge() method."""

    def test_happy_path(self, git_repo):
        """Fetch, merge, push all succeed on the first attempt."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with work
        mgr.create_branch(clone, "task/happy")
        _git_commit(clone, "feature.txt", "feature", "add feature")
        _git(["checkout", "main"], cwd=clone)

        success, err = mgr.sync_and_merge(clone, "task/happy")

        assert success is True
        assert err == ""
        # The merge should be on main
        assert _current_branch(clone) == "main"
        log = _git(["log", "--oneline"], cwd=clone)
        assert "add feature" in log

    def test_push_lands_on_remote(self, git_repo, tmp_path):
        """After sync_and_merge, the remote has the merged commit."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/push-check")
        _git_commit(clone, "pushed.txt", "pushed", "pushed commit")
        _git(["checkout", "main"], cwd=clone)

        success, _ = mgr.sync_and_merge(clone, "task/push-check")
        assert success is True

        # Verify via a fresh clone that the commit reached the remote
        clone2 = str(tmp_path / "verify-clone")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=clone2)
        assert "pushed commit" in log

    def test_merge_conflict_returns_error(self, git_repo, tmp_path):
        """When the task branch conflicts with main, returns (False, 'merge_conflict')."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch that modifies README.md
        mgr.create_branch(clone, "task/conflict")
        _git_commit(clone, "README.md", "task version", "task changes README")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main with a conflicting change
        clone2 = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True,
            capture_output=True,
        )
        _git_commit(clone2, "README.md", "other version", "other changes README")
        _git(["push", "origin", "main"], cwd=clone2)

        success, err = mgr.sync_and_merge(clone, "task/conflict")

        assert success is False
        assert err == "merge_conflict"
        # main should still be in a clean state (merge aborted)
        assert _current_branch(clone) == "main"

    def test_syncs_before_merging(self, git_repo, tmp_path):
        """sync_and_merge fetches latest origin/main before merging."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch
        mgr.create_branch(clone, "task/sync-test")
        _git_commit(clone, "task-file.txt", "task", "task commit")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True,
            capture_output=True,
        )
        _git_commit(clone2, "other.txt", "other", "other agent commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # Local clone's main is behind — sync_and_merge should fetch first
        success, err = mgr.sync_and_merge(clone, "task/sync-test")

        assert success is True
        assert err == ""
        log = _git(["log", "--oneline"], cwd=clone)
        assert "task commit" in log
        assert "other agent commit" in log

    def test_discards_stale_local_main(self, git_repo):
        """sync_and_merge hard-resets main, discarding un-pushed local commits."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with work
        mgr.create_branch(clone, "task/stale")
        _git_commit(clone, "feature.txt", "feature", "feature commit")

        # Go back to main and create a local-only commit (stale state)
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "stale.txt", "stale", "stale local commit")

        success, err = mgr.sync_and_merge(clone, "task/stale")

        assert success is True
        log = _git(["log", "--oneline"], cwd=clone)
        assert "feature commit" in log
        assert "stale local commit" not in log

    def test_max_retries_zero_single_push_attempt(self, git_repo, tmp_path):
        """With max_retries=0, only one push attempt is made."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/single-try")
        _git_commit(clone, "f.txt", "f", "single try commit")
        _git(["checkout", "main"], cwd=clone)

        # Should succeed with a single attempt when remote is reachable
        success, err = mgr.sync_and_merge(clone, "task/single-try", max_retries=0)
        assert success is True
        assert err == ""

    def test_already_on_task_branch(self, git_repo):
        """sync_and_merge works even if we start on the task branch."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/on-branch")
        _git_commit(clone, "work.txt", "work", "work commit")
        # Don't switch back to main — stay on task branch

        success, err = mgr.sync_and_merge(clone, "task/on-branch")

        assert success is True
        assert err == ""
        assert _current_branch(clone) == "main"
        log = _git(["log", "--oneline"], cwd=clone)
        assert "work commit" in log


@pytest.fixture
def two_agent_clones(tmp_path):
    """Bare remote with two agent clones, each starting from same initial commit."""
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
    (pathlib.Path(agent1) / "README.md").write_text("init\n")
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


class TestSyncAndMergeRebaseOnConflict:
    """Integration tests verifying sync_and_merge tries rebase on conflict.

    These tests use real git repos with intentional conflicting changes
    between two branches to exercise the rebase fallback path.
    """

    def test_intentional_conflict_triggers_rebase_attempt(self, two_agent_clones):
        """Two agents modify the same file → rebase_onto is actually called.

        Scenario:
          - Agent 2 creates a task branch modifying README.md
          - Agent 1 pushes a conflicting change to README.md on main
          - Agent 2 calls sync_and_merge → merge fails → rebase attempted
          - Rebase also fails (same conflict) → returns merge_conflict

        Uses a spy on rebase_onto to verify the fallback was invoked.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch that modifies README.md
        mgr.create_branch(agent2, "task/a2-readme")
        _git_commit(agent2, "README.md", "agent2 version\n", "agent2 modifies README")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes a conflicting change to README.md on origin/main
        _git_commit(agent1, "README.md", "agent1 version\n", "agent1 modifies README")
        _git(["push", "origin", "main"], cwd=agent1)

        # Spy on rebase_onto to verify it's called during sync_and_merge
        with patch.object(mgr, "rebase_onto", wraps=mgr.rebase_onto) as spy_rebase:
            success, err = mgr.sync_and_merge(agent2, "task/a2-readme")

        assert success is False
        assert err == "merge_conflict"
        # Verify rebase_onto was actually called as a fallback
        spy_rebase.assert_called_once_with(agent2, "task/a2-readme", "main")

    def test_workspace_clean_after_failed_rebase_fallback(self, two_agent_clones):
        """After failed merge + failed rebase, workspace has no uncommitted changes.

        The workspace must be left in a usable state so subsequent tasks
        can proceed without manual cleanup.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Create conflicting changes between agent1 (pushed to main) and agent2 (task branch)
        mgr.create_branch(agent2, "task/a2-dirty")
        _git_commit(agent2, "README.md", "agent2 dirty\n", "agent2 dirty change")
        _git(["checkout", "main"], cwd=agent2)

        _git_commit(agent1, "README.md", "agent1 dirty\n", "agent1 dirty change")
        _git(["push", "origin", "main"], cwd=agent1)

        success, err = mgr.sync_and_merge(agent2, "task/a2-dirty")
        assert success is False

        # Workspace should be clean: on main, no uncommitted changes, no
        # leftover merge/rebase state
        assert _current_branch(agent2) == "main"
        status = _git(["status", "--porcelain"], cwd=agent2)
        assert status == "", f"Workspace has uncommitted changes: {status}"

        # Verify no rebase is in progress
        rebase_dir = pathlib.Path(agent2) / ".git" / "rebase-merge"
        assert not rebase_dir.exists(), "Rebase state was not cleaned up"
        rebase_apply = pathlib.Path(agent2) / ".git" / "rebase-apply"
        assert not rebase_apply.exists(), "Rebase-apply state was not cleaned up"

    def test_no_push_after_failed_rebase_fallback(self, two_agent_clones):
        """When both merge and rebase fail, push is never attempted.

        Uses a spy on _run to verify no push commands are issued after
        the conflict is detected.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Create conflicting changes
        mgr.create_branch(agent2, "task/a2-nopush")
        _git_commit(agent2, "README.md", "agent2 nopush\n", "agent2 nopush")
        _git(["checkout", "main"], cwd=agent2)

        _git_commit(agent1, "README.md", "agent1 nopush\n", "agent1 nopush")
        _git(["push", "origin", "main"], cwd=agent1)

        # Spy on _run to capture all git commands issued
        original_run = mgr._run
        commands_issued = []

        def tracking_run(args, cwd=None):
            commands_issued.append(args)
            return original_run(args, cwd=cwd)

        with patch.object(mgr, "_run", side_effect=tracking_run):
            success, err = mgr.sync_and_merge(agent2, "task/a2-nopush")

        assert success is False
        assert err == "merge_conflict"

        # Verify push was never called
        push_calls = [c for c in commands_issued if c[0] == "push"]
        assert len(push_calls) == 0, f"Push should not be attempted, but got: {push_calls}"

    def test_conflict_on_multiple_files(self, two_agent_clones):
        """Conflict across multiple files still triggers rebase and fails cleanly."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates task branch modifying two files
        mgr.create_branch(agent2, "task/a2-multi")
        _git_commit(agent2, "README.md", "agent2 readme\n", "agent2 readme")
        _git_commit(agent2, "config.txt", "agent2 config\n", "agent2 config")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 modifies the same files and pushes
        _git_commit(agent1, "README.md", "agent1 readme\n", "agent1 readme")
        _git_commit(agent1, "config.txt", "agent1 config\n", "agent1 config")
        _git(["push", "origin", "main"], cwd=agent1)

        with patch.object(mgr, "rebase_onto", wraps=mgr.rebase_onto) as spy_rebase:
            success, err = mgr.sync_and_merge(agent2, "task/a2-multi")

        assert success is False
        assert err == "merge_conflict"
        spy_rebase.assert_called_once()
        # Workspace is clean
        assert _current_branch(agent2) == "main"

    def test_rebase_succeeds_for_non_conflicting_stale_branch(self, two_agent_clones, tmp_path):
        """Non-conflicting stale branch: merge succeeds directly (rebase not needed).

        When the task branch and main modify different files, git merge
        succeeds without needing the rebase fallback.  This verifies the
        happy path still works when main has advanced.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch touching only feature.txt
        mgr.create_branch(agent2, "task/a2-clean")
        _git_commit(agent2, "feature.txt", "feature work\n", "agent2 feature")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes a change to a different file
        _git_commit(agent1, "other.txt", "other work\n", "agent1 other change")
        _git(["push", "origin", "main"], cwd=agent1)

        with patch.object(mgr, "rebase_onto", wraps=mgr.rebase_onto) as spy_rebase:
            success, err = mgr.sync_and_merge(agent2, "task/a2-clean")

        assert success is True
        assert err == ""
        # Merge succeeded directly, so rebase should NOT have been called
        spy_rebase.assert_not_called()

        # Both changes present on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "agent2 feature" in log
        assert "agent1 other change" in log

    def test_conflict_leaves_task_branch_intact(self, two_agent_clones):
        """After a failed merge+rebase, the task branch still has its commits.

        The task branch should not be corrupted by the failed rebase attempt;
        it should still contain all its original work so it can be manually
        resolved later.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates a task branch with multiple commits
        mgr.create_branch(agent2, "task/a2-preserve")
        _git_commit(agent2, "README.md", "agent2 v1\n", "agent2 first change")
        _git_commit(agent2, "extra.txt", "extra work\n", "agent2 extra work")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 pushes conflicting change
        _git_commit(agent1, "README.md", "agent1 conflict\n", "agent1 conflict")
        _git(["push", "origin", "main"], cwd=agent1)

        success, err = mgr.sync_and_merge(agent2, "task/a2-preserve")
        assert success is False

        # Switch to task branch and verify its commits are intact
        _git(["checkout", "task/a2-preserve"], cwd=agent2)
        log = _git(["log", "--oneline"], cwd=agent2)
        assert "agent2 first change" in log
        assert "agent2 extra work" in log


class TestSyncAndMergeRetry:
    """Unit tests for the push-retry logic using mocked _run."""

    def test_push_retry_succeeds_on_second_attempt(self):
        """If push fails once and then succeeds, returns (True, '')."""
        mgr = GitManager()
        calls = []

        original_run = mgr._run

        def mock_run(args, cwd=None):
            calls.append(args)
            if args[:3] == ["push", "origin", "main"]:
                if sum(1 for c in calls if c[:3] == ["push", "origin", "main"]) == 1:
                    raise GitError("rejected")
            # For all other commands (and second push), just record but don't execute
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch", max_retries=2)

        assert success is True
        assert err == ""
        # Should have two push attempts
        push_calls = [c for c in calls if c[:3] == ["push", "origin", "main"]]
        assert len(push_calls) == 2
        # Should have a pull --rebase between them
        rebase_calls = [c for c in calls if "pull" in c and "--rebase" in c]
        assert len(rebase_calls) == 1

    def test_push_retries_exhausted(self):
        """When all push attempts fail, returns (False, 'push_failed: ...')."""
        mgr = GitManager()

        def mock_run(args, cwd=None):
            if args[:3] == ["push", "origin", "main"]:
                raise GitError("rejected")
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch", max_retries=2)

        assert success is False
        assert err.startswith("push_failed:")

    def test_merge_conflict_no_push_attempted(self):
        """On merge conflict, push is never attempted (even after rebase retry)."""
        mgr = GitManager()
        calls = []

        def mock_run(args, cwd=None):
            calls.append(args)
            if args[:2] == ["merge", "task/branch"]:
                raise GitError("conflict")
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch")

        assert success is False
        assert err == "merge_conflict"
        # Verify merge --abort was called (twice: initial + retry after rebase)
        abort_calls = [c for c in calls if c == ["merge", "--abort"]]
        assert len(abort_calls) == 2
        # Verify push was never called
        push_calls = [c for c in calls if c[0] == "push"]
        assert len(push_calls) == 0
