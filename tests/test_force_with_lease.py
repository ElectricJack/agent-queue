"""Tests for --force-with-lease support in push_branch.

Phase 5 of the workspace-sync plan: task branches (used for PRs) should be
pushed with ``--force-with-lease`` so that retried tasks and subtask chains
that previously pushed intermediate results don't fail with non-fast-forward
errors.

Coverage:
  - ``push_branch(force_with_lease=False)`` (default) uses plain push.
  - ``push_branch(force_with_lease=True)`` uses ``--force-with-lease``.
  - Integration test: push succeeds on a previously-pushed branch when
    force_with_lease is enabled, and fails without it.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from src.git.manager import GitError, GitManager
from tests.git_seed_helpers import clone_seed, init_bare_seed


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
    """Create a bare remote + working clone for this test."""
    remote = tmp_path / "remote.git"
    init_bare_seed(str(remote))
    clone = tmp_path / "clone"
    clone_seed(str(remote), str(clone))
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

    def test_push_pushes_the_branch_even_when_a_same_named_tag_exists(self, git_repo):
        """``refs/heads/<name>`` is pushed, so a planted tag can neither shadow nor block."""
        mgr = GitManager()
        clone = git_repo["clone"]

        _git(["checkout", "-b", "feature/shadowed"], cwd=clone)
        branch_tip = _git_commit(clone, "file.txt", "content", "add file")
        _git(["checkout", "--detach", "main"], cwd=clone)
        decoy_tip = _git_commit(clone, "decoy.txt", "decoy", "decoy")
        _git(["tag", "feature/shadowed", decoy_tip], cwd=clone)
        _git(["checkout", "feature/shadowed"], cwd=clone)

        mgr.push_branch(clone, "feature/shadowed")

        assert _git(["rev-parse", "origin/feature/shadowed"], cwd=clone) == branch_tip

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
