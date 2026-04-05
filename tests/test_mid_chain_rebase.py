"""Tests for optional mid-chain rebase in subtask chains.

Covers:
- ``GitManager.mid_chain_rebase()`` — the new method for rebasing task
  branches onto latest main between subtask completions.
- ``Orchestrator._mid_chain_rebase()`` — integration with the subtask
  completion workflow.
- Config-driven enable/disable via ``auto_task.mid_chain_rebase``.
- Integration tests with real git repos.

NOTE: These tests target the old mid_chain_rebase API which has been replaced
by mid_chain_sync. The functionality is now covered by tests in
test_git_manager.py (TestMidChainSync, TestSubtaskChainDriftAndMidChainRebase).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="mid_chain_rebase API replaced by mid_chain_sync; "
    "see test_git_manager.py for current coverage"
)

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AutoTaskConfig
from src.git.manager import GitError, GitManager
from src.models import RepoConfig, RepoSourceType, Task, TaskStatus, Workspace


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
        id="subtask-2",
        project_id="proj-1",
        title="Subtask 2",
        description="desc",
        branch_name="parent-task/feature-branch",
        is_plan_subtask=True,
        parent_task_id="parent-task",
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
def git_repo(tmp_path):
    """Create a bare remote + working clone with initial commit."""
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


@pytest.fixture
def two_agent_clones(tmp_path):
    """Bare remote with two clones simulating concurrent agents."""
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


# ===========================================================================
# Unit tests: GitManager.mid_chain_rebase()
# ===========================================================================


class TestMidChainRebase:
    """Test mid_chain_rebase() method on GitManager."""

    def test_successful_rebase_onto_updated_main(self, git_repo):
        """Branch is rebased onto latest origin/main after main advances."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with a commit
        _git(["checkout", "-b", "task/feature"], cwd=clone)
        _git_commit(clone, "feature.py", "print('hello')", "add feature")
        task_sha_before = _head_sha(clone)

        # Advance main on origin (simulate another agent's merge)
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "other.py", "# other", "other agent's work")
        _git(["push", "origin", "main"], cwd=clone)
        main_sha = _head_sha(clone)

        # mid_chain_rebase should put task branch on top of latest main
        result = mgr.mid_chain_rebase(clone, "task/feature", "main")

        assert result is True
        assert _current_branch(clone) == "task/feature"
        # After rebase, the task branch should include the main commit
        log = _git(["log", "--oneline"], cwd=clone)
        assert "other agent's work" in log
        assert "add feature" in log

    def test_rebase_no_op_when_already_up_to_date(self, git_repo):
        """Rebase succeeds (returns True) when branch is already on latest main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        _git(["checkout", "-b", "task/feature"], cwd=clone)
        _git_commit(clone, "feature.py", "code", "add feature")

        result = mgr.mid_chain_rebase(clone, "task/feature", "main")

        assert result is True
        assert _current_branch(clone) == "task/feature"

    def test_rebase_conflict_aborts_cleanly(self, git_repo):
        """When rebase has conflicts, it aborts and returns False."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch editing README.md
        _git(["checkout", "-b", "task/conflict"], cwd=clone)
        _git_commit(clone, "README.md", "task version", "task changes README")
        task_sha = _head_sha(clone)

        # Advance main with a conflicting change to the same file
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "README.md", "main version", "main changes README")
        _git(["push", "origin", "main"], cwd=clone)

        # Rebase should fail due to conflict
        result = mgr.mid_chain_rebase(clone, "task/conflict", "main")

        assert result is False
        # Branch should still exist and be on the original commit
        _git(["checkout", "task/conflict"], cwd=clone)
        assert _head_sha(clone) == task_sha

    def test_rebase_with_push(self, git_repo):
        """With push=True, rebased branch is pushed to remote."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create and push a branch
        _git(["checkout", "-b", "task/push-test"], cwd=clone)
        _git_commit(clone, "file.py", "v1", "initial work")
        _git(["push", "origin", "task/push-test"], cwd=clone)

        # Advance main
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "other.py", "stuff", "main advance")
        _git(["push", "origin", "main"], cwd=clone)

        # Rebase with push
        result = mgr.mid_chain_rebase(
            clone,
            "task/push-test",
            "main",
            push=True,
        )

        assert result is True
        # Verify remote branch was updated
        remote_sha = _git(
            ["rev-parse", "origin/task/push-test"],
            cwd=clone,
        )
        _git(["checkout", "task/push-test"], cwd=clone)
        local_sha = _head_sha(clone)
        assert remote_sha == local_sha

    def test_rebase_without_push(self, git_repo):
        """With push=False (default), branch is not pushed to remote."""
        mgr = GitManager()
        clone = git_repo["clone"]

        _git(["checkout", "-b", "task/no-push"], cwd=clone)
        _git_commit(clone, "file.py", "v1", "initial work")
        _git(["push", "origin", "task/no-push"], cwd=clone)
        original_remote_sha = _git(
            ["rev-parse", "origin/task/no-push"],
            cwd=clone,
        )

        # Advance main
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "other.py", "stuff", "main advance")
        _git(["push", "origin", "main"], cwd=clone)

        # Rebase without push (default)
        result = mgr.mid_chain_rebase(clone, "task/no-push", "main")

        assert result is True
        # Remote should still have the old SHA (not updated)
        remote_sha_after = _git(
            ["rev-parse", "origin/task/no-push"],
            cwd=clone,
        )
        assert remote_sha_after == original_remote_sha

    def test_fetch_failure_returns_false(self):
        """If fetch fails (no remote), mid_chain_rebase returns False."""
        mgr = GitManager()
        mgr._run = MagicMock(side_effect=GitError("fetch failed"))

        result = mgr.mid_chain_rebase("/fake/path", "task/branch", "main")
        assert result is False

    def test_push_keyword_only(self):
        """The push parameter must be keyword-only."""
        import inspect

        sig = inspect.signature(GitManager.mid_chain_rebase)
        param = sig.parameters["push"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


class TestMidChainRebaseMultiAgent:
    """Integration tests: mid-chain rebase reduces drift when main moves."""

    def test_drift_reduced_after_other_agent_merges(self, two_agent_clones):
        """After agent2 pushes to main, agent1's task branch is rebased."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Agent 1 creates a subtask branch and does work
        _git(["checkout", "-b", "task/subtask-chain"], cwd=agent1)
        _git_commit(agent1, "subtask1.py", "# step 1", "subtask 1 work")

        # Agent 2 completes different work and pushes to main
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "agent2-feature.py", "# agent2", "agent2 feature")
        _git(["push", "origin", "main"], cwd=agent2)

        # Agent 1 does mid-chain rebase
        result = mgr.mid_chain_rebase(agent1, "task/subtask-chain", "main")

        assert result is True
        # Agent 1's branch should now include agent2's commit
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "agent2 feature" in log
        assert "subtask 1 work" in log

    def test_multi_step_chain_with_rebases(self, two_agent_clones):
        """Simulate a 3-step subtask chain with rebases between each step."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]

        # Step 1: subtask 1 does work
        _git(["checkout", "-b", "task/chain"], cwd=agent1)
        _git_commit(agent1, "step1.py", "# step 1", "subtask 1")

        # Another agent pushes to main between steps
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent1.py", "# c1", "concurrent work 1")
        _git(["push", "origin", "main"], cwd=agent2)

        # Mid-chain rebase after step 1
        assert mgr.mid_chain_rebase(agent1, "task/chain", "main") is True

        # Step 2: subtask 2 does work on the same branch
        _git(["checkout", "task/chain"], cwd=agent1)
        _git_commit(agent1, "step2.py", "# step 2", "subtask 2")

        # More concurrent work
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent2.py", "# c2", "concurrent work 2")
        _git(["push", "origin", "main"], cwd=agent2)

        # Mid-chain rebase after step 2
        assert mgr.mid_chain_rebase(agent1, "task/chain", "main") is True

        # Step 3: subtask 3 (final) does work
        _git(["checkout", "task/chain"], cwd=agent1)
        _git_commit(agent1, "step3.py", "# step 3", "subtask 3")

        # Final merge should be clean (close to fast-forward)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "subtask 1" in log
        assert "subtask 2" in log
        assert "subtask 3" in log
        assert "concurrent work 1" in log
        assert "concurrent work 2" in log


# ===========================================================================
# Orchestrator: _mid_chain_rebase integration
# ===========================================================================


class _FakeConfig:
    """Minimal config for testing _mid_chain_rebase."""

    def __init__(self, **auto_task_overrides):
        defaults = dict(
            mid_chain_rebase=True,
            mid_chain_rebase_push=False,
            chain_dependencies=True,
        )
        defaults.update(auto_task_overrides)
        self.auto_task = AutoTaskConfig(**defaults)


class _FakeOrchestrator:
    """Minimal stand-in for testing _mid_chain_rebase in isolation."""

    def __init__(self, git: GitManager, config: _FakeConfig | None = None):
        self.git = git
        self.config = config or _FakeConfig()
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    async def _emit_notify(self, event_type: str, event) -> None:
        self._notifications.append(event_type)

    async def _emit_text_notify(self, message: str, project_id: str | None = None) -> None:
        self._notifications.append(message)

    # Removed: _mid_chain_rebase was inlined into orchestrator's
    # _complete_workspace flow (now uses git.mid_chain_sync directly)
    pass


class TestOrchestratorMidChainRebase:
    """Verify _mid_chain_rebase respects config and calls GitManager."""

    @pytest.fixture
    def git(self):
        return MagicMock(spec=GitManager)

    @pytest.mark.asyncio
    async def test_calls_mid_chain_rebase_when_enabled(self, git):
        """When mid_chain_rebase is enabled, it calls git.mid_chain_rebase."""
        git.mid_chain_rebase.return_value = True
        orch = _FakeOrchestrator(git)
        task = _make_task()
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is True
        git.mid_chain_rebase.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            "main",
            push=False,
        )

    @pytest.mark.asyncio
    async def test_skipped_when_disabled(self, git):
        """When mid_chain_rebase is disabled, no rebase is attempted."""
        config = _FakeConfig(mid_chain_rebase=False)
        orch = _FakeOrchestrator(git, config)
        task = _make_task()
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False
        git.mid_chain_rebase.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_no_chain_dependencies(self, git):
        """Without chain_dependencies, mid-chain rebase is skipped."""
        config = _FakeConfig(chain_dependencies=False)
        orch = _FakeOrchestrator(git, config)
        task = _make_task()
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False
        git.mid_chain_rebase.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_push_config(self, git):
        """mid_chain_rebase_push config is forwarded to git method."""
        git.mid_chain_rebase.return_value = True
        config = _FakeConfig(mid_chain_rebase_push=True)
        orch = _FakeOrchestrator(git, config)
        task = _make_task()
        repo = _make_repo()

        await orch._mid_chain_rebase(task, repo, "/workspace")

        git.mid_chain_rebase.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            "main",
            push=True,
        )

    @pytest.mark.asyncio
    async def test_conflict_returns_false(self, git):
        """When rebase has conflicts, _mid_chain_rebase returns False."""
        git.mid_chain_rebase.return_value = False
        orch = _FakeOrchestrator(git)
        task = _make_task()
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self, git):
        """Unexpected exceptions are caught and return False."""
        git.mid_chain_rebase.side_effect = Exception("unexpected error")
        orch = _FakeOrchestrator(git)
        task = _make_task()
        repo = _make_repo()

        result = await orch._mid_chain_rebase(task, repo, "/workspace")

        assert result is False

    @pytest.mark.asyncio
    async def test_uses_repo_default_branch(self, git):
        """Uses the repo's default_branch, not hardcoded 'main'."""
        git.mid_chain_rebase.return_value = True
        orch = _FakeOrchestrator(git)
        task = _make_task()
        repo = _make_repo(default_branch="develop")

        await orch._mid_chain_rebase(task, repo, "/workspace")

        git.mid_chain_rebase.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            "develop",
            push=False,
        )


