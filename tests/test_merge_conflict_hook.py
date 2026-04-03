"""Tests for the merge conflict detection hook and script."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
CHECK_SCRIPT = SCRIPTS_DIR / "check-merge-conflicts.sh"


def _run_script(repo_path: str) -> tuple[int, dict]:
    """Run check-merge-conflicts.sh and return (exit_code, parsed_json)."""
    result = subprocess.run(
        ["bash", str(CHECK_SCRIPT), repo_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout.strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        data = {"raw_output": output, "stderr": result.stderr}
    return result.returncode, data


def _git(repo: str, *args: str, check: bool = True) -> str:
    """Run a git command in the given repo."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare 'remote' and a clone to simulate origin/main + branches."""
    bare = str(tmp_path / "bare.git")
    clone = str(tmp_path / "clone")

    # Create bare repo with 'main' as initial branch
    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", bare)

    # Clone it
    _git(str(tmp_path), "clone", bare, clone)

    # Configure git user for commits
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")

    # Ensure we're on main branch
    _git(clone, "checkout", "-b", "main", check=False)

    # Initial commit on main
    test_file = os.path.join(clone, "file.txt")
    with open(test_file, "w") as f:
        f.write("line 1\nline 2\nline 3\n")
    _git(clone, "add", "file.txt")
    _git(clone, "commit", "-m", "Initial commit")
    _git(clone, "push", "-u", "origin", "main")

    return clone


class TestCheckMergeConflictsScript:
    """Test the check-merge-conflicts.sh script."""

    def test_no_branches_returns_clean(self, git_repo):
        """With only main, the script should report clean status."""
        exit_code, data = _run_script(git_repo)
        assert exit_code == 0
        assert data["status"] == "clean"
        assert data["conflicts"] == []

    def test_clean_branch_no_conflicts(self, git_repo):
        """A branch that can merge cleanly should not be reported."""
        # Create a branch with non-conflicting changes
        _git(git_repo, "checkout", "-b", "test-task/add-feature")
        with open(os.path.join(git_repo, "new_file.txt"), "w") as f:
            f.write("new content\n")
        _git(git_repo, "add", "new_file.txt")
        _git(git_repo, "commit", "-m", "Add new file")
        _git(git_repo, "push", "origin", "test-task/add-feature")
        _git(git_repo, "checkout", "main")

        exit_code, data = _run_script(git_repo)
        assert exit_code == 0
        assert data["status"] == "clean"
        assert data["checked"] == 1
        assert data["clean"] == 1

    def test_conflicting_branch_detected(self, git_repo):
        """A branch that conflicts with main should be reported."""
        # Create a branch that modifies file.txt
        _git(git_repo, "checkout", "-b", "brave-fox/fix-auth-bug")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("branch change line 1\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Branch change")
        _git(git_repo, "push", "origin", "brave-fox/fix-auth-bug")

        # Go back to main and make a conflicting change
        _git(git_repo, "checkout", "main")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("main change line 1\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Main change")
        _git(git_repo, "push", "origin", "main")

        exit_code, data = _run_script(git_repo)
        assert exit_code == 1
        assert data["status"] == "conflicts_found"
        assert data["conflict_count"] == 1
        assert len(data["conflicts"]) == 1

        conflict = data["conflicts"][0]
        assert conflict["branch"] == "brave-fox/fix-auth-bug"
        assert conflict["task_id"] == "brave-fox"
        assert conflict["description"] == "fix-auth-bug"

    def test_multiple_branches_mixed(self, git_repo):
        """Test with both clean and conflicting branches."""
        # Create a clean branch
        _git(git_repo, "checkout", "-b", "clean-task/no-conflict")
        with open(os.path.join(git_repo, "clean_file.txt"), "w") as f:
            f.write("no conflict here\n")
        _git(git_repo, "add", "clean_file.txt")
        _git(git_repo, "commit", "-m", "Clean change")
        _git(git_repo, "push", "origin", "clean-task/no-conflict")

        # Create a conflicting branch
        _git(git_repo, "checkout", "main")
        _git(git_repo, "checkout", "-b", "bad-task/will-conflict")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("conflict from branch\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Conflicting change")
        _git(git_repo, "push", "origin", "bad-task/will-conflict")

        # Update main to create the conflict
        _git(git_repo, "checkout", "main")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("conflict from main\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Main conflicting change")
        _git(git_repo, "push", "origin", "main")

        exit_code, data = _run_script(git_repo)
        assert exit_code == 1
        assert data["status"] == "conflicts_found"
        assert data["checked"] == 2
        assert data["clean"] == 1
        assert data["conflict_count"] == 1
        assert data["conflicts"][0]["task_id"] == "bad-task"

    def test_branch_without_slash_uses_full_name_as_task_id(self, git_repo):
        """Branches without a slash should use the full name as task_id."""
        # Create a conflicting branch with no slash
        _git(git_repo, "checkout", "-b", "simple-branch")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("simple branch change\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Simple branch")
        _git(git_repo, "push", "origin", "simple-branch")

        _git(git_repo, "checkout", "main")
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("main different change\nline 2\nline 3\n")
        _git(git_repo, "add", "file.txt")
        _git(git_repo, "commit", "-m", "Main different")
        _git(git_repo, "push", "origin", "main")

        exit_code, data = _run_script(git_repo)
        assert exit_code == 1
        conflict = data["conflicts"][0]
        assert conflict["task_id"] == "simple-branch"
        assert conflict["description"] == "simple-branch"
