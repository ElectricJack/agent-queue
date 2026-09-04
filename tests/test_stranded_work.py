"""``preserve_unpushed_work`` — the push-before-you-close rule.

Real git repositories throughout (a bare "origin" plus clones), because
every interesting case here is a git question — detached HEAD, a branch with
no upstream, a remote branch that diverged — and a mocked GitManager would
only assert that the code calls the methods the code calls.

See ``src/orchestrator/stranded_work.py`` for the outage this guards.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from src.git.manager import GitManager
from src.orchestrator.stranded_work import preserve_unpushed_work


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: str, name: str, body: str = "x") -> str:
    pathlib.Path(repo, name).write_text(body)
    _git(["add", name], cwd=repo)
    _git(["commit", "-m", f"add {name}"], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def origin(tmp_path):
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", bare],
        check=True,
        capture_output=True,
    )
    return bare


@pytest.fixture
def clone(tmp_path, origin):
    path = str(tmp_path / "clone")
    subprocess.run(["git", "clone", origin, path], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=path)
    _git(["config", "user.email", "t@t.com"], cwd=path)
    _commit(path, "README.md", "init")
    _git(["push", "origin", "main"], cwd=path)
    return path


@pytest.fixture
def git():
    return GitManager()


async def test_clean_workspace_reports_clean(clone, git):
    result = await preserve_unpushed_work(git, clone, "task-1")
    assert result.status == "clean"
    assert result.at_risk is False


async def test_unpushed_commits_land_on_the_task_branch(clone, origin, git):
    _git(["checkout", "-b", "aq/task-1"], cwd=clone)
    sha = _commit(clone, "work.py", "print(1)")

    result = await preserve_unpushed_work(git, clone, "task-1")

    assert result.status == "pushed"
    assert result.branch == "aq/task-1"
    assert result.commit == sha
    assert result.count == 1
    assert _git(["rev-parse", "refs/heads/aq/task-1"], cwd=origin) == sha


async def test_detached_head_is_rescued_too(clone, origin, git):
    """A slot left on a detached HEAD is the shape ``@{u}`` cannot see."""
    _git(["checkout", "--detach"], cwd=clone)
    sha = _commit(clone, "work.py", "print(1)")

    result = await preserve_unpushed_work(git, clone, "task-2")

    assert result.status == "pushed"
    assert result.branch == "aq/task-2"
    assert _git(["rev-parse", "refs/heads/aq/task-2"], cwd=origin) == sha


async def test_rescue_is_not_delivery_and_preserves_daemon_owned_paths(clone, origin, git):
    """Recovery saves the complete commit; delivery validation happens elsewhere."""
    _git(["checkout", "-b", "aq/task-rescue"], cwd=clone)
    reserved = pathlib.Path(clone, ".aq-worktree.json")
    reserved.write_text('{"workspace": "recovery evidence"}\n')
    _git(["add", "-f", ".aq-worktree.json"], cwd=clone)
    _git(["commit", "-m", "preserve recovery evidence"], cwd=clone)
    sha = _git(["rev-parse", "HEAD"], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-rescue")

    assert result.status == "pushed"
    assert _git(["rev-parse", "refs/heads/aq/task-rescue"], cwd=origin) == sha
    assert _git(["show", "aq/task-rescue:.aq-worktree.json"], cwd=origin) == (
        '{"workspace": "recovery evidence"}'
    )


async def test_branch_pushed_under_another_name_counts_as_safe(clone, git):
    """Commits already on *some* remote branch are not stranded."""
    _git(["checkout", "-b", "feature/x"], cwd=clone)
    _commit(clone, "work.py", "print(1)")
    _git(["push", "origin", "feature/x"], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-3")

    assert result.status == "clean"


async def test_diverged_remote_branch_is_never_overwritten(clone, origin, tmp_path, git):
    """Another run already owns ``aq/task-4`` — the rescue goes to ``-wip``."""
    other = str(tmp_path / "other")
    subprocess.run(["git", "clone", origin, other], check=True, capture_output=True)
    _git(["config", "user.name", "Other"], cwd=other)
    _git(["config", "user.email", "o@o.com"], cwd=other)
    _git(["checkout", "-b", "aq/task-4"], cwd=other)
    theirs = _commit(other, "theirs.py", "other")
    _git(["push", "origin", "aq/task-4"], cwd=other)

    _git(["checkout", "-b", "aq/task-4"], cwd=clone)
    mine = _commit(clone, "mine.py", "mine")

    result = await preserve_unpushed_work(git, clone, "task-4")

    assert result.status == "pushed"
    assert result.branch == "aq/task-4-wip"
    assert _git(["rev-parse", "refs/heads/aq/task-4"], cwd=origin) == theirs
    assert _git(["rev-parse", "refs/heads/aq/task-4-wip"], cwd=origin) == mine


async def test_fast_forward_over_our_own_earlier_push_reuses_the_branch(clone, origin, git):
    _git(["checkout", "-b", "aq/task-5"], cwd=clone)
    _commit(clone, "one.py")
    _git(["push", "origin", "aq/task-5"], cwd=clone)
    second = _commit(clone, "two.py")

    result = await preserve_unpushed_work(git, clone, "task-5")

    assert result.branch == "aq/task-5"
    assert _git(["rev-parse", "refs/heads/aq/task-5"], cwd=origin) == second


async def test_repository_with_no_remote_reports_no_remote(tmp_path, git):
    local = str(tmp_path / "local")
    pathlib.Path(local).mkdir()
    _git(["init", "--initial-branch=main"], cwd=local)
    _git(["config", "user.name", "Test"], cwd=local)
    _git(["config", "user.email", "t@t.com"], cwd=local)
    sha = _commit(local, "a.py")

    result = await preserve_unpushed_work(git, local, "task-6")

    assert result.status == "no_remote"
    assert result.branch == "main"
    assert result.commit == sha
    assert result.at_risk is True


async def test_push_failure_is_reported_not_swallowed(clone, tmp_path, git):
    _git(["checkout", "-b", "aq/task-7"], cwd=clone)
    _commit(clone, "work.py")
    # Point origin at a path that does not exist: every push fails.
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=clone)

    result = await preserve_unpushed_work(git, clone, "task-7")

    assert result.status == "push_failed"
    assert result.at_risk is True
    assert result.count == 1
    assert result.error


async def test_missing_workspace_is_unknown_not_clean(tmp_path, git):
    result = await preserve_unpushed_work(git, str(tmp_path / "nope"), "task-8")
    assert result.status == "unknown"
    assert result.at_risk is False