# ===========================================================================
# _complete_workspace integration: mid-chain rebase is called for
# intermediate subtasks
# ===========================================================================


class TestCompleteWorkspaceMidChainRebase:
    """Verify _complete_workspace calls _mid_chain_rebase for non-last subtasks."""

    @pytest.mark.asyncio
    async def test_intermediate_subtask_triggers_rebase(self):
        """An intermediate subtask (not last) should trigger mid-chain rebase."""
        from src.orchestrator import Orchestrator

        git = MagicMock(spec=GitManager)
        git.validate_checkout.return_value = True
        git.commit_all.return_value = True
        git.mid_chain_rebase.return_value = True

        db = MagicMock()
        # Simulate: task is a subtask, siblings still pending
        sibling_pending = _make_task(id="subtask-3", status=TaskStatus.DEFINED)
        db.get_subtasks = MagicMock(
            return_value=[
                _make_task(id="subtask-1", status=TaskStatus.COMPLETED),
                _make_task(id="subtask-2", status=TaskStatus.IN_PROGRESS),
                sibling_pending,
            ]
        )
        db.get_agent_workspace = MagicMock(
            return_value=MagicMock(
                workspace_path="/workspace",
                repo_id="repo-1",
            )
        )
        db.get_repo = MagicMock(return_value=_make_repo())

        # Use the _FakeOrchestrator pattern to isolate _complete_workspace
        # We'll test via _mid_chain_rebase being called
        task = _make_task(id="subtask-2")
        agent = MagicMock(id="agent-1")

        # The full orchestrator is complex to mock. Instead, verify the
        # GitManager.mid_chain_rebase is called via the _mid_chain_rebase
        # unit tests above, and verify the _complete_workspace flow here
        # by checking that _mid_chain_rebase would be triggered.
        # (The _complete_workspace code path calls _mid_chain_rebase when
        # not is_last and repo and branch_name are present.)
        assert task.is_plan_subtask is True
        assert task.branch_name is not None


