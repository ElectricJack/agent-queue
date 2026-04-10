"""Tests for git mutex serialization of shared operations.

Verifies that GitManager's lock provider mechanism correctly serializes
fetch, gc, and pull operations when multiple worktrees share the same
underlying repository (branch-isolated mode).
"""

import asyncio
import pathlib
import subprocess

import pytest

from src.git.manager import GitManager


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    (clone / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(clone), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "init"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    return {"remote": str(remote), "clone": str(clone)}


class TestLockProvider:
    """Tests for GitManager lock provider integration."""

    def test_no_lock_provider_by_default(self):
        """GitManager should have no lock provider initially."""
        mgr = GitManager()
        assert mgr._lock_provider is None

    def test_set_lock_provider(self):
        """set_lock_provider should store the callback."""
        mgr = GitManager()

        def provider(cwd):
            return None

        mgr.set_lock_provider(provider)
        assert mgr._lock_provider is provider

    def test_clear_lock_provider(self):
        """set_lock_provider(None) should clear the provider."""
        mgr = GitManager()
        mgr.set_lock_provider(lambda cwd: None)
        mgr.set_lock_provider(None)
        assert mgr._lock_provider is None

    @pytest.mark.asyncio
    async def test_arun_no_lock_for_non_serialized_commands(self, git_repo):
        """Non-serialized commands (e.g. status, log) should not acquire any lock."""
        mgr = GitManager()
        lock = asyncio.Lock()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return lock

        mgr.set_lock_provider(provider)

        # 'status' is not in _SERIALIZED_SUBCOMMANDS
        await mgr._arun(["status"], cwd=git_repo["clone"])
        assert len(calls) == 0, "Lock provider should not be called for non-serialized commands"

    @pytest.mark.asyncio
    async def test_arun_acquires_lock_for_fetch(self, git_repo):
        """fetch commands should call the lock provider and acquire the lock."""
        mgr = GitManager()
        lock = asyncio.Lock()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return lock

        mgr.set_lock_provider(provider)

        await mgr._arun(["fetch", "origin"], cwd=git_repo["clone"])
        assert len(calls) == 1
        assert calls[0] == git_repo["clone"]

    @pytest.mark.asyncio
    async def test_arun_acquires_lock_for_gc(self, git_repo):
        """gc commands should call the lock provider and acquire the lock."""
        mgr = GitManager()
        lock = asyncio.Lock()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return lock

        mgr.set_lock_provider(provider)

        await mgr._arun(["gc"], cwd=git_repo["clone"])
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_arun_acquires_lock_for_pull(self, git_repo):
        """pull commands should call the lock provider and acquire the lock."""
        mgr = GitManager()
        lock = asyncio.Lock()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return lock

        mgr.set_lock_provider(provider)

        await mgr._arun(["pull", "origin", "main"], cwd=git_repo["clone"])
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_arun_no_lock_when_provider_returns_none(self, git_repo):
        """When provider returns None, no lock should be acquired."""
        mgr = GitManager()

        def provider(cwd):
            return None  # No lock for this path

        mgr.set_lock_provider(provider)

        # Should work without any lock
        await mgr._arun(["fetch", "origin"], cwd=git_repo["clone"])

    @pytest.mark.asyncio
    async def test_arun_no_lock_when_cwd_is_none(self, git_repo):
        """When cwd is None, the lock provider should not be called."""
        mgr = GitManager()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        # cwd=None -- provider should not be called
        # (git will use the process cwd, but lock provider can't resolve it)
        try:
            await mgr._arun(["fetch", "origin"])
        except Exception:
            pass  # May fail since cwd might not be a git repo

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_arun_unlocked_bypasses_lock(self, git_repo):
        """_arun_unlocked should never consult the lock provider."""
        mgr = GitManager()
        calls = []

        def provider(cwd):
            calls.append(cwd)
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        await mgr._arun_unlocked(["fetch", "origin"], cwd=git_repo["clone"])
        assert len(calls) == 0, "_arun_unlocked should bypass the lock provider"


class TestFetchSerialization:
    """Test that concurrent fetch operations are properly serialized."""

    @pytest.mark.asyncio
    async def test_concurrent_fetches_serialized(self, git_repo):
        """Two concurrent fetches on the same repo should not overlap."""
        mgr = GitManager()
        lock = asyncio.Lock()
        execution_log: list[tuple[str, str]] = []  # (event, cwd)

        original_arun_unlocked = mgr._arun_unlocked

        async def tracking_arun(args, cwd=None, timeout=None):
            if args and args[0] == "fetch":
                execution_log.append(("start", cwd or ""))
                result = await original_arun_unlocked(args, cwd, timeout)
                execution_log.append(("end", cwd or ""))
                return result
            return await original_arun_unlocked(args, cwd, timeout)

        mgr._arun_unlocked = tracking_arun
        mgr.set_lock_provider(lambda cwd: lock)

        # Run two fetches concurrently
        await asyncio.gather(
            mgr._arun(["fetch", "origin"], cwd=git_repo["clone"]),
            mgr._arun(["fetch", "origin"], cwd=git_repo["clone"]),
        )

        # With serialization, the events should be strictly interleaved:
        # start, end, start, end (never start, start, end, end)
        assert len(execution_log) == 4
        assert execution_log[0][0] == "start"
        assert execution_log[1][0] == "end"
        assert execution_log[2][0] == "start"
        assert execution_log[3][0] == "end"

    @pytest.mark.asyncio
    async def test_concurrent_fetches_not_serialized_without_provider(self, git_repo):
        """Without a lock provider, fetches should run concurrently."""
        mgr = GitManager()
        execution_log: list[tuple[str, str]] = []
        event = asyncio.Event()

        original_arun_unlocked = mgr._arun_unlocked

        async def tracking_arun(args, cwd=None, timeout=None):
            if args and args[0] == "fetch":
                execution_log.append(("start", cwd or ""))
                # Signal that we've started, wait briefly for the other to start
                event.set()
                result = await original_arun_unlocked(args, cwd, timeout)
                execution_log.append(("end", cwd or ""))
                return result
            return await original_arun_unlocked(args, cwd, timeout)

        mgr._arun_unlocked = tracking_arun

        # No lock provider — fetches may overlap
        await asyncio.gather(
            mgr._arun(["fetch", "origin"], cwd=git_repo["clone"]),
            mgr._arun(["fetch", "origin"], cwd=git_repo["clone"]),
        )

        # Without serialization, both should still complete (4 events)
        assert len(execution_log) == 4

    @pytest.mark.asyncio
    async def test_different_repos_not_serialized(self, git_repo, tmp_path):
        """Fetches on different repos should use different locks (no blocking)."""
        # Create a second clone
        clone2 = tmp_path / "clone2"
        subprocess.run(
            ["git", "clone", git_repo["remote"], str(clone2)],
            check=True,
            capture_output=True,
        )

        mgr = GitManager()
        locks = {
            git_repo["clone"]: asyncio.Lock(),
            str(clone2): asyncio.Lock(),
        }

        def provider(cwd):
            return locks.get(cwd)

        mgr.set_lock_provider(provider)

        execution_log: list[tuple[str, str]] = []
        original_arun_unlocked = mgr._arun_unlocked

        async def tracking_arun(args, cwd=None, timeout=None):
            if args and args[0] == "fetch":
                execution_log.append(("start", cwd or ""))
                result = await original_arun_unlocked(args, cwd, timeout)
                execution_log.append(("end", cwd or ""))
                return result
            return await original_arun_unlocked(args, cwd, timeout)

        mgr._arun_unlocked = tracking_arun

        # Two fetches on different repos — different locks, so they can overlap
        await asyncio.gather(
            mgr._arun(["fetch", "origin"], cwd=git_repo["clone"]),
            mgr._arun(["fetch", "origin"], cwd=str(clone2)),
        )

        # Both should complete
        assert len(execution_log) == 4


class TestSerializedSubcommands:
    """Verify the set of serialized subcommands."""

    def test_fetch_is_serialized(self):
        assert "fetch" in GitManager._SERIALIZED_SUBCOMMANDS

    def test_gc_is_serialized(self):
        assert "gc" in GitManager._SERIALIZED_SUBCOMMANDS

    def test_pull_is_serialized(self):
        assert "pull" in GitManager._SERIALIZED_SUBCOMMANDS

    def test_push_is_not_serialized(self):
        """Push operates on per-branch refs and doesn't need serialization."""
        assert "push" not in GitManager._SERIALIZED_SUBCOMMANDS

    def test_checkout_is_not_serialized(self):
        assert "checkout" not in GitManager._SERIALIZED_SUBCOMMANDS

    def test_commit_is_not_serialized(self):
        assert "commit" not in GitManager._SERIALIZED_SUBCOMMANDS


class TestWorktreeBasePathResolution:
    """Test the orchestrator's static _get_worktree_base_path method.

    Imported from orchestrator to verify the resolution logic that the
    lock provider depends on.
    """

    def test_worktree_path_resolves_to_base(self):
        from src.orchestrator import Orchestrator

        base = Orchestrator._get_worktree_base_path("/repos/.worktrees-myrepo/task-123/")
        assert base == "/repos/myrepo"

    def test_non_worktree_path_returns_none(self):
        from src.orchestrator import Orchestrator

        base = Orchestrator._get_worktree_base_path("/repos/myrepo")
        assert base is None

    def test_worktree_path_no_trailing_slash(self):
        from src.orchestrator import Orchestrator

        base = Orchestrator._get_worktree_base_path("/repos/.worktrees-myrepo/task-123")
        assert base == "/repos/myrepo"


class TestGitManagerMethodsSerialization:
    """Test that GitManager async methods that call fetch are serialized."""

    @pytest.mark.asyncio
    async def test_apull_latest_main_serialized(self, git_repo):
        """apull_latest_main calls fetch, which should be serialized."""
        mgr = GitManager()
        calls = []

        def provider(cwd):
            calls.append(("fetch_lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)
        await mgr.apull_latest_main(git_repo["clone"], "main")

        # Should have called the provider for the fetch
        fetch_calls = [c for c in calls if c[0] == "fetch_lock"]
        assert len(fetch_calls) >= 1

    @pytest.mark.asyncio
    async def test_aprepare_for_task_serialized(self, git_repo):
        """aprepare_for_task calls fetch, which should be serialized."""
        mgr = GitManager()
        calls = []

        def provider(cwd):
            calls.append(("fetch_lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)
        await mgr.aprepare_for_task(git_repo["clone"], "task/test-branch", "main")

        fetch_calls = [c for c in calls if c[0] == "fetch_lock"]
        assert len(fetch_calls) >= 1

    @pytest.mark.asyncio
    async def test_aswitch_to_branch_serialized(self, git_repo):
        """aswitch_to_branch calls fetch + pull, both should be serialized."""
        mgr = GitManager()
        clone = git_repo["clone"]
        calls = []

        def provider(cwd):
            calls.append(("lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        # Create the branch first
        _git(["checkout", "-b", "test-switch"], cwd=clone)
        _git(["checkout", "main"], cwd=clone)

        await mgr.aswitch_to_branch(clone, "test-switch", "main")

        # Should have called provider for both fetch and pull
        assert len(calls) >= 2

    @pytest.mark.asyncio
    async def test_amerge_branch_serialized(self, git_repo, tmp_path):
        """amerge_branch calls fetch, which should be serialized."""
        mgr = GitManager()
        clone = git_repo["clone"]
        calls = []

        def provider(cwd):
            calls.append(("lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        # Create a branch with changes
        _git(["checkout", "-b", "merge-test"], cwd=clone)
        pathlib.Path(clone, "merge-file.txt").write_text("merge content")
        _git(["add", "."], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "merge commit"],
            cwd=clone,
        )

        await mgr.amerge_branch(clone, "merge-test", "main")

        fetch_calls = [c for c in calls if c[0] == "lock"]
        assert len(fetch_calls) >= 1

    @pytest.mark.asyncio
    async def test_async_and_merge_serialized(self, git_repo, tmp_path):
        """async_and_merge calls fetch, which should be serialized."""
        mgr = GitManager()
        clone = git_repo["clone"]
        calls = []

        def provider(cwd):
            calls.append(("lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        # Create a branch with changes
        _git(["checkout", "-b", "sync-merge-test"], cwd=clone)
        pathlib.Path(clone, "sync-file.txt").write_text("sync content")
        _git(["add", "."], cwd=clone)
        _git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@t.com",
                "commit",
                "-m",
                "sync commit",
            ],
            cwd=clone,
        )

        await mgr.async_and_merge(clone, "sync-merge-test", "main")

        # Should have called provider for fetch (and possibly pull)
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_amid_chain_sync_serialized(self, git_repo, tmp_path):
        """amid_chain_sync calls fetch, which should be serialized."""
        mgr = GitManager()
        clone = git_repo["clone"]
        calls = []

        def provider(cwd):
            calls.append(("lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)

        # Create and checkout a branch
        _git(["checkout", "-b", "chain-test"], cwd=clone)
        pathlib.Path(clone, "chain-file.txt").write_text("chain content")
        _git(["add", "."], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "chain commit"],
            cwd=clone,
        )

        await mgr.amid_chain_sync(clone, "chain-test", "main")

        fetch_calls = [c for c in calls if c[0] == "lock"]
        assert len(fetch_calls) >= 1
