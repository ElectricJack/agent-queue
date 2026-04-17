"""Tests for branch-isolated workspace mode (Roadmap 7.4.4).

End-to-end and integration tests covering the full lifecycle of
branch-isolated workspace sharing.  These tests exercise the orchestrator,
database, and git layers together — unlike the database-only tests in
``test_database.py::TestBranchIsolatedWorkspaceMode`` and the git mutex
unit tests in ``test_git_mutex.py``.

Test cases (a)–(g) from the roadmap:
  (a) Two agents acquire workspace with lock_mode="branch-isolated" on same
      repo — both succeed.
  (b) Each agent operates on a separate branch (no cross-branch interference).
  (c) Shared git operations (fetch, gc) are serialized via mutex — concurrent
      fetches do not corrupt the repo.
  (d) Agent A's commits on branch-A are not visible on agent B's branch-B.
  (e) Branch-isolated lock is released when agent completes task.
  (f) Three or more agents can work concurrently in branch-isolated mode.
  (g) Branch-isolated mode with conflicting branches (same branch name) is
      rejected.
"""

import asyncio
import os
import subprocess

import pytest

from src.adapters.base import AgentAdapter
from src.config import AppConfig
from src.git.manager import GitManager
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceMode,
)
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> str:
    """Run a git command synchronously and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: str, *, bare: bool = False) -> None:
    """Initialise a git repo with an initial commit on ``main``."""
    if bare:
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", path],
            check=True,
            capture_output=True,
        )
        return

    os.makedirs(path, exist_ok=True)
    _git(["init", "--initial-branch=main"], cwd=path)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@t.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=path,
    )


def _clone_repo(remote: str, dest: str) -> None:
    """Clone *remote* into *dest*."""
    subprocess.run(
        ["git", "clone", remote, dest],
        check=True,
        capture_output=True,
    )


def _commit_file(repo: str, filename: str, content: str, message: str) -> str:
    """Write *content* to *filename* in *repo*, commit, and return the SHA."""
    filepath = os.path.join(repo, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    _git(["add", filename], cwd=repo)
    _git(
        ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message],
        cwd=repo,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo)


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockAdapter(AgentAdapter):
    """Controllable adapter for testing."""

    def __init__(self, result=AgentResult.COMPLETED, tokens=1000, on_wait=None):
        self._result = result
        self._tokens = tokens
        self._on_wait = on_wait
        self._ctx = None

    async def start(self, task):
        self._ctx = task

    async def wait(self, on_message=None):
        if self._on_wait:
            await self._on_wait(self._ctx)
        return AgentOutput(result=self._result, summary="Done", tokens_used=self._tokens)

    async def stop(self):
        pass

    async def is_alive(self):
        return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000, on_wait=None):
        self.result = result
        self.tokens = tokens
        self.on_wait = on_wait
        self.create_calls = []

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        self.create_calls.append({"agent_type": agent_type, "profile": profile})
        return MockAdapter(result=self.result, tokens=self.tokens, on_wait=self.on_wait)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _drain_running_tasks(orch: Orchestrator) -> None:
    """Wait for all background tasks launched by the orchestrator to complete."""
    if orch._running_tasks:
        await asyncio.gather(*orch._running_tasks.values(), return_exceptions=True)
        orch._running_tasks.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote and a working clone for testing.

    Returns a dict with ``remote`` and ``clone`` paths.
    """
    remote = str(tmp_path / "remote.git")
    clone = str(tmp_path / "clone")

    _init_repo(remote, bare=True)
    _clone_repo(remote, clone)
    _commit_file(clone, "README.md", "initial", "init")
    _git(["push", "origin", "main"], cwd=clone)

    return {"remote": remote, "clone": clone, "tmp_path": tmp_path}


@pytest.fixture
async def orch(tmp_path):
    """Create an orchestrator with mock adapters and a fresh database."""
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    o = Orchestrator(config, adapter_factory=MockAdapterFactory())
    await o.initialize()
    yield o
    await _drain_running_tasks(o)
    await o.shutdown()


