"""Tests for the async API of GitManager.

Mirrors key tests from test_git_manager.py but exercises the async methods
(_arun, _arun_subprocess, and all a-prefixed public methods).
"""

import asyncio
import pathlib
import subprocess

import pytest
from src.git.manager import GitManager, GitError


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_file(clone: str, filename: str, content: str, message: str) -> str:
    pathlib.Path(clone, filename).write_text(content)
    _git(["add", filename], cwd=clone)
    _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message], cwd=clone)
    return _git(["rev-parse", "HEAD"], cwd=clone)


@pytest.fixture
def bare_repo(tmp_path):
    """Create a bare repo to act as 'origin'."""
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", bare], check=True, capture_output=True
    )
    return bare


@pytest.fixture
def clone(tmp_path, bare_repo):
    """Clone the bare repo and add an initial commit."""
    clone_path = str(tmp_path / "clone")
    subprocess.run(["git", "clone", bare_repo, clone_path], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=clone_path)
    _git(["config", "user.email", "t@t.com"], cwd=clone_path)
    pathlib.Path(clone_path, "README.md").write_text("init")
    _git(["add", "."], cwd=clone_path)
    _git(["commit", "-m", "init"], cwd=clone_path)
    _git(["push", "origin", "main"], cwd=clone_path)
    return clone_path


@pytest.fixture
def mgr():
    return GitManager()


# ------------------------------------------------------------------
# _arun basic tests
# ------------------------------------------------------------------


class TestArun:
    @pytest.mark.asyncio
    async def test_arun_returns_stdout(self, clone, mgr):
        result = await mgr._arun(["rev-parse", "--git-dir"], cwd=clone)
        assert result == ".git"

    @pytest.mark.asyncio
    async def test_arun_raises_on_failure(self, clone, mgr):
        with pytest.raises(GitError, match="failed"):
            await mgr._arun(["checkout", "nonexistent-branch"], cwd=clone)

    @pytest.mark.asyncio
    async def test_arun_timeout(self, clone, mgr, monkeypatch):
        """Timeout should raise GitError, not asyncio.TimeoutError."""
        import unittest.mock as mock

        async def _slow_communicate():
            await asyncio.sleep(10)
            return b"", b""

        original_create = asyncio.create_subprocess_exec

        async def mock_create(*args, **kwargs):
            proc = await original_create(*args, **kwargs)
            proc.communicate = _slow_communicate
            # Wrap kill() to tolerate the process already being gone
            original_kill = proc.kill

            def safe_kill():
                try:
                    original_kill()
                except ProcessLookupError:
                    pass

            proc.kill = safe_kill
            return proc

        with mock.patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with pytest.raises(GitError, match="timed out"):
                await mgr._arun(["status"], cwd=clone, timeout=1)


# ------------------------------------------------------------------
# _arun_subprocess tests
# ------------------------------------------------------------------


class TestArunSubprocess:
    @pytest.mark.asyncio
    async def test_returns_completed_process(self, mgr):
        result = await mgr._arun_subprocess(["git", "--version"])
        assert result.returncode == 0
        assert "git version" in result.stdout

    @pytest.mark.asyncio
    async def test_nonzero_returncode(self, tmp_path, mgr):
        # Use a valid dir but invalid git command to get non-zero exit
        result = await mgr._arun_subprocess(
            ["git", "log", "--oneline", "-1"],
            cwd=str(tmp_path),
        )
        assert result.returncode != 0


# ------------------------------------------------------------------
# Async public methods
# ------------------------------------------------------------------


class TestAsyncValidateCheckout:
    @pytest.mark.asyncio
    async def test_valid(self, clone, mgr):
        assert await mgr.avalidate_checkout(clone) is True

    @pytest.mark.asyncio
    async def test_invalid(self, tmp_path, mgr):
        assert await mgr.avalidate_checkout(str(tmp_path / "nope")) is False


class TestAsyncGetCurrentBranch:
    @pytest.mark.asyncio
    async def test_returns_branch(self, clone, mgr):
        branch = await mgr.aget_current_branch(clone)
        assert branch == "main"


class TestAsyncGetStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self, clone, mgr):
        status = await mgr.aget_status(clone)
        assert "nothing to commit" in status or "working tree clean" in status


