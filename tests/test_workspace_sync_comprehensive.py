"""Comprehensive tests for the workspace sync mechanism.

End-to-end scenarios covering the full agent workspace lifecycle:
  - Concurrent push race conditions between multiple agents.
  - Merge conflict detection and rebase recovery workflows.
  - Workspace reset after failed merge-and-push.
  - Retried task branch rebase (prepare_for_task on existing branch).
  - ``--force-with-lease`` push behavior across task retries.
  - Subtask chain drift and mid-chain rebase.

These tests complement the unit-level tests in test_sync_and_merge.py,
test_force_with_lease.py, test_mid_chain_rebase.py, test_merge_and_push.py,
test_concurrent_merge_and_push.py, and test_rebase_before_merge.py.  The
focus here is on multi-step, cross-method workflows that exercise the
system end-to-end.
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AutoTaskConfig
from src.git.manager import GitError, GitManager
from src.models import (
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
)


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def three_agent_clones(tmp_path):
    """Bare remote with three agent clones for multi-agent concurrency tests."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    agents = {}
    for i in range(1, 4):
        agent_path = str(tmp_path / f"agent{i}")
        subprocess.run(
            ["git", "clone", str(remote), agent_path],
            check=True,
            capture_output=True,
        )
        agents[f"agent{i}"] = agent_path

    # Initial commit from agent1
    a1 = agents["agent1"]
    (pathlib.Path(a1) / "README.md").write_text("init\n")
    _git(["add", "."], cwd=a1)
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
        cwd=a1,
    )
    _git(["push", "origin", "main"], cwd=a1)

    # Pull initial commit into other agents
    for name in ["agent2", "agent3"]:
        _git(["pull", "origin", "main"], cwd=agents[name])

    agents["remote"] = str(remote)
    return agents


# ===========================================================================
# 1. Concurrent push race conditions
# ===========================================================================


