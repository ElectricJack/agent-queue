"""get_git_status diff stat must diff against the project's default branch.

``GitPlugin._git_diff_stat`` (src/plugins/internal/git.py) used to ask git for
origin's HEAD and diff ``merge-base origin/<that>..HEAD`` against it.  For a
project whose ``repo_default_branch`` is not origin HEAD (e.g. matter-engine-cpp
on ``vg-vt-improvements``) the diff stat for a task branch then included every
project-branch commit relative to main, overstating the task's change.  The
configured branch now wins, matching ``WorktreeSlotManager._default_branch``
after quick-willow; detection stays the fallback only when the value is unset.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from src.plugins.internal.git import GitPlugin


def _git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


@pytest.fixture
def branch_repo(tmp_path: Path) -> Path:
    """Bare origin with ``main`` and ``vg-vt-improvements`` (ahead of main).

    ``origin``'s HEAD points at ``main``; a task branch is cut from the
    project branch and adds one file, so diffing against ``origin/main``
    would also pick up the project-branch commit.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    (repo / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    _git(["push", "origin", "main"], cwd=repo)

    _git(["switch", "-c", "vg-vt-improvements"], cwd=repo)
    (repo / "proj.txt").write_text("project branch work\n")
    _git(["add", "proj.txt"], cwd=repo)
    _git(["commit", "-m", "project branch work"], cwd=repo)
    _git(["push", "origin", "vg-vt-improvements"], cwd=repo)
    project_tip = _git(["rev-parse", "HEAD"], cwd=repo)

    _git(["switch", "-c", "aq/task"], cwd=repo)
    (repo / "task.txt").write_text("task change\n")
    _git(["add", "task.txt"], cwd=repo)
    _git(["commit", "-m", "task work"], cwd=repo)
    assert _git(["rev-parse", "main"], cwd=repo) != project_tip
    assert _git(["rev-parse", "HEAD"], cwd=repo) != _git(["rev-parse", "main"], cwd=repo)
    return repo


class _FakeGit:
    """Async double: records _arun invocations, serves the real ref as HEAD."""

    def __init__(self, detected_branch: str):
        self.detected_branch = detected_branch
        self.arun_calls: list[list[str]] = []

    async def aget_default_branch(self, ws_path, **kwargs):
        return self.detected_branch

    async def _arun(self, args, *, cwd=None):
        self.arun_calls.append(list(args))
        return _git(args, cwd)


def _diff_stat(git, repo: Path, branch: str, project_branch: str | None) -> str:
    return asyncio.run(
        GitPlugin._git_diff_stat(git, str(repo), branch, project_branch=project_branch)
    )


class TestDiffStatProjectBranch:
    def test_configured_branch_wins(self, branch_repo: Path):
        """The recorded project branch anchors the diff, not origin HEAD."""
        git = _FakeGit(detected_branch="main")
        stat = _diff_stat(git, branch_repo, "aq/task", "vg-vt-improvements")
        assert "task.txt" in stat
        assert "proj.txt" not in stat
        # The merge-base anchor used was the remote project branch, not main.
        assert git.arun_calls[0][:2] == ["merge-base", "origin/vg-vt-improvements"]

    def test_unset_branch_falls_back_to_detection(self, branch_repo: Path):
        """No configured branch: origin HEAD detection anchors the diff."""
        git = _FakeGit(detected_branch="main")
        stat = _diff_stat(git, branch_repo, "aq/task", None)
        # Detection returns main, so the project-branch commit is included.
        assert "task.txt" in stat
        assert "proj.txt" in stat
        assert git.arun_calls[0][:2] == ["merge-base", "origin/main"]

    def test_on_the_project_branch_itself_is_empty(self, branch_repo: Path):
        """Branch == configured default: no diff, no git calls into the stat."""
        git = _FakeGit(detected_branch="main")
        assert _diff_stat(git, branch_repo, "vg-vt-improvements", "vg-vt-improvements") == ""
        assert git.arun_calls == []