class TestAsyncCreateBranch:
    @pytest.mark.asyncio
    async def test_creates_and_switches(self, clone, mgr):
        await mgr.acreate_branch(clone, "feature-x")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "feature-x"

    @pytest.mark.asyncio
    async def test_existing_branch_switches(self, clone, mgr):
        await mgr.acreate_branch(clone, "feature-x")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.acreate_branch(clone, "feature-x")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "feature-x"


class TestAsyncCommitAll:
    @pytest.mark.asyncio
    async def test_commit_with_changes(self, clone, mgr):
        pathlib.Path(clone, "newfile.txt").write_text("hello")
        committed = await mgr.acommit_all(clone, "add newfile")
        assert committed is True

    @pytest.mark.asyncio
    async def test_no_changes(self, clone, mgr):
        committed = await mgr.acommit_all(clone, "nothing")
        assert committed is False


class TestAsyncPrepareForTask:
    @pytest.mark.asyncio
    async def test_creates_branch(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/test-branch")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "task/test-branch"

    @pytest.mark.asyncio
    async def test_existing_branch_reuse(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/reuse")
        pathlib.Path(clone, "work.txt").write_text("work")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "work"], cwd=clone
        )
        # Go back to main and prepare again — should reuse branch
        _git(["checkout", "main"], cwd=clone)
        await mgr.aprepare_for_task(clone, "task/reuse")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "task/reuse"


class TestAsyncPushBranch:
    @pytest.mark.asyncio
    async def test_push(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/push-test")
        pathlib.Path(clone, "pushed.txt").write_text("data")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "push test"],
            cwd=clone,
        )
        await mgr.apush_branch(clone, "task/push-test")

    @pytest.mark.asyncio
    async def test_force_with_lease(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/fwl")
        pathlib.Path(clone, "f.txt").write_text("1")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "first"], cwd=clone
        )
        await mgr.apush_branch(clone, "task/fwl")
        # Amend and force push
        pathlib.Path(clone, "f.txt").write_text("2")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@t.com",
                "commit",
                "--amend",
                "-m",
                "amended",
            ],
            cwd=clone,
        )
        await mgr.apush_branch(clone, "task/fwl", force_with_lease=True)


class TestAsyncMergeBranch:
    @pytest.mark.asyncio
    async def test_clean_merge(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/merge-test")
        pathlib.Path(clone, "feature.txt").write_text("feature")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "feature"],
            cwd=clone,
        )
        result = await mgr.amerge_branch(clone, "task/merge-test")
        assert result is True


class TestAsyncGetDefaultBranch:
    @pytest.mark.asyncio
    async def test_returns_main(self, clone, mgr):
        branch = await mgr.aget_default_branch(clone)
        assert branch == "main"


class TestAsyncGetDiff:
    @pytest.mark.asyncio
    async def test_returns_diff(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/diff-test")
        pathlib.Path(clone, "changed.txt").write_text("changed")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "change"],
            cwd=clone,
        )
        diff = await mgr.aget_diff(clone, "main")
        assert "changed.txt" in diff


class TestAsyncListBranches:
    @pytest.mark.asyncio
    async def test_lists_branches(self, clone, mgr):
        await mgr.acreate_branch(clone, "branch-a")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.acreate_branch(clone, "branch-b")
        branches = await mgr.alist_branches(clone)
        names = [b.lstrip("* ") for b in branches]
        assert "branch-a" in names
        assert "branch-b" in names


class TestAsyncRecoverWorkspace:
    @pytest.mark.asyncio
    async def test_recovers(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/recover")
        await mgr.arecover_workspace(clone)
        branch = await mgr.aget_current_branch(clone)
        assert branch == "main"


class TestAsyncDeleteBranch:
    @pytest.mark.asyncio
    async def test_deletes_local(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/delete-me")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.adelete_branch(clone, "task/delete-me", delete_remote=False)
        branches = await mgr.alist_branches(clone)
        names = [b.lstrip("* ") for b in branches]
        assert "task/delete-me" not in names


class TestAsyncHasRemote:
    @pytest.mark.asyncio
    async def test_has_origin(self, clone, mgr):
        assert await mgr.ahas_remote(clone) is True

    @pytest.mark.asyncio
    async def test_no_remote(self, clone, mgr):
        assert await mgr.ahas_remote(clone, "nonexistent") is False


class TestAsyncGetRecentCommits:
    @pytest.mark.asyncio
    async def test_returns_commits(self, clone, mgr):
        commits = await mgr.aget_recent_commits(clone)
        assert "init" in commits