class TestConcurrentPushRaceConditions:
    """End-to-end tests for concurrent agent push race conditions.

    Verifies that when multiple agents finish work simultaneously and try
    to push to the same remote, the sync_and_merge retry logic handles
    the race conditions correctly.
    """

    def test_race_three_agents_non_conflicting(self, three_agent_clones, tmp_path):
        """Three agents merge sequentially with non-conflicting changes."""
        mgr = GitManager()
        agents = three_agent_clones

        # Each agent creates a task branch and does work on different files
        for i in range(1, 4):
            agent = agents[f"agent{i}"]
            branch = f"task/agent{i}-race"
            mgr.prepare_for_task(agent, branch)
            _git_commit(agent, f"agent{i}_work.txt", f"agent{i}", f"agent{i} work")
            _git(["checkout", "main"], cwd=agent)

        # All three merge — each may need to retry due to the previous push
        for i in range(1, 4):
            success, err = mgr.sync_and_merge(
                agents[f"agent{i}"],
                f"task/agent{i}-race",
                max_retries=3,
            )
            assert success is True, f"Agent {i} failed: {err}"

        # Verify all work on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", agents["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        for i in range(1, 4):
            assert f"agent{i} work" in log

    def test_race_interleaved_prepare_and_push(self, two_agent_clones, tmp_path):
        """Agent 2 prepares while Agent 1 pushes, then Agent 2 syncs correctly.

        Simulates: Agent 1 finishes and pushes → Agent 2 (which prepared
        before Agent 1 pushed) now has a stale main → sync_and_merge
        fetches latest and handles it.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 prepares its workspace (before agent 1 finishes)
        mgr.prepare_for_task(agent2, "task/a2-interleave")
        _git_commit(agent2, "a2.txt", "a2 work", "agent2 early work")

        # Agent 1 does work and pushes to main
        mgr.prepare_for_task(agent1, "task/a1-interleave")
        _git_commit(agent1, "a1.txt", "a1 work", "agent1 work")
        _git(["checkout", "main"], cwd=agent1)
        success1, _ = mgr.sync_and_merge(agent1, "task/a1-interleave")
        assert success1 is True

        # Agent 2 now tries to merge — its prepare_for_task was done
        # before agent 1 pushed, but sync_and_merge fetches latest
        _git(["checkout", "main"], cwd=agent2)
        success2, err2 = mgr.sync_and_merge(
            agent2,
            "task/a2-interleave",
            max_retries=2,
        )
        assert success2 is True
        assert err2 == ""

        # Both changes present on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "agent1 work" in log
        assert "agent2 early work" in log

    def test_push_retry_with_pull_rebase_integrates_new_commit(self):
        """When push is rejected, pull --rebase incorporates the new commit.

        Uses mocks to simulate the exact race condition: push fails once
        because remote moved, pull --rebase succeeds, second push succeeds.
        """
        mgr = GitManager()
        calls = []
        push_count = [0]

        def mock_run(args, cwd=None):
            calls.append(args)
            if args[:3] == ["push", "origin", "main"]:
                push_count[0] += 1
                if push_count[0] == 1:
                    raise GitError("rejected: non-fast-forward")
            return ""

        mgr._run = mock_run
        success, err = mgr.sync_and_merge("/ws", "task/branch", max_retries=2)

        assert success is True
        assert err == ""
        # Verify pull --rebase was called between push attempts
        pull_rebase_calls = [c for c in calls if c[:2] == ["pull", "--rebase"]]
        assert len(pull_rebase_calls) == 1
        assert pull_rebase_calls[0] == ["pull", "--rebase", "origin", "main"]


# ===========================================================================
# 2. Merge conflict detection and rebase recovery
# ===========================================================================


class TestMergeConflictAndRebaseRecovery:
    """End-to-end tests for merge conflict detection and rebase fallback."""

    def test_conflict_detected_workspace_stays_clean(self, two_agent_clones):
        """After merge+rebase both fail, workspace has no dirty state."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 modifies README.md
        mgr.prepare_for_task(agent2, "task/a2-conflict")
        _git_commit(agent2, "README.md", "agent2 version\n", "agent2 README")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 modifies same file and pushes
        _git_commit(agent1, "README.md", "agent1 version\n", "agent1 README")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2 sync_and_merge: merge fails, rebase fails
        success, err = mgr.sync_and_merge(agent2, "task/a2-conflict")
        assert success is False
        assert err == "merge_conflict"

        # Workspace is clean: on main, no uncommitted changes
        assert _current_branch(agent2) == "main"
        status = _git(["status", "--porcelain"], cwd=agent2)
        assert status == "", f"Dirty workspace: {status}"

        # No rebase in progress
        rebase_dir = pathlib.Path(agent2) / ".git" / "rebase-merge"
        assert not rebase_dir.exists()

    def test_rebase_resolves_stale_branch_different_files(self, two_agent_clones, tmp_path):
        """Non-conflicting stale branch merges after rebase brings it up to date."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates branch and works on feature.txt
        mgr.prepare_for_task(agent2, "task/a2-feature")
        _git_commit(agent2, "feature.txt", "feature\n", "agent2 feature")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 works on other.txt and pushes (non-conflicting)
        _git_commit(agent1, "other.txt", "other\n", "agent1 other")
        _git(["push", "origin", "main"], cwd=agent1)

        # Agent 2 merges — fetch+reset resolves stale main
        success, err = mgr.sync_and_merge(agent2, "task/a2-feature")
        assert success is True

        # Verify both changes on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "agent1 other" in log
        assert "agent2 feature" in log

    def test_conflict_preserves_task_branch_commits(self, two_agent_clones):
        """After failed merge+rebase, the task branch still has all its commits."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates branch with multiple commits
        mgr.prepare_for_task(agent2, "task/a2-multi")
        _git_commit(agent2, "README.md", "agent2 v1\n", "agent2 commit 1")
        _git_commit(agent2, "extra.txt", "extra\n", "agent2 commit 2")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 creates conflicting change and pushes
        _git_commit(agent1, "README.md", "agent1 version\n", "agent1 conflict")
        _git(["push", "origin", "main"], cwd=agent1)

        # Merge fails
        success, _ = mgr.sync_and_merge(agent2, "task/a2-multi")
        assert success is False

        # Task branch should still have both commits
        _git(["checkout", "task/a2-multi"], cwd=agent2)
        log = _git(["log", "--oneline"], cwd=agent2)
        assert "agent2 commit 1" in log
        assert "agent2 commit 2" in log

    def test_rebase_fallback_triggers_on_additive_conflict(self, two_agent_clones, tmp_path):
        """Rebase fallback is triggered when merge fails but rebase resolves it.

        Scenario: Agent 2 appends to a file, agent 1 modifies the beginning.
        Merge might fail due to contextual conflict, but rebase replays
        agent 2's append on top of agent 1's changes.
        """
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates branch and adds a new file
        mgr.prepare_for_task(agent2, "task/a2-additive")
        _git_commit(agent2, "new_feature.txt", "feature code\n", "agent2 new feature")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 adds different new file and pushes (non-conflicting)
        _git_commit(agent1, "agent1_feature.txt", "agent1 code\n", "agent1 feature")
        _git(["push", "origin", "main"], cwd=agent1)

        # sync_and_merge should handle this via direct merge (fetch+reset)
        success, err = mgr.sync_and_merge(agent2, "task/a2-additive")
        assert success is True

        # Both features on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        assert (pathlib.Path(verify) / "new_feature.txt").exists()
        assert (pathlib.Path(verify) / "agent1_feature.txt").exists()