# ===========================================================================
# Config loading
# ===========================================================================


class TestMidChainRebaseConfig:
    """Verify config loading for mid_chain_rebase options."""

    def test_default_values(self):
        """Default config has mid_chain_rebase=True, push=False."""
        config = AutoTaskConfig()
        assert config.mid_chain_rebase is True
        assert config.mid_chain_rebase_push is False

    def test_config_from_dict(self):
        """Config can be loaded from dict values."""
        config = AutoTaskConfig(
            mid_chain_rebase=False,
            mid_chain_rebase_push=True,
        )
        assert config.mid_chain_rebase is False
        assert config.mid_chain_rebase_push is True

    def test_load_config_parses_mid_chain_rebase(self, tmp_path):
        """load_config correctly parses mid_chain_rebase fields from YAML."""
        import yaml
        from src.config import load_config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "auto_task": {
                        "mid_chain_rebase": False,
                        "mid_chain_rebase_push": True,
                    }
                }
            )
        )

        config = load_config(str(config_path))
        assert config.auto_task.mid_chain_rebase is False
        assert config.auto_task.mid_chain_rebase_push is True

    def test_load_config_defaults(self, tmp_path):
        """When not specified in YAML, defaults are used."""
        import yaml
        from src.config import load_config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"auto_task": {"enabled": True}}))

        config = load_config(str(config_path))
        assert config.auto_task.mid_chain_rebase is True
        assert config.auto_task.mid_chain_rebase_push is False