# ---------------------------------------------------------------------------
# (a) Two agents acquire workspace with lock_mode="branch-isolated"
#     on same repo — both succeed
# ---------------------------------------------------------------------------


class TestTwoAgentsAcquireBranchIsolated:
    """(a) Two agents acquire workspace with lock_mode='branch-isolated' on
    same repo — both succeed."""

    async def test_both_agents_acquire_successfully(self, orch, git_repo):
        """First agent locks the workspace directly; second agent gets a
        worktree.  Both end up with usable workspaces."""
        db = orch.db
        clone = git_repo["clone"]

        # Set up project + workspace + two agents + two tasks
        await db.create_project(
            Project(
                id="p-1",
                name="alpha",
                repo_url=git_repo["remote"],
                repo_default_branch="main",
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Task One",
                description="First task",
                status=TaskStatus.READY,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
        )
        await db.create_task(
            Task(
                id="t-2",
                project_id="p-1",
                title="Task Two",
                description="Second task",
                status=TaskStatus.READY,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
        )

        agent1 = await db.get_agent("a-1")
        agent2 = await db.get_agent("a-2")
        task1 = await db.get_task("t-1")
        task2 = await db.get_task("t-2")

        # Mock git to avoid real clone operations (workspace already exists)
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        mock_git.make_branch_name = GitManager.make_branch_name
        mock_git.slugify = GitManager.slugify
        mock_git.acreate_worktree = AsyncMock()
        mock_git.aremove_worktree = AsyncMock()
        mock_git.set_lock_provider = MagicMock()
        orch.git = mock_git

        # Agent 1 acquires workspace — should get the original workspace
        ws1_path = await orch._prepare_workspace(task1, agent1)
        assert ws1_path is not None
        assert ws1_path == clone

        # Agent 2 acquires workspace — original is locked, so it should get a worktree
        ws2_path = await orch._prepare_workspace(task2, agent2)
        assert ws2_path is not None
        assert ws2_path != ws1_path  # Different workspace path
        assert ".worktrees-" in ws2_path  # Worktree path convention

    async def test_db_level_branch_isolated_compatible(self, orch):
        """Two BRANCH_ISOLATED locks on the same path are compatible at DB level."""
        db = orch.db
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        # Two workspaces pointing to the same path (different projects)
        for i, pid in enumerate(["p-1", "p-2"], 1):
            await db.create_workspace(
                Workspace(
                    id=f"ws-{i}",
                    project_id=pid,
                    workspace_path="/tmp/shared-repo",
                    source_type=RepoSourceType.LINK,
                )
            )

        ws1 = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )

        assert ws1 is not None
        assert ws2 is not None
        assert ws1.lock_mode == WorkspaceMode.BRANCH_ISOLATED
        assert ws2.lock_mode == WorkspaceMode.BRANCH_ISOLATED


# ---------------------------------------------------------------------------
# (b) Each agent operates on a separate branch (no cross-branch interference)
# ---------------------------------------------------------------------------


class TestSeparateBranches:
    """(b) Each agent operates on a separate branch (no cross-branch
    interference)."""

    async def test_worktrees_get_separate_branches(self, git_repo):
        """Git worktrees created from the same repo work on independent branches."""
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        # Create two worktrees from the clone
        wt1 = str(tmp / ".worktrees-clone" / "task-1")
        wt2 = str(tmp / ".worktrees-clone" / "task-2")

        mgr = GitManager()
        await mgr.acreate_worktree(clone, wt1, "branch-a")
        await mgr.acreate_worktree(clone, wt2, "branch-b")

        # Each worktree should be on its own branch
        branch_a = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt1)
        branch_b = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt2)
        assert branch_a == "branch-a"
        assert branch_b == "branch-b"

        # Commit a file in worktree 1
        _commit_file(wt1, "file-a.txt", "content-a", "commit on branch-a")

        # The file should NOT exist in worktree 2
        assert not os.path.exists(os.path.join(wt2, "file-a.txt"))

        # Commit a file in worktree 2
        _commit_file(wt2, "file-b.txt", "content-b", "commit on branch-b")

        # The file should NOT exist in worktree 1
        assert not os.path.exists(os.path.join(wt1, "file-b.txt"))

        # Clean up
        await mgr.aremove_worktree(clone, wt1)
        await mgr.aremove_worktree(clone, wt2)

    async def test_branch_names_derived_from_task(self, orch, git_repo):
        """Each task gets a unique branch name derived from task.id + task.title."""
        branch1 = GitManager.make_branch_name("t-1", "Add login page")
        branch2 = GitManager.make_branch_name("t-2", "Fix auth bug")

        assert branch1 != branch2
        assert "t-1" in branch1
        assert "t-2" in branch2