# ===========================================================================
# 3. Workspace reset after failed merge-and-push
# ===========================================================================


class TestWorkspaceResetAfterFailure:
    """Verify workspace recovery leaves clones ready for the next task."""

    def test_full_recovery_cycle_merge_conflict(self, two_agent_clones):
        """Full cycle: conflict → recovery → prepare_for_task → new task succeeds."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 2 creates its branch BEFORE agent 1 pushes (stale base)
        mgr.prepare_for_task(agent2, "task/conflict")
        _git_commit(agent2, "README.md", "agent2 version\n", "agent2 README")
        _git(["checkout", "main"], cwd=agent2)

        # Agent 1 modifies same file and pushes (creating a real conflict)
        _git_commit(agent1, "README.md", "agent1 version\n", "agent1 README")
        _git(["push", "origin", "main"], cwd=agent1)

        # sync_and_merge fails: merge conflict on README.md
        success, err = mgr.sync_and_merge(agent2, "task/conflict")
        assert success is False
        assert err == "merge_conflict"

        # Recovery (as done by orchestrator's _merge_and_push)
        mgr._run(["checkout", "main"], cwd=agent2)
        mgr._run(["reset", "--hard", "origin/main"], cwd=agent2)

        # Verify recovery: on main, matching origin
        assert _current_branch(agent2) == "main"
        origin_sha = _git(["rev-parse", "origin/main"], cwd=agent2)
        assert _head_sha(agent2) == origin_sha

        # New task works fine
        mgr.prepare_for_task(agent2, "task/after-recovery")
        assert _current_branch(agent2) == "task/after-recovery"
        _git_commit(agent2, "recovered.txt", "ok", "recovery commit")
        _git(["checkout", "main"], cwd=agent2)
        success, err = mgr.sync_and_merge(agent2, "task/after-recovery")
        assert success is True

    def test_recovery_discards_stale_merge_commits(self, two_agent_clones):
        """Recovery hard-reset discards merge commits that were never pushed."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]

        origin_sha = _git(["rev-parse", "origin/main"], cwd=agent1)

        # Simulate failed push: merge locally but don't push
        mgr.create_branch(agent1, "task/failed")
        _git_commit(agent1, "failed.txt", "failed", "failed work")
        _git(["checkout", "main"], cwd=agent1)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "merge", "task/failed"], cwd=agent1
        )

        # Local main diverged from origin
        assert _head_sha(agent1) != origin_sha

        # Recovery
        mgr._run(["reset", "--hard", "origin/main"], cwd=agent1)
        assert _head_sha(agent1) == origin_sha

    def test_multiple_failures_then_success(self, two_agent_clones):
        """Agent can recover from multiple consecutive failures."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        for attempt in range(3):
            # Agent 2 creates its branch BEFORE agent 1 pushes (stale base)
            mgr.prepare_for_task(agent2, f"task/fail-{attempt}")
            _git_commit(
                agent2,
                "README.md",
                f"agent2 v{attempt}\n",
                f"agent2 attempt {attempt}",
            )
            _git(["checkout", "main"], cwd=agent2)

            # Agent 1 pushes a conflicting change (same file, different content)
            _git_commit(
                agent1,
                "README.md",
                f"agent1 v{attempt}\n",
                f"agent1 attempt {attempt}",
            )
            _git(["push", "origin", "main"], cwd=agent1)

            success, err = mgr.sync_and_merge(agent2, f"task/fail-{attempt}")
            assert success is False
            assert err == "merge_conflict"

            # Recovery after each failure
            mgr._run(["checkout", "main"], cwd=agent2)
            mgr._run(["reset", "--hard", "origin/main"], cwd=agent2)

        # After 3 failures, agent 2 can still do a non-conflicting task
        mgr.prepare_for_task(agent2, "task/final-success")
        _git_commit(agent2, "success.txt", "ok", "finally succeeds")
        _git(["checkout", "main"], cwd=agent2)
        success, err = mgr.sync_and_merge(agent2, "task/final-success")
        assert success is True


# ===========================================================================
# 4. Retried task branch rebase
# ===========================================================================


class TestRetriedTaskBranchRebase:
    """Tests for retried tasks where the branch already exists.

    When a task is retried (e.g. after failure), prepare_for_task finds the
    existing branch and rebases it onto the latest origin/main so the agent
    starts from up-to-date code.
    """

    def test_retried_task_includes_upstream_changes(self, two_agent_clones, tmp_path):
        """Retried task branch is rebased and includes new upstream commits."""
        mgr = GitManager()
        clone = two_agent_clones["agent1"]
        branch = "task/retried"

        # First attempt: create branch, do work
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "work.txt", "attempt 1", "first attempt")

        # Another agent pushes to main
        clone2 = two_agent_clones["agent2"]
        _git(["checkout", "main"], cwd=clone2)
        _git_commit(clone2, "upstream.txt", "upstream", "upstream change")
        _git(["push", "origin", "main"], cwd=clone2)

        # Second attempt (retry): prepare_for_task should rebase
        mgr.prepare_for_task(clone, branch)

        assert _current_branch(clone) == branch
        log = _git(["log", "--oneline"], cwd=clone)
        assert "first attempt" in log
        assert "upstream change" in log

    def test_retried_task_conflict_leaves_branch_usable(self, two_agent_clones):
        """If rebase on retry conflicts, branch is left as-is but usable."""
        mgr = GitManager()
        clone = two_agent_clones["agent1"]
        branch = "task/conflict-retry"

        # First attempt: modify README.md
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "README.md", "agent version\n", "agent modifies README")
        sha_before = _head_sha(clone)

        # Conflicting upstream change
        clone2 = two_agent_clones["agent2"]
        _git(["checkout", "main"], cwd=clone2)
        _git_commit(clone2, "README.md", "upstream version\n", "upstream README")
        _git(["push", "origin", "main"], cwd=clone2)

        # Go back to main, then retry
        _git(["checkout", "main"], cwd=clone)
        mgr.prepare_for_task(clone, branch)

        assert _current_branch(clone) == branch
        # Branch keeps original commit (rebase was aborted)
        assert _head_sha(clone) == sha_before

        # Agent can still work on the branch
        _git_commit(clone, "new_work.txt", "retry work", "retry commit")
        log = _git(["log", "--oneline"], cwd=clone)
        assert "retry commit" in log
        assert "agent modifies README" in log

    def test_retried_task_in_worktree(self, two_agent_clones, tmp_path):
        """Retried task in a worktree also gets rebased."""
        mgr = GitManager()
        clone = two_agent_clones["agent1"]
        branch = "task/wt-retry"

        # Create a worktree
        wt_path = str(tmp_path / "worktree")
        mgr.create_worktree(clone, wt_path, "wt-setup")

        # First attempt
        mgr.prepare_for_task(wt_path, branch)
        _git_commit(wt_path, "work.txt", "work", "worktree work")

        # Upstream change
        clone2 = two_agent_clones["agent2"]
        _git(["checkout", "main"], cwd=clone2)
        _git_commit(clone2, "upstream.txt", "upstream", "upstream change")
        _git(["push", "origin", "main"], cwd=clone2)

        # Retry
        mgr.prepare_for_task(wt_path, branch)

        assert _current_branch(wt_path) == branch
        log = _git(["log", "--oneline"], cwd=wt_path)
        assert "worktree work" in log
        assert "upstream change" in log


# ===========================================================================
# 5. force-with-lease push behavior
# ===========================================================================


class TestForceWithLeasePushBehavior:
    """End-to-end tests for --force-with-lease in various scenarios."""

    def test_retried_task_pr_push_with_force_with_lease(self, two_agent_clones):
        """Retried task that needs to push for PR uses force-with-lease.

        Scenario: First attempt pushes the branch, task fails, branch gets
        rebased on retry.  The second push needs --force-with-lease because
        the remote branch was already pushed with different history.
        """
        mgr = GitManager()
        clone = two_agent_clones["agent1"]
        branch = "task/fwl-retry"

        # First attempt: create branch, do work, push
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "v1.txt", "v1", "first attempt")
        mgr.push_branch(clone, branch)

        # Upstream advances (simulating another agent)
        clone2 = two_agent_clones["agent2"]
        _git(["checkout", "main"], cwd=clone2)
        _git_commit(clone2, "upstream.txt", "up", "upstream")
        _git(["push", "origin", "main"], cwd=clone2)

        # Retry: prepare_for_task rebases onto new main
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "v2.txt", "v2", "retry work")

        # Plain push would fail (history rewritten by rebase)
        with pytest.raises(GitError):
            mgr.push_branch(clone, branch)

        # force-with-lease push should succeed
        mgr.push_branch(clone, branch, force_with_lease=True)

        # Verify remote branch is updated
        remote_sha = _git(["rev-parse", f"origin/{branch}"], cwd=clone)
        local_sha = _head_sha(clone)
        assert remote_sha == local_sha

    def test_force_with_lease_rejects_concurrent_branch_update(
        self,
        two_agent_clones,
    ):
        """force-with-lease fails if another clone updated the branch."""
        mgr = GitManager()
        clone1 = two_agent_clones["agent1"]
        clone2 = two_agent_clones["agent2"]
        branch = "task/contested"

        # Clone 1 creates and pushes the branch
        mgr.create_branch(clone1, branch)
        _git_commit(clone1, "v1.txt", "v1", "clone1 push")
        mgr.push_branch(clone1, branch)

        # Clone 2 creates the same branch from remote and pushes
        _git(["fetch", "origin"], cwd=clone2)
        _git(["checkout", branch], cwd=clone2)
        _git_commit(clone2, "v2.txt", "v2", "clone2 push")
        _git(["push", "origin", branch], cwd=clone2)

        # Clone 1 does more work (local diverges from remote)
        _git(["checkout", branch], cwd=clone1)
        _git_commit(clone1, "v3.txt", "v3", "clone1 more work")

        # force-with-lease should fail (remote ref changed by clone2)
        with pytest.raises(GitError):
            mgr.push_branch(clone1, branch, force_with_lease=True)

    @pytest.mark.skip(reason="mid_chain_rebase replaced by mid_chain_sync")
    def test_mid_chain_rebase_push_uses_force_with_lease(self, two_agent_clones):
        """mid_chain_rebase(push=True) uses --force-with-lease internally."""
        mgr = GitManager()
        clone = two_agent_clones["agent1"]
        branch = "task/mcr-push"

        # Create and push the branch
        _git(["checkout", "-b", branch], cwd=clone)
        _git_commit(clone, "step1.txt", "step1", "step 1")
        _git(["push", "origin", branch], cwd=clone)

        # Advance main
        clone2 = two_agent_clones["agent2"]
        _git(["checkout", "main"], cwd=clone2)
        _git_commit(clone2, "upstream.txt", "up", "upstream")
        _git(["push", "origin", "main"], cwd=clone2)

        # mid_chain_rebase with push=True — should use force-with-lease
        result = mgr.mid_chain_rebase(clone, branch, push=True)
        assert result is True

        # Verify remote branch was updated
        remote_sha = _git(["rev-parse", f"origin/{branch}"], cwd=clone)
        local_sha = _head_sha(clone)
        assert remote_sha == local_sha

        # Log should have both commits
        log = _git(["log", "--oneline"], cwd=clone)
        assert "step 1" in log
        assert "upstream" in log


# ===========================================================================
# 6. Subtask chain drift and mid-chain rebase
# ===========================================================================


@pytest.mark.skip(reason="mid_chain_rebase replaced by mid_chain_sync; see test_git_manager.py")
class TestSubtaskChainDrift:
    """End-to-end tests for subtask chain drift reduction.

    Simulates the full subtask chain workflow:
      - Multiple subtasks share a branch.
      - Between each subtask, the branch may be rebased onto latest main.
      - The final subtask merges the accumulated branch.
    """

    def test_full_chain_with_mid_chain_rebase(self, two_agent_clones, tmp_path):
        """Full 3-step chain: each step does work, mid-chain rebase between steps."""
        mgr = GitManager()
        agent = two_agent_clones["agent1"]
        other = two_agent_clones["agent2"]
        branch = "parent/shared-branch"

        # Step 1: first subtask does work
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "step1.py", "# step 1\n", "subtask 1 work")

        # Another agent pushes between steps 1 and 2
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "concurrent1.py", "# c1\n", "concurrent 1")
        _git(["push", "origin", "main"], cwd=other)

        # Mid-chain rebase after step 1
        result = mgr.mid_chain_rebase(agent, branch)
        assert result is True

        # Step 2: next subtask switches to branch and works
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "step2.py", "# step 2\n", "subtask 2 work")

        # More concurrent work
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "concurrent2.py", "# c2\n", "concurrent 2")
        _git(["push", "origin", "main"], cwd=other)

        # Mid-chain rebase after step 2
        result = mgr.mid_chain_rebase(agent, branch)
        assert result is True

        # Step 3 (final): last subtask works then merges
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "step3.py", "# step 3\n", "subtask 3 work")

        # Final merge via sync_and_merge
        _git(["checkout", "main"], cwd=agent)
        success, err = mgr.sync_and_merge(agent, branch)
        assert success is True

        # Verify everything on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        for expected in ("subtask 1", "subtask 2", "subtask 3", "concurrent 1", "concurrent 2"):
            assert expected in log

    def test_chain_without_rebase_accumulates_drift(self, two_agent_clones):
        """Without mid-chain rebase, the branch drifts from main."""
        mgr = GitManager()
        agent = two_agent_clones["agent1"]
        other = two_agent_clones["agent2"]
        branch = "parent/no-rebase-chain"

        # Step 1
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "step1.py", "# step 1\n", "subtask 1")

        # Concurrent work
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "c1.py", "# c1\n", "concurrent 1")
        _git(["push", "origin", "main"], cwd=other)

        # No mid-chain rebase — just continue

        # Step 2
        _git_commit(agent, "step2.py", "# step 2\n", "subtask 2")

        # The branch does NOT have the concurrent commit
        log = _git(["log", "--oneline"], cwd=agent)
        assert "concurrent 1" not in log
        assert "subtask 1" in log
        assert "subtask 2" in log

    def test_chain_with_rebase_between_subtasks_flag(self, two_agent_clones):
        """switch_to_branch(rebase=True) brings in upstream changes each step."""
        mgr = GitManager()
        agent = two_agent_clones["agent1"]
        other = two_agent_clones["agent2"]
        branch = "parent/rebase-switch-chain"

        # Step 1
        mgr.switch_to_branch(agent, branch, rebase=False)
        _git_commit(agent, "step1.py", "# step 1\n", "subtask 1 work")

        # Concurrent work
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "c1.py", "# c1\n", "concurrent 1")
        _git(["push", "origin", "main"], cwd=other)

        # Step 2: switch with rebase=True
        mgr.switch_to_branch(agent, branch, rebase=True)
        log = _git(["log", "--oneline"], cwd=agent)
        assert "concurrent 1" in log  # upstream picked up
        assert "subtask 1 work" in log

    def test_mid_chain_rebase_conflict_does_not_break_chain(self, two_agent_clones):
        """If mid-chain rebase conflicts, the next subtask can still work."""
        mgr = GitManager()
        agent = two_agent_clones["agent1"]
        other = two_agent_clones["agent2"]
        branch = "parent/conflict-chain"

        # Step 1: modify README.md
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "README.md", "agent version\n", "subtask 1 README")
        sha_after_step1 = _head_sha(agent)

        # Concurrent conflicting change to same file
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "README.md", "other version\n", "concurrent README")
        _git(["push", "origin", "main"], cwd=other)

        # Mid-chain rebase: should fail due to conflict
        result = mgr.mid_chain_rebase(agent, branch)
        assert result is False

        # Branch should still be at the same commit (rebase was aborted)
        _git(["checkout", branch], cwd=agent)
        assert _head_sha(agent) == sha_after_step1

        # Step 2: agent can still do work
        _git_commit(agent, "step2.py", "# step 2\n", "subtask 2 work")
        log = _git(["log", "--oneline"], cwd=agent)
        assert "subtask 2 work" in log
        assert "subtask 1 README" in log

    def test_full_chain_final_merge_after_mid_chain_conflict(
        self,
        two_agent_clones,
        tmp_path,
    ):
        """Chain with mid-chain conflict still merges at the end via rebase fallback."""
        mgr = GitManager()
        agent = two_agent_clones["agent1"]
        other = two_agent_clones["agent2"]
        branch = "parent/end-merge-chain"

        # Step 1: work on a unique file
        mgr.switch_to_branch(agent, branch)
        _git_commit(agent, "step1.py", "# step 1\n", "step 1 work")

        # Concurrent non-conflicting change
        _git(["checkout", "main"], cwd=other)
        _git_commit(other, "other_work.py", "# other\n", "concurrent work")
        _git(["push", "origin", "main"], cwd=other)

        # Step 2: more work
        _git_commit(agent, "step2.py", "# step 2\n", "step 2 work")

        # Final merge — sync_and_merge handles the stale base
        _git(["checkout", "main"], cwd=agent)
        success, err = mgr.sync_and_merge(agent, branch)
        assert success is True

        # All work is on remote
        verify = str(tmp_path / "verify")
        subprocess.run(
            ["git", "clone", two_agent_clones["remote"], verify],
            check=True,
            capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=verify)
        assert "step 1 work" in log
        assert "step 2 work" in log
        assert "concurrent work" in log


# ===========================================================================
# 7. Orchestrator integration: _merge_and_push with sync_and_merge
# ===========================================================================


class _FakeOrchestrator:
    """Minimal stand-in for testing orchestrator methods in isolation."""

    def __init__(self, git: GitManager, config=None):
        self.git = git
        self.config = (
            config
            or type(
                "C",
                (),
                {
                    "auto_task": AutoTaskConfig(),
                },
            )()
        )
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    from src.orchestrator import Orchestrator as _Orch

    _merge_and_push = _Orch._merge_and_push
    # _mid_chain_rebase was removed; now inlined in orchestrator using git.mid_chain_sync


class TestOrchestratorMergeAndPushIntegration:
    """End-to-end orchestrator tests for _merge_and_push error handling."""

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
    async def test_merge_conflict_recovery_uses_correct_branch(self, orch, git):
        """Recovery after merge conflict uses the repo's configured default_branch."""
        git.async_and_merge = AsyncMock(return_value=(False, "merge_conflict"))
        git.arecover_workspace = AsyncMock()
        task = _make_task()
        repo = _make_repo(default_branch="develop")

        await orch._merge_and_push(task, repo, "/workspace")

        # Recovery should use "develop", not "main"
        git.arecover_workspace.assert_called_once_with("/workspace", "develop")

    @pytest.mark.asyncio
    async def test_push_failure_recovery_and_notification(self, orch, git):
        """Push failure sends detailed notification and recovers workspace."""
        git.async_and_merge = AsyncMock(
            return_value=(
                False,
                "push_failed: remote rejected (non-fast-forward)",
            )
        )
        git.arecover_workspace = AsyncMock()
        task = _make_task(id="my-task")
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=5)

        # Notification includes key details
        assert len(orch._notifications) == 1
        msg = orch._notifications[0]
        assert "Push Failed" in msg
        assert "5 attempts" in msg
        assert "diverged" in msg

        # Recovery via arecover_workspace
        git.arecover_workspace.assert_called_once_with("/workspace", "main")

    @pytest.mark.asyncio
    async def test_success_cleans_up_branch(self, orch, git):
        """Successful merge cleans up the task branch."""
        git.async_and_merge = AsyncMock(return_value=(True, ""))
        git.adelete_branch = AsyncMock()
        task = _make_task(branch_name="task/cleanup-test")
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        git.adelete_branch.assert_called_once_with(
            "/workspace",
            "task/cleanup-test",
            delete_remote=True,
        )
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_link_repo_rebase_fallback_on_conflict(self, orch, git):
        """LINK repo: rebase fallback tried when merge conflicts."""
        git.ahas_remote = AsyncMock(return_value=False)
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
        assert git.amerge_branch.call_count == 2
        assert not orch._notifications
        git.adelete_branch.assert_called_once_with(
            "/workspace",
            "task/test-branch",
            delete_remote=False,
        )