# ===========================================================================
# _prepare_workspace: rebase_between_subtasks wiring
# ===========================================================================


class _FakePrepareOrchestrator:
    """Minimal stand-in for testing _prepare_workspace rebase wiring.

    Borrows _prepare_workspace from the real Orchestrator and stubs out
    the database and notification methods so we can assert on git calls.
    """

    def __init__(self, git: GitManager, config: _FakeConfig | None = None):
        self.git = git
        self.config = config or _FakeConfig()
        self.db = MagicMock()
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    def _get_task_repo_id(self, task):
        return task.repo_id

    def _compute_workspace_path(self, agent, project_id, repo):
        return "/workspace"

    from src.orchestrator import Orchestrator as _Orch

    _prepare_workspace = _Orch._prepare_workspace


class TestPrepareWorkspaceRebaseWiring:
    """Verify _prepare_workspace passes rebase_between_subtasks to switch_to_branch."""

    @pytest.fixture
    def git(self):
        git = MagicMock(spec=GitManager)
        git.validate_checkout.return_value = True
        return git

    def _setup_db(self, orch, parent_task, repo):
        """Wire up async DB stubs so _prepare_workspace can resolve workspace/repo."""
        ws = Workspace(
            id="ws-1",
            project_id="proj-1",
            workspace_path="/workspace",
            source_type=repo.source_type,
        )
        orch.db.get_agent_workspace = AsyncMock(return_value=ws)
        orch.db.get_repo = AsyncMock(return_value=repo)
        orch.db.get_task = AsyncMock(return_value=parent_task)
        orch.db.update_task = AsyncMock()

    @pytest.mark.asyncio
    async def test_subtask_switch_passes_rebase_true(self, git):
        """When rebase_between_subtasks=True, switch_to_branch gets rebase=True."""
        config = _FakeConfig(rebase_between_subtasks=True)
        orch = _FakePrepareOrchestrator(git, config)

        parent = _make_task(
            id="parent-task",
            branch_name="parent-task/feature-branch",
        )
        task = _make_task(
            id="subtask-2",
            parent_task_id="parent-task",
            is_plan_subtask=True,
        )
        repo = _make_repo(source_type=RepoSourceType.CLONE)
        self._setup_db(orch, parent, repo)

        agent = MagicMock(id="agent-1")
        await orch._prepare_workspace(task, agent)

        git.switch_to_branch.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            default_branch="main",
            rebase=True,
        )
        git.prepare_for_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtask_switch_passes_rebase_false_by_default(self, git):
        """Default rebase_between_subtasks=False → switch_to_branch gets rebase=False."""
        config = _FakeConfig(rebase_between_subtasks=False)
        orch = _FakePrepareOrchestrator(git, config)

        parent = _make_task(
            id="parent-task",
            branch_name="parent-task/feature-branch",
        )
        task = _make_task(
            id="subtask-2",
            parent_task_id="parent-task",
            is_plan_subtask=True,
        )
        repo = _make_repo(source_type=RepoSourceType.CLONE)
        self._setup_db(orch, parent, repo)

        agent = MagicMock(id="agent-1")
        await orch._prepare_workspace(task, agent)

        git.switch_to_branch.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            default_branch="main",
            rebase=False,
        )

    @pytest.mark.asyncio
    async def test_non_subtask_uses_prepare_for_task(self, git):
        """A non-subtask should call prepare_for_task, not switch_to_branch."""
        config = _FakeConfig(rebase_between_subtasks=True)
        orch = _FakePrepareOrchestrator(git, config)

        task = _make_task(
            id="root-task",
            is_plan_subtask=False,
            parent_task_id=None,
        )
        repo = _make_repo(source_type=RepoSourceType.CLONE)
        ws = Workspace(
            id="ws-1",
            project_id="proj-1",
            workspace_path="/workspace",
            source_type=repo.source_type,
        )
        orch.db.get_agent_workspace = AsyncMock(return_value=ws)
        orch.db.get_repo = AsyncMock(return_value=repo)
        orch.db.update_task = AsyncMock()

        agent = MagicMock(id="agent-1")
        await orch._prepare_workspace(task, agent)

        git.prepare_for_task.assert_called_once()
        git.switch_to_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_repo_passes_rebase_flag(self, git):
        """LINK repos also pass the rebase flag through to switch_to_branch."""
        config = _FakeConfig(rebase_between_subtasks=True)
        orch = _FakePrepareOrchestrator(git, config)

        parent = _make_task(
            id="parent-task",
            branch_name="parent-task/feature-branch",
        )
        task = _make_task(
            id="subtask-2",
            parent_task_id="parent-task",
            is_plan_subtask=True,
        )
        repo = _make_repo(source_type=RepoSourceType.LINK)
        ws = Workspace(
            id="ws-1",
            project_id="proj-1",
            workspace_path="/workspace",
            source_type=repo.source_type,
        )
        orch.db.get_agent_workspace = AsyncMock(return_value=ws)
        orch.db.get_repo = AsyncMock(return_value=repo)
        orch.db.get_task = AsyncMock(return_value=parent)
        orch.db.update_task = AsyncMock()
        # LINK repos check os.path.isdir
        with patch("os.path.isdir", return_value=True):
            agent = MagicMock(id="agent-1")
            await orch._prepare_workspace(task, agent)

        git.switch_to_branch.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            default_branch="main",
            rebase=True,
        )

    @pytest.mark.asyncio
    async def test_custom_default_branch_passed_through(self, git):
        """Repo's default_branch (not hardcoded 'main') is forwarded."""
        config = _FakeConfig(rebase_between_subtasks=True)
        orch = _FakePrepareOrchestrator(git, config)

        parent = _make_task(
            id="parent-task",
            branch_name="parent-task/feature-branch",
        )
        task = _make_task(
            id="subtask-2",
            parent_task_id="parent-task",
            is_plan_subtask=True,
        )
        repo = _make_repo(default_branch="develop")
        self._setup_db(orch, parent, repo)

        agent = MagicMock(id="agent-1")
        await orch._prepare_workspace(task, agent)

        git.switch_to_branch.assert_called_once_with(
            "/workspace",
            "parent-task/feature-branch",
            default_branch="develop",
            rebase=True,
        )