# ---------------------------------------------------------------------------
# (c) Shared git operations (fetch, gc) are serialized via mutex
# ---------------------------------------------------------------------------


class TestGitMutexSerialization:
    """(c) Shared git operations (fetch, gc) are serialized via mutex —
    concurrent fetches do not corrupt the repo."""

    async def test_concurrent_fetches_serialized_via_orchestrator_mutex(self, git_repo):
        """Two concurrent fetch operations through the orchestrator's git
        mutex are serialized (never overlap)."""
        clone = git_repo["clone"]

        mgr = GitManager()
        mutex = asyncio.Lock()
        execution_log: list[tuple[str, str]] = []

        original_arun_unlocked = mgr._arun_unlocked

        async def tracking_arun(args, cwd=None, timeout=None):
            if args and args[0] == "fetch":
                execution_log.append(("start", cwd or ""))
                # Add a small delay so timing-dependent overlap is visible
                await asyncio.sleep(0.01)
                result = await original_arun_unlocked(args, cwd, timeout)
                execution_log.append(("end", cwd or ""))
                return result
            return await original_arun_unlocked(args, cwd, timeout)

        mgr._arun_unlocked = tracking_arun
        mgr.set_lock_provider(lambda cwd: mutex)

        # Two concurrent fetches — should be serialized by the lock
        await asyncio.gather(
            mgr._arun(["fetch", "origin"], cwd=clone),
            mgr._arun(["fetch", "origin"], cwd=clone),
        )

        # Verify strict serialization: start, end, start, end
        assert len(execution_log) == 4
        assert execution_log[0][0] == "start"
        assert execution_log[1][0] == "end"
        assert execution_log[2][0] == "start"
        assert execution_log[3][0] == "end"

    async def test_orchestrator_resolve_git_lock_worktree(self, tmp_path):
        """The orchestrator's _resolve_git_lock maps worktree paths to the
        base workspace's mutex."""
        config = AppConfig(
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        o = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await o.initialize()

        # Register a mutex for the base workspace
        base_path = "/repos/myrepo"
        o._git_mutex(base_path)

        # A worktree path should resolve to the same mutex
        worktree_path = "/repos/.worktrees-myrepo/task-123"
        lock = o._resolve_git_lock(worktree_path)
        assert lock is not None
        assert lock is o._git_mutexes[base_path]

        # A non-worktree path with no registered mutex should return None
        lock2 = o._resolve_git_lock("/some/other/path")
        assert lock2 is None

        await o.shutdown()

    async def test_gc_also_serialized(self, git_repo):
        """gc operations are also serialized via the same lock."""
        clone = git_repo["clone"]
        mgr = GitManager()
        calls = []

        def provider(cwd):
            calls.append(("lock", cwd))
            return asyncio.Lock()

        mgr.set_lock_provider(provider)
        await mgr._arun(["gc"], cwd=clone)

        assert len(calls) == 1
        assert calls[0][0] == "lock"


# ---------------------------------------------------------------------------
# (d) Agent A's commits on branch-A are not visible on agent B's branch-B
# ---------------------------------------------------------------------------


class TestCommitIsolation:
    """(d) Agent A's commits on branch-A are not visible on agent B's
    branch-B."""

    async def test_commits_isolated_between_worktrees(self, git_repo):
        """Commits made in one worktree are invisible from the other."""
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        wt_a = str(tmp / ".worktrees-clone" / "agent-a")
        wt_b = str(tmp / ".worktrees-clone" / "agent-b")

        mgr = GitManager()
        await mgr.acreate_worktree(clone, wt_a, "branch-a")
        await mgr.acreate_worktree(clone, wt_b, "branch-b")

        # Agent A commits several files
        sha_a1 = _commit_file(wt_a, "module_a.py", "def func_a(): pass", "Add module A")
        sha_a2 = _commit_file(wt_a, "tests/test_a.py", "test_a", "Add test A")

        # Agent B commits different files
        sha_b1 = _commit_file(wt_b, "module_b.py", "def func_b(): pass", "Add module B")

        # Verify branch-A commits are NOT visible on branch-B
        log_b = _git(["log", "--oneline"], cwd=wt_b)
        assert sha_a1[:7] not in log_b
        assert sha_a2[:7] not in log_b

        # Verify branch-B commits are NOT visible on branch-A
        log_a = _git(["log", "--oneline"], cwd=wt_a)
        assert sha_b1[:7] not in log_a

        # Verify files are physically absent in the other worktree
        assert not os.path.exists(os.path.join(wt_b, "module_a.py"))
        assert not os.path.exists(os.path.join(wt_b, "tests/test_a.py"))
        assert not os.path.exists(os.path.join(wt_a, "module_b.py"))

        # Clean up
        await mgr.aremove_worktree(clone, wt_a)
        await mgr.aremove_worktree(clone, wt_b)

    async def test_shared_initial_history(self, git_repo):
        """Both worktrees share the initial commit history but diverge from there."""
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        # Get the initial commit SHA
        init_sha = _git(["rev-parse", "HEAD"], cwd=clone)

        wt_a = str(tmp / ".worktrees-clone" / "agent-a")
        wt_b = str(tmp / ".worktrees-clone" / "agent-b")

        mgr = GitManager()
        await mgr.acreate_worktree(clone, wt_a, "branch-a")
        await mgr.acreate_worktree(clone, wt_b, "branch-b")

        # Both branches should contain the initial commit
        log_a = _git(["log", "--format=%H"], cwd=wt_a)
        log_b = _git(["log", "--format=%H"], cwd=wt_b)
        assert init_sha in log_a
        assert init_sha in log_b

        # After committing, they should diverge
        _commit_file(wt_a, "a.txt", "a", "A commit")
        _commit_file(wt_b, "b.txt", "b", "B commit")

        head_a = _git(["rev-parse", "HEAD"], cwd=wt_a)
        head_b = _git(["rev-parse", "HEAD"], cwd=wt_b)
        assert head_a != head_b

        await mgr.aremove_worktree(clone, wt_a)
        await mgr.aremove_worktree(clone, wt_b)


# ---------------------------------------------------------------------------
# (e) Branch-isolated lock is released when agent completes task
# ---------------------------------------------------------------------------


class TestLockReleasedOnCompletion:
    """(e) Branch-isolated lock is released when agent completes task."""

    async def test_release_workspaces_for_task_cleans_up_worktree(self, orch, git_repo):
        """_release_workspaces_for_task removes worktree workspaces and deletes
        the DB record."""
        db = orch.db
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        # Set up project + workspace
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="Base", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="Worktree", description="D"))

        # Create base workspace locked by agent-1
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED)

        # Create a worktree workspace locked by agent-2
        wt_path = str(tmp / ".worktrees-clone" / "task-2")
        os.makedirs(wt_path, exist_ok=True)
        await db.create_workspace(
            Workspace(
                id="ws-wt-1",
                project_id="p-1",
                workspace_path=wt_path,
                source_type=RepoSourceType.WORKTREE,
                name="worktree:ws-1",
            )
        )
        await db.acquire_workspace(
            "p-1",
            "a-2",
            "t-2",
            preferred_workspace_id="ws-wt-1",
            lock_mode=WorkspaceMode.BRANCH_ISOLATED,
        )

        # Mock git to avoid real worktree removal
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.aremove_worktree = AsyncMock()
        orch.git = mock_git

        # Release workspaces for task t-2 (the worktree task)
        await orch._release_workspaces_for_task("t-2")

        # The worktree workspace should be deleted
        ws_wt = await db.get_workspace("ws-wt-1")
        assert ws_wt is None, "Worktree workspace record should be deleted"

        # The base workspace should still be locked by agent-1
        ws_base = await db.get_workspace("ws-1")
        assert ws_base is not None
        assert ws_base.locked_by_agent_id == "a-1"

    async def test_regular_workspace_released_not_deleted(self, orch):
        """Releasing a non-worktree BRANCH_ISOLATED workspace only releases
        the lock — it does not delete the workspace record."""
        db = orch.db
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/test-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # Acquire with BRANCH_ISOLATED
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None

        # Release via orchestrator
        await orch._release_workspaces_for_task("t-1")

        # Workspace should still exist but be unlocked
        ws_after = await db.get_workspace("ws-1")
        assert ws_after is not None
        assert ws_after.locked_by_agent_id is None
        assert ws_after.lock_mode is None

    async def test_lock_released_allows_reacquisition(self, orch):
        """After releasing a BRANCH_ISOLATED lock, the workspace can be
        acquired again."""
        db = orch.db
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/test-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # Agent 1 acquires and releases
        ws1 = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws1 is not None
        await db.release_workspace("ws-1")

        # Agent 2 can now acquire
        ws2 = await db.acquire_workspace(
            "p-1", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is not None
        assert ws2.locked_by_agent_id == "a-2"


# ---------------------------------------------------------------------------
# (f) Three or more agents can work concurrently in branch-isolated mode
# ---------------------------------------------------------------------------


class TestThreeOrMoreAgentsConcurrent:
    """(f) Three or more agents can work concurrently in branch-isolated mode."""

    async def test_three_agents_on_same_repo(self, orch, git_repo):
        """Three agents all get workspaces on the same repo — one gets the
        original workspace, two get worktrees."""
        db = orch.db
        clone = git_repo["clone"]

        await db.create_project(
            Project(
                id="p-1",
                name="alpha",
                repo_url=git_repo["remote"],
                repo_default_branch="main",
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )

        agents = []
        tasks = []
        for i in range(1, 4):
            await db.create_agent(Agent(id=f"a-{i}", name=f"agent-{i}", agent_type="claude"))
            await db.create_task(
                Task(
                    id=f"t-{i}",
                    project_id="p-1",
                    title=f"Task {i}",
                    description=f"Task {i} desc",
                    status=TaskStatus.READY,
                    workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
                )
            )
            agents.append(await db.get_agent(f"a-{i}"))
            tasks.append(await db.get_task(f"t-{i}"))

        # Mock git
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        mock_git.make_branch_name = GitManager.make_branch_name
        mock_git.slugify = GitManager.slugify
        mock_git.acreate_worktree = AsyncMock()
        mock_git.aremove_worktree = AsyncMock()
        mock_git.set_lock_provider = MagicMock()
        orch.git = mock_git

        # All three agents acquire workspaces
        workspace_paths = []
        for task, agent in zip(tasks, agents):
            ws_path = await orch._prepare_workspace(task, agent)
            assert ws_path is not None, f"Agent {agent.id} failed to acquire workspace"
            workspace_paths.append(ws_path)

        # All paths should be unique
        assert len(set(workspace_paths)) == 3, (
            f"Expected 3 unique workspace paths, got {workspace_paths}"
        )

        # First agent gets the clone, others get worktrees
        assert workspace_paths[0] == clone
        for path in workspace_paths[1:]:
            assert ".worktrees-" in path

    async def test_three_agents_db_level(self, orch):
        """Three BRANCH_ISOLATED locks on the same path via separate projects
        all succeed at the database level."""
        db = orch.db

        for i in range(1, 4):
            await db.create_project(Project(id=f"p-{i}", name=f"project-{i}"))
            await db.create_agent(Agent(id=f"a-{i}", name=f"agent-{i}", agent_type="claude"))
            await db.create_task(
                Task(id=f"t-{i}", project_id=f"p-{i}", title=f"Task {i}", description="D")
            )
            await db.create_workspace(
                Workspace(
                    id=f"ws-{i}",
                    project_id=f"p-{i}",
                    workspace_path="/tmp/shared-monorepo",
                    source_type=RepoSourceType.LINK,
                )
            )

        results = []
        for i in range(1, 4):
            ws = await db.acquire_workspace(
                f"p-{i}",
                f"a-{i}",
                f"t-{i}",
                lock_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
            results.append(ws)

        assert all(ws is not None for ws in results)
        assert all(ws.lock_mode == WorkspaceMode.BRANCH_ISOLATED for ws in results)
        assert len({ws.locked_by_agent_id for ws in results}) == 3

    async def test_three_real_worktrees_concurrent(self, git_repo):
        """Three real git worktrees from the same repo can operate
        simultaneously without interference."""
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        mgr = GitManager()
        wt_paths = []
        for i in range(1, 4):
            wt = str(tmp / ".worktrees-clone" / f"agent-{i}")
            await mgr.acreate_worktree(clone, wt, f"branch-{i}")
            wt_paths.append(wt)

        # Each worktree should be on its own branch
        for i, wt in enumerate(wt_paths, 1):
            branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
            assert branch == f"branch-{i}"

        # Commit in all three simultaneously
        for i, wt in enumerate(wt_paths, 1):
            _commit_file(wt, f"agent{i}.txt", f"agent {i} content", f"Agent {i} commit")

        # No cross-contamination
        for i, wt in enumerate(wt_paths, 1):
            for j in range(1, 4):
                if j != i:
                    assert not os.path.exists(os.path.join(wt, f"agent{j}.txt"))

        # Clean up
        for wt in wt_paths:
            await mgr.aremove_worktree(clone, wt)


# ---------------------------------------------------------------------------
# (g) Branch-isolated mode with conflicting branches (same branch name)
#     is rejected
# ---------------------------------------------------------------------------


class TestConflictingBranchRejected:
    """(g) Branch-isolated mode with conflicting branches (same branch name)
    is rejected."""

    async def test_duplicate_worktree_branch_rejected_by_git(self, git_repo):
        """Creating a second worktree with the same branch name as an existing
        worktree is rejected by git."""
        clone = git_repo["clone"]
        tmp = git_repo["tmp_path"]

        mgr = GitManager()
        wt1 = str(tmp / ".worktrees-clone" / "agent-1")
        wt2 = str(tmp / ".worktrees-clone" / "agent-2")

        # First worktree succeeds
        await mgr.acreate_worktree(clone, wt1, "conflicting-branch")

        # Second worktree with the same branch name should fail
        from src.git.manager import GitError

        with pytest.raises(GitError):
            await mgr.acreate_worktree(clone, wt2, "conflicting-branch")

        # Clean up the successful worktree
        await mgr.aremove_worktree(clone, wt1)

    async def test_worktree_creation_failure_returns_none(self, orch, git_repo):
        """When _create_branch_isolated_worktree fails due to a git error
        (e.g. duplicate branch name), it returns None."""
        db = orch.db
        clone = git_repo["clone"]

        await db.create_project(Project(id="p-1", name="alpha", repo_url=git_repo["remote"]))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="First Task",
                description="D",
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
        )
        await db.create_task(
            Task(
                id="t-2",
                project_id="p-1",
                title="Second Task",
                description="D",
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
        )

        # Create and lock base workspace
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED)

        # Mock git.acreate_worktree to raise GitError (simulating duplicate branch)
        from unittest.mock import AsyncMock, MagicMock
        from src.git.manager import GitError

        mock_git = MagicMock()
        mock_git.acreate_worktree = AsyncMock(side_effect=GitError("fatal: branch already exists"))
        mock_git.make_branch_name = GitManager.make_branch_name
        mock_git.slugify = GitManager.slugify
        orch.git = mock_git

        task2 = await db.get_task("t-2")
        agent2 = await db.get_agent("a-2")
        project = await db.get_project("p-1")

        result = await orch._create_branch_isolated_worktree(task2, agent2, project)
        assert result is None

    async def test_each_task_gets_unique_branch_name(self):
        """Different tasks always produce different branch names, preventing
        branch conflicts."""
        branch_a = GitManager.make_branch_name("t-1", "Feature A")
        branch_b = GitManager.make_branch_name("t-2", "Feature A")  # Same title, different task ID
        branch_c = GitManager.make_branch_name("t-1", "Feature B")  # Same task ID, different title

        # All should be unique
        branches = {branch_a, branch_b, branch_c}
        assert len(branches) == 3


# ---------------------------------------------------------------------------
# Additional integration tests
# ---------------------------------------------------------------------------


class TestWorktreeBasePathResolution:
    """Tests for _get_worktree_base_path static method."""

    def test_standard_worktree_path(self):
        base = Orchestrator._get_worktree_base_path("/repos/.worktrees-myrepo/task-123/")
        assert base == "/repos/myrepo"

    def test_no_trailing_slash(self):
        base = Orchestrator._get_worktree_base_path("/repos/.worktrees-myrepo/task-123")
        assert base == "/repos/myrepo"

    def test_non_worktree_path(self):
        base = Orchestrator._get_worktree_base_path("/repos/myrepo")
        assert base is None

    def test_nested_worktree_path(self):
        base = Orchestrator._get_worktree_base_path(
            "/home/user/dev/.worktrees-agent-queue/t-42-fix-bug/"
        )
        assert base == "/home/user/dev/agent-queue"


class TestWorktreeCleanup:
    """Tests for worktree cleanup on task completion and daemon restart."""

    async def test_cleanup_worktree_workspace_calls_git_and_deletes_record(self, orch):
        """_cleanup_worktree_workspace removes the git worktree, releases
        the lock, and deletes the workspace record."""
        db = orch.db

        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))

        wt_path = "/repos/.worktrees-myrepo/task-1"
        await db.create_workspace(
            Workspace(
                id="ws-wt-1",
                project_id="p-1",
                workspace_path=wt_path,
                source_type=RepoSourceType.WORKTREE,
                name="worktree:ws-1",
            )
        )
        await db.acquire_workspace(
            "p-1",
            "a-1",
            "t-1",
            preferred_workspace_id="ws-wt-1",
            lock_mode=WorkspaceMode.BRANCH_ISOLATED,
        )

        # Mock git
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.aremove_worktree = AsyncMock()
        orch.git = mock_git

        ws = await db.get_workspace("ws-wt-1")
        await orch._cleanup_worktree_workspace(ws)

        # Verify git worktree remove was called
        mock_git.aremove_worktree.assert_awaited_once_with("/repos/myrepo", wt_path)

        # Workspace record should be deleted
        ws_after = await db.get_workspace("ws-wt-1")
        assert ws_after is None

    async def test_release_workspace_and_cleanup_regular_vs_worktree(self, orch):
        """_release_workspace_and_cleanup behaves differently for WORKTREE
        vs regular workspaces."""
        db = orch.db

        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))

        # Regular workspace
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/repos/myrepo",
                source_type=RepoSourceType.LINK,
            )
        )
        ws_regular = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )

        # Worktree workspace
        await db.create_workspace(
            Workspace(
                id="ws-wt-1",
                project_id="p-1",
                workspace_path="/repos/.worktrees-myrepo/task-2",
                source_type=RepoSourceType.WORKTREE,
                name="worktree:ws-1",
            )
        )
        ws_worktree = await db.acquire_workspace(
            "p-1",
            "a-2",
            "t-2",
            preferred_workspace_id="ws-wt-1",
            lock_mode=WorkspaceMode.BRANCH_ISOLATED,
        )

        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.aremove_worktree = AsyncMock()
        orch.git = mock_git

        # Release worktree — should delete the record
        await orch._release_workspace_and_cleanup(ws_worktree)
        assert await db.get_workspace("ws-wt-1") is None

        # Release regular — should only unlock, record persists
        await orch._release_workspace_and_cleanup(ws_regular)
        ws1_after = await db.get_workspace("ws-1")
        assert ws1_after is not None
        assert ws1_after.locked_by_agent_id is None

    async def test_exclusive_blocked_by_branch_isolated(self, orch):
        """An EXCLUSIVE request on a path already locked with BRANCH_ISOLATED
        is rejected."""
        db = orch.db
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        for i, pid in enumerate(["p-1", "p-2"], 1):
            await db.create_workspace(
                Workspace(
                    id=f"ws-{i}",
                    project_id=pid,
                    workspace_path="/tmp/shared-repo",
                    source_type=RepoSourceType.LINK,
                )
            )

        # Lock with BRANCH_ISOLATED
        ws1 = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws1 is not None

        # EXCLUSIVE should be blocked
        ws2 = await db.acquire_workspace("p-2", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws2 is None

    async def test_branch_isolated_blocked_by_exclusive(self, orch):
        """A BRANCH_ISOLATED request on a path already locked with EXCLUSIVE
        is rejected."""
        db = orch.db
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="agent-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        for i, pid in enumerate(["p-1", "p-2"], 1):
            await db.create_workspace(
                Workspace(
                    id=f"ws-{i}",
                    project_id=pid,
                    workspace_path="/tmp/shared-repo",
                    source_type=RepoSourceType.LINK,
                )
            )

        # Lock with EXCLUSIVE
        ws1 = await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws1 is not None

        # BRANCH_ISOLATED should be blocked
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is None


class TestGitMutexRegistration:
    """Tests for git mutex registration and lookup."""

    async def test_mutex_created_for_branch_isolated_workspace(self, orch, git_repo):
        """_prepare_workspace registers a git mutex for BRANCH_ISOLATED
        workspaces."""
        db = orch.db
        clone = git_repo["clone"]

        await db.create_project(
            Project(id="p-1", name="alpha", repo_url=git_repo["remote"], repo_default_branch="main")
        )
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="BI Task",
                description="D",
                status=TaskStatus.READY,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
        )

        agent = await db.get_agent("a-1")
        task = await db.get_task("t-1")

        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        mock_git.make_branch_name = GitManager.make_branch_name
        mock_git.set_lock_provider = MagicMock()
        orch.git = mock_git

        # Before: no mutex registered
        assert clone not in orch._git_mutexes

        await orch._prepare_workspace(task, agent)

        # After: mutex registered for the workspace path
        assert clone in orch._git_mutexes
        assert isinstance(orch._git_mutexes[clone], asyncio.Lock)

    async def test_no_mutex_for_exclusive_workspace(self, orch, git_repo):
        """_prepare_workspace does NOT register a git mutex for EXCLUSIVE
        workspaces."""
        db = orch.db
        clone = git_repo["clone"]

        await db.create_project(
            Project(id="p-1", name="alpha", repo_url=git_repo["remote"], repo_default_branch="main")
        )
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path=clone,
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.create_agent(Agent(id="a-1", name="agent-1", agent_type="claude"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Exclusive Task",
                description="D",
                status=TaskStatus.READY,
                # workspace_mode defaults to None → EXCLUSIVE
            )
        )

        agent = await db.get_agent("a-1")
        task = await db.get_task("t-1")

        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.avalidate_checkout = AsyncMock(return_value=True)
        mock_git.ahas_remote = AsyncMock(return_value=True)
        mock_git.ahas_uncommitted_changes = AsyncMock(return_value=False)
        mock_git._arun = AsyncMock(return_value="")
        mock_git.aforce_clean_workspace = AsyncMock(return_value=True)
        mock_git.make_branch_name = GitManager.make_branch_name
        mock_git.set_lock_provider = MagicMock()
        orch.git = mock_git

        await orch._prepare_workspace(task, agent)

        # No mutex should be registered for exclusive mode
        assert clone not in orch._git_mutexes