@pytest.mark.skip(reason="mid_chain_rebase replaced by mid_chain_sync; see test_git_manager.py")
class TestOrchestratorMidChainRebaseIntegration:
    """Orchestrator-level tests for mid-chain rebase wiring."""

    @pytest.fixture
    def git(self):
        return MagicMock(spec=GitManager)

    @pytest.mark.asyncio
    async def test_mid_chain_disabled_skips(self, git):
        """When mid_chain_rebase is False, no rebase is attempted."""
        config = type(
            "C",
            (),
            {
                "auto_task": AutoTaskConfig(mid_chain_rebase=False),
            },
        )()
        orch = _FakeOrchestrator(git, config)
        task = _make_task(
            branch_name="parent/branch",
            is_plan_subtask=True,
            parent_task_id="parent",
        )
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False
        git.mid_chain_rebase.assert_not_called()

    @pytest.mark.asyncio
    async def test_mid_chain_no_chain_deps_skips(self, git):
        """Without chain_dependencies, mid-chain rebase is skipped."""
        config = type(
            "C",
            (),
            {
                "auto_task": AutoTaskConfig(chain_dependencies=False),
            },
        )()
        orch = _FakeOrchestrator(git, config)
        task = _make_task(
            branch_name="parent/branch",
            is_plan_subtask=True,
            parent_task_id="parent",
        )
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False
        git.mid_chain_rebase.assert_not_called()

    @pytest.mark.asyncio
    async def test_mid_chain_success_with_push(self, git):
        """Mid-chain rebase with push enabled passes push=True."""
        git.mid_chain_rebase.return_value = True
        config = type(
            "C",
            (),
            {
                "auto_task": AutoTaskConfig(
                    mid_chain_rebase=True,
                    mid_chain_rebase_push=True,
                    chain_dependencies=True,
                ),
            },
        )()
        orch = _FakeOrchestrator(git, config)
        task = _make_task(
            branch_name="parent/branch",
            is_plan_subtask=True,
            parent_task_id="parent",
        )
        repo = _make_repo(default_branch="develop")

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is True
        git.mid_chain_rebase.assert_called_once_with(
            "/workspace",
            "parent/branch",
            "develop",
            push=True,
        )

    @pytest.mark.asyncio
    async def test_mid_chain_exception_returns_false(self, git):
        """Unexpected exceptions during mid-chain rebase return False."""
        git.mid_chain_rebase.side_effect = RuntimeError("unexpected")
        config = type(
            "C",
            (),
            {
                "auto_task": AutoTaskConfig(),
            },
        )()
        orch = _FakeOrchestrator(git, config)
        task = _make_task(
            branch_name="parent/branch",
            is_plan_subtask=True,
            parent_task_id="parent",
        )
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")
        assert result is False