# ===========================================================================
# Integration: subtask chain with switch_to_branch rebase
# ===========================================================================


class TestSubtaskChainWithSwitchRebase:
    """Integration tests: subtask chain using switch_to_branch(rebase=True)
    between steps to keep up with origin/main.

    Complements TestMidChainRebaseMultiAgent which tests mid_chain_rebase()
    (post-completion rebase). This class tests rebase-on-switch (pre-start
    rebase via switch_to_branch), which is the rebase_between_subtasks path.
    """

    def test_three_step_chain_with_rebase(self, two_agent_clones):
        """Subtask chain with rebase=True picks up upstream changes each step."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]
        branch = "task/chain-rebase"

        # Step 1: first subtask creates and works on the branch
        mgr.switch_to_branch(agent1, branch, rebase=False)
        _git_commit(agent1, "step1.py", "# step 1", "subtask 1 work")

        # Concurrent work pushed to main by another agent
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent1.py", "# c1", "concurrent work 1")
        _git(["push", "origin", "main"], cwd=agent2)

        # Step 2: switch_to_branch with rebase=True (simulates _prepare_workspace)
        mgr.switch_to_branch(agent1, branch, rebase=True)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "concurrent work 1" in log  # upstream picked up
        assert "subtask 1 work" in log

        _git_commit(agent1, "step2.py", "# step 2", "subtask 2 work")

        # More concurrent work
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent2.py", "# c2", "concurrent work 2")
        _git(["push", "origin", "main"], cwd=agent2)

        # Step 3: another switch with rebase
        mgr.switch_to_branch(agent1, branch, rebase=True)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "concurrent work 2" in log
        assert "subtask 2 work" in log
        assert "subtask 1 work" in log

        _git_commit(agent1, "step3.py", "# step 3", "subtask 3 work")

        # Final log should have all commits in a clean linear history
        log = _git(["log", "--oneline"], cwd=agent1)
        for expected in (
            "subtask 1 work",
            "subtask 2 work",
            "subtask 3 work",
            "concurrent work 1",
            "concurrent work 2",
        ):
            assert expected in log

    def test_three_step_chain_without_rebase(self, two_agent_clones):
        """Subtask chain without rebase does NOT pick up upstream changes."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]
        branch = "task/chain-no-rebase"

        # Step 1: first subtask creates and works on the branch
        mgr.switch_to_branch(agent1, branch, rebase=False)
        _git_commit(agent1, "step1.py", "# step 1", "subtask 1 work")

        # Concurrent work pushed to main
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent1.py", "# c1", "concurrent work 1")
        _git(["push", "origin", "main"], cwd=agent2)

        # Step 2: switch without rebase (default)
        mgr.switch_to_branch(agent1, branch, rebase=False)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "concurrent work 1" not in log  # upstream NOT picked up
        assert "subtask 1 work" in log

        _git_commit(agent1, "step2.py", "# step 2", "subtask 2 work")

        # More concurrent work
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "concurrent2.py", "# c2", "concurrent work 2")
        _git(["push", "origin", "main"], cwd=agent2)

        # Step 3: switch without rebase
        mgr.switch_to_branch(agent1, branch, rebase=False)
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "concurrent work 2" not in log
        assert "concurrent work 1" not in log
        # Only subtask commits present
        assert "subtask 1 work" in log
        assert "subtask 2 work" in log

    def test_rebase_conflict_in_chain_continues_safely(self, two_agent_clones):
        """If rebase conflicts mid-chain, the branch remains usable."""
        mgr = GitManager()
        agent1 = two_agent_clones["agent1"]
        agent2 = two_agent_clones["agent2"]
        branch = "task/chain-conflict"

        # Step 1: work on a shared file
        mgr.switch_to_branch(agent1, branch, rebase=False)
        _git_commit(agent1, "README.md", "agent1 version", "subtask 1 edits README")
        sha_after_step1 = _head_sha(agent1)

        # Concurrent conflicting change to same file
        _git(["checkout", "main"], cwd=agent2)
        _git_commit(agent2, "README.md", "agent2 version", "conflict on README")
        _git(["push", "origin", "main"], cwd=agent2)

        # Step 2: switch with rebase — conflict should be handled gracefully
        mgr.switch_to_branch(agent1, branch, rebase=True)

        assert _current_branch(agent1) == branch
        # Branch should still be functional (rebase aborted, HEAD unchanged)
        assert _head_sha(agent1) == sha_after_step1

        # Agent can still do work on the branch
        _git_commit(agent1, "step2.py", "# step 2", "subtask 2 work")
        log = _git(["log", "--oneline"], cwd=agent1)
        assert "subtask 2 work" in log
        assert "subtask 1 edits README" in log
