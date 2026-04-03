"""Unit tests for git-related command handler methods.

Tests each new command handler (_cmd_create_branch, _cmd_checkout_branch,
_cmd_commit_changes, _cmd_push_branch, _cmd_merge_branch, _cmd_git_log,
_cmd_git_diff) with mocked GitManager and Database to test command handler
logic in isolation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError, GitManager
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory database for tests."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def mock_git():
    """Create a mock GitManager with sensible defaults."""
    git = MagicMock(spec=GitManager)
    # Sync defaults (kept for backward compat / sync-path tests)
    git.validate_checkout.return_value = True
    git.get_current_branch.return_value = "main"
    git.get_recent_commits.return_value = "abc1234 Initial commit"
    git.get_diff.return_value = "diff --git a/file.py b/file.py"
    git._run.return_value = ""
    git.create_branch.return_value = None
    git.checkout_branch.return_value = None
    git.commit_all.return_value = True
    git.push_branch.return_value = None
    git.merge_branch.return_value = True
    # Async defaults — command handler now uses the async API
    git.avalidate_checkout = AsyncMock(return_value=True)
    git.aget_current_branch = AsyncMock(return_value="main")
    git.aget_recent_commits = AsyncMock(return_value="abc1234 Initial commit")
    git.aget_diff = AsyncMock(return_value="diff --git a/file.py b/file.py")
    git._arun = AsyncMock(return_value="")
    git.acreate_branch = AsyncMock(return_value=None)
    git.acheckout_branch = AsyncMock(return_value=None)
    git.acommit_all = AsyncMock(return_value=True)
    git.apush_branch = AsyncMock(return_value=None)
    git.amerge_branch = AsyncMock(return_value=True)
    git.apull_branch = AsyncMock(return_value="main")
    git.aget_changed_files = AsyncMock(return_value=[])
    git.alist_branches = AsyncMock(return_value=["* main"])
    git.acreate_pr = AsyncMock(return_value="https://github.com/test/pr/1")
    git.acheck_gh_auth = AsyncMock(return_value=True)
    git.acreate_github_repo = AsyncMock(return_value="https://github.com/user/repo")
    git.aget_status = AsyncMock(return_value="")
    git.aget_default_branch = AsyncMock(return_value="main")
    return git


@pytest.fixture
async def handler(db, config, mock_git):
    """Create a CommandHandler with a mocked GitManager."""
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = mock_git
    return CommandHandler(orchestrator, config)


@pytest.fixture
async def project_with_repo(db, tmp_path):
    """Create a test project and linked repo in the database.

    Returns (project_id, repo_id, checkout_path).
    """
    project_id = "test-proj"
    repo_id = "test-repo"
    checkout_path = str(tmp_path / "workspaces" / "test-proj")

    # Create the directory so _resolve_repo_path doesn't fail the isdir check
    import os

    os.makedirs(checkout_path, exist_ok=True)

    await db.create_project(
        Project(
            id=project_id,
            name="Test Project",
        )
    )
    await db.create_repo(
        RepoConfig(
            id=repo_id,
            project_id=project_id,
            source_type=RepoSourceType.LINK,
            source_path=checkout_path,
            default_branch="main",
        )
    )
    return project_id, repo_id, checkout_path


# ---------------------------------------------------------------------------
# test_create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:
    """Tests for _cmd_create_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/new-thing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/new-thing"
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/new-thing",
        )

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, repo_id, _ = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "create_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acreate_branch.side_effect = GitError("branch already exists")

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/dup",
            },
        )

        assert "error" in result
        assert "branch already exists" in result["error"]


# ---------------------------------------------------------------------------
# test_checkout_branch
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    """Tests for _cmd_checkout_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/existing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/existing"
        assert result["status"] == "checked_out"
        mock_git.acheckout_branch.assert_called_once_with(
            checkout_path,
            "feature/existing",
        )

    async def test_branch_not_found(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acheckout_branch.side_effect = GitError(
            "error: pathspec 'no-such-branch' did not match any file(s) known to git"
        )

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "no-such-branch",
            },
        )

        assert "error" in result
        assert "no-such-branch" in result["error"]

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "main",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        """When tasks are IN_PROGRESS, result should include a warning."""
        project_id, _, _ = project_with_repo

        # Create an IN_PROGRESS task for this project
        from src.models import Task

        task = Task(
            id="task-1",
            project_id=project_id,
            title="Running task",
            description="A task that is running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/switch",
            },
        )

        assert "error" not in result
        assert result["status"] == "checked_out"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_commit_changes
# ---------------------------------------------------------------------------


class TestCommitChanges:
    """Tests for _cmd_commit_changes."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "feat: add new feature",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["commit_message"] == "feat: add new feature"
        assert result["status"] == "committed"
        mock_git.acommit_all.assert_called_once_with(
            checkout_path,
            "feat: add new feature",
        )

    async def test_nothing_to_commit(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.return_value = False  # nothing to commit

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "empty commit",
            },
        )

        assert "error" not in result
        assert result["status"] == "nothing_to_commit"
        assert "No changes" in result["message"]

    async def test_missing_message(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "message is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "commit_changes",
            {
                "project_id": "nonexistent",
                "message": "some message",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.side_effect = GitError("failed to commit")

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "a commit",
            },
        )

        assert "error" in result
        assert "failed to commit" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.return_value = True

        from src.models import Task

        task = Task(
            id="task-running",
            project_id=project_id,
            title="Active task",
            description="Running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "fix: something",
            },
        )

        assert "error" not in result
        assert result["status"] == "committed"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_push_branch
# ---------------------------------------------------------------------------


class TestPushBranch:
    """Tests for _cmd_push_branch."""

    async def test_success_with_explicit_branch(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/push-me",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/push-me"
        assert result["status"] == "pushed"
        mock_git.apush_branch.assert_called_once_with(
            checkout_path,
            "feature/push-me",
        )

    async def test_success_with_current_branch(self, handler, mock_git, project_with_repo):
        """When branch_name is not provided, uses current branch."""
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto-detect"

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["branch"] == "feature/auto-detect"
        assert result["status"] == "pushed"
        mock_git.aget_current_branch.assert_called_with(checkout_path)
        mock_git.apush_branch.assert_called_once_with(
            checkout_path,
            "feature/auto-detect",
        )

    async def test_push_failure(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.apush_branch.side_effect = GitError("git push origin feature/x failed: rejected")

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "rejected" in result["error"]

    async def test_cannot_determine_current_branch(self, handler, mock_git, project_with_repo):
        """When no branch_name given and current branch can't be determined."""
        project_id, _, _ = project_with_repo
        mock_git.aget_current_branch.return_value = ""

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
            },
        )

        assert "error" in result
        assert "Could not determine current branch" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "push_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "main",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_merge_branch
# ---------------------------------------------------------------------------


class TestMergeBranch:
    """Tests for _cmd_merge_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/done",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/done"
        assert result["target"] == "main"
        assert result["status"] == "merged"
        mock_git.amerge_branch.assert_called_once_with(
            checkout_path,
            "feature/done",
            "main",
        )

    async def test_conflict_scenario(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = False  # conflict

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/conflicting",
            },
        )

        assert "error" not in result  # conflict is not an error, just a status
        assert result["status"] == "conflict"
        assert "conflict" in result["message"].lower()
        assert result["branch"] == "feature/conflicting"
        assert result["target"] == "main"

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "merge_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.amerge_branch.side_effect = GitError("fatal: not a git repo")

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/bad",
            },
        )

        assert "error" in result
        assert "not a git repo" in result["error"]

    async def test_uses_repo_default_branch(self, handler, db, mock_git, tmp_path):
        """Merge should use the project's configured default branch, not hardcoded 'main'."""
        import os
        from src.models import Workspace

        project_id = "proj-develop"
        checkout_path = str(tmp_path / "workspaces" / "proj-develop")
        os.makedirs(checkout_path, exist_ok=True)

        await db.create_project(
            Project(
                id=project_id,
                name="Develop Project",
                repo_default_branch="develop",
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-develop",
                project_id=project_id,
                workspace_path=checkout_path,
                source_type=RepoSourceType.LINK,
            )
        )

        mock_git.amerge_branch.return_value = True

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/custom-default",
            },
        )

        assert "error" not in result
        assert result["target"] == "develop"
        mock_git.amerge_branch.assert_called_once_with(
            checkout_path,
            "feature/custom-default",
            "develop",
        )

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.amerge_branch.return_value = True

        from src.models import Task

        await db.create_task(
            Task(
                id="task-active",
                project_id=project_id,
                title="Active",
                description="Running",
                status=TaskStatus.IN_PROGRESS,
            )
        )

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/merge-warn",
            },
        )

        assert "error" not in result
        assert result["status"] == "merged"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_git_log
# ---------------------------------------------------------------------------


class TestGitLog:
    """Tests for _cmd_git_log."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = (
            "abc1234 feat: add feature\ndef5678 fix: bug fix"
        )
        mock_git.aget_current_branch.return_value = "feature/test"

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/test"
        assert "abc1234" in result["log"]
        assert "def5678" in result["log"]
        # Default count is 10
        mock_git.aget_recent_commits.assert_called_once_with(
            checkout_path,
            count=10,
        )

    async def test_custom_count(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = "abc1234 commit"

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
                "count": 3,
            },
        )

        assert "error" not in result
        mock_git.aget_recent_commits.assert_called_once_with(
            checkout_path,
            count=3,
        )

    async def test_empty_log(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.aget_recent_commits.return_value = ""

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["log"] == "(no commits)"

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "git_log",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_git_diff
# ---------------------------------------------------------------------------


class TestGitDiff:
    """Tests for _cmd_git_diff."""

    async def test_working_tree_diff(self, handler, mock_git, project_with_repo):
        """Without base_branch, shows working tree diff (unstaged changes)."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git._arun.return_value = "diff --git a/file.py b/file.py\n+new line"

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["base_branch"] == "(working tree)"
        assert "new line" in result["diff"]
        mock_git._arun.assert_called_once_with(["diff"], cwd=checkout_path)

    async def test_diff_against_base_branch(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_diff.return_value = "diff --git a/src/app.py b/src/app.py\n+import new_module"

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
                "base_branch": "main",
            },
        )

        assert "error" not in result
        assert result["base_branch"] == "main"
        assert "new_module" in result["diff"]
        mock_git.aget_diff.assert_called_once_with(checkout_path, "main")

    async def test_no_changes(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git._arun.return_value = ""

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["diff"] == "(no changes)"

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git._arun.side_effect = GitError("fatal: bad revision")

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" in result
        assert "bad revision" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "git_diff",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_diff_against_base_branch_git_error(self, handler, mock_git, project_with_repo):
        """GitError when diffing against a base branch."""
        project_id, _, _ = project_with_repo
        mock_git.aget_diff.side_effect = GitError("fatal: bad object 'develop'")

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
                "base_branch": "develop",
            },
        )

        assert "error" in result
        assert "bad object" in result["error"]


# ---------------------------------------------------------------------------
# test_resolve_repo_path edge cases
# ---------------------------------------------------------------------------


class TestResolveRepoPath:
    """Edge cases for _resolve_repo_path used by all commands."""

    async def test_missing_project_id_no_active(self, handler):
        """Commands without project_id and no active project should error."""
        result = await handler.execute(
            "create_branch",
            {
                "branch_name": "feature/orphan",
            },
        )

        assert "error" in result
        assert "project_id" in result["error"].lower() or "required" in result["error"].lower()

    async def test_with_workspace_param(self, handler, db, mock_git, project_with_repo, tmp_path):
        """Commands should work when project_id is explicitly specified."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/via-repo-id",
            },
        )

        assert "error" not in result
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/via-repo-id",
        )


# ---------------------------------------------------------------------------
# test_active_project_fallback
# ---------------------------------------------------------------------------


class TestActiveProjectFallback:
    """Tests for active project inference in git commands."""

    async def test_create_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """create_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "create_branch",
            {
                "branch_name": "feature/auto-project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/auto-project"
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/auto-project",
        )

    async def test_commit_changes_infers_active_project(self, handler, mock_git, project_with_repo):
        """commit_changes should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "commit_changes",
            {
                "message": "feat: auto-inferred project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "committed"
        mock_git.acommit_all.assert_called_once_with(
            checkout_path,
            "feat: auto-inferred project",
        )

    async def test_push_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """push_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto"
        handler.set_active_project(project_id)

        result = await handler.execute("push_branch", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "pushed"
        mock_git.apush_branch.assert_called_once_with(
            checkout_path,
            "feature/auto",
        )

    async def test_checkout_branch_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """checkout_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "checkout_branch",
            {
                "branch_name": "feature/existing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "checked_out"

    async def test_merge_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """merge_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "merge_branch",
            {
                "branch_name": "feature/auto-merge",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "merged"

    async def test_git_log_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_log should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = "abc1234 test commit"
        handler.set_active_project(project_id)

        result = await handler.execute("git_log", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert "abc1234" in result["log"]

    async def test_git_diff_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_diff should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git._arun.return_value = "diff output"
        handler.set_active_project(project_id)

        result = await handler.execute("git_diff", {})

        assert "error" not in result
        assert result["project_id"] == project_id

    async def test_git_commit_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_commit (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_commit",
            {
                "message": "feat: inferred commit",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["project_id"] == project_id
        mock_git.acommit_all.assert_called_once_with(
            checkout_path,
            "feat: inferred commit",
        )

    async def test_git_pull_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_pull should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.return_value = "main"
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {})

        assert "error" not in result
        assert result["pulled"] == "main"
        assert result["project_id"] == project_id
        mock_git.apull_branch.assert_called_once_with(checkout_path, None)

    async def test_git_pull_with_branch(self, handler, mock_git, project_with_repo):
        """git_pull with explicit branch should pass it through."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.return_value = "feature/xyz"
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {"branch": "feature/xyz"})

        assert "error" not in result
        assert result["pulled"] == "feature/xyz"
        mock_git.apull_branch.assert_called_once_with(checkout_path, "feature/xyz")

    async def test_git_pull_error(self, handler, mock_git, project_with_repo):
        """git_pull should return error when pull fails."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.side_effect = GitError("merge conflict")
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {})

        assert "error" in result
        assert "merge conflict" in result["error"]

    async def test_git_push_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_push (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto"
        handler.set_active_project(project_id)

        result = await handler.execute("git_push", {})

        assert "error" not in result
        assert result["pushed"] == "feature/auto"
        assert result["project_id"] == project_id

    async def test_git_create_branch_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """git_create_branch (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_create_branch",
            {
                "branch_name": "feature/auto-branch",
            },
        )

        assert "error" not in result
        assert result["created_branch"] == "feature/auto-branch"
        assert result["project_id"] == project_id

    async def test_git_changed_files_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """git_changed_files (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_changed_files.return_value = ["file1.py", "file2.py"]
        handler.set_active_project(project_id)

        result = await handler.execute("git_changed_files", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["count"] == 2

    async def test_no_fallback_without_active_project(self, handler):
        """Without active project, commands should still fail gracefully."""
        handler.set_active_project(None)

        result = await handler.execute(
            "git_commit",
            {
                "message": "should fail",
            },
        )

        assert "error" in result
        assert (
            "project_id" in result["error"].lower() or "active project" in result["error"].lower()
        )

    async def test_get_git_status_infers_active_project(self, handler, mock_git, project_with_repo):
        """get_git_status should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_status.return_value = "nothing to commit"
        mock_git.aget_current_branch.return_value = "main"
        mock_git.aget_recent_commits.return_value = "abc1234 commit"
        handler.set_active_project(project_id)

        result = await handler.execute("get_git_status", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert len(result["repos"]) > 0

    async def test_get_git_status_no_fallback_without_active(self, handler):
        """get_git_status without project_id and no active project should error."""
        handler.set_active_project(None)

        result = await handler.execute("get_git_status", {})

        assert "error" in result
        assert (
            "project_id" in result["error"].lower() or "active project" in result["error"].lower()
        )

    async def test_git_merge_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_merge (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_merge",
            {
                "branch_name": "feature/auto-merge",
            },
        )

        assert "error" not in result
        assert result["merged"] is True
        assert result["project_id"] == project_id

    async def test_git_create_pr_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_create_pr should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/pr-test"
        mock_git.acreate_pr.return_value = "https://github.com/test/repo/pull/1"
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_create_pr",
            {
                "title": "Test PR",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert "pr_url" in result

    async def test_explicit_project_id_overrides_active(
        self, handler, db, mock_git, project_with_repo, tmp_path
    ):
        """Explicit project_id should take precedence over active project."""
        project_id, repo_id, checkout_path = project_with_repo

        # Set active project to something else
        handler.set_active_project("some-other-project")

        # But explicitly pass the real project_id
        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "explicit project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "committed"


# ---------------------------------------------------------------------------
# test_create_github_repo
# ---------------------------------------------------------------------------


class TestCreateGithubRepo:
    """Tests for _cmd_create_github_repo."""

    async def test_success(self, handler, mock_git):
        mock_git.acheck_gh_auth.return_value = True
        mock_git.acreate_github_repo.return_value = "https://github.com/user/my-app"

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" not in result
        assert result["created"] is True
        assert result["repo_url"] == "https://github.com/user/my-app"
        assert result["name"] == "my-app"
        mock_git.acreate_github_repo.assert_called_once_with(
            "my-app",
            private=True,
            org=None,
            description="",
        )

    async def test_success_with_options(self, handler, mock_git):
        mock_git.acheck_gh_auth.return_value = True
        mock_git.acreate_github_repo.return_value = "https://github.com/my-org/my-app"

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
                "private": False,
                "org": "my-org",
                "description": "A cool app",
            },
        )

        assert "error" not in result
        assert result["created"] is True
        assert result["repo_url"] == "https://github.com/my-org/my-app"
        mock_git.acreate_github_repo.assert_called_once_with(
            "my-app",
            private=False,
            org="my-org",
            description="A cool app",
        )

    async def test_missing_name(self, handler, mock_git):
        result = await handler.execute("create_github_repo", {})

        assert result == {"error": "name is required"}

    async def test_gh_not_authenticated(self, handler, mock_git):
        mock_git.acheck_gh_auth.return_value = False

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" in result
        assert "not authenticated" in result["error"].lower()
        mock_git.acreate_github_repo.assert_not_called()

    async def test_git_error(self, handler, mock_git):
        mock_git.acheck_gh_auth.return_value = True
        mock_git.acreate_github_repo.side_effect = GitError(
            "gh repo create failed: Name already exists"
        )

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" in result
        assert "Name already exists" in result["error"]


# ---------------------------------------------------------------------------
# test_generate_readme
# ---------------------------------------------------------------------------


class TestGenerateReadme:
    """Tests for _cmd_generate_readme."""

    async def test_success_full(self, handler, mock_git, project_with_repo, tmp_path):
        """README generated with description and tech stack, committed and pushed."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "My Awesome App",
                "description": "A web application for managing tasks.",
                "tech_stack": "Python, FastAPI, PostgreSQL",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["pushed"] is True
        assert result["status"] == "generated"

        import os

        readme_path = os.path.join(checkout_path, "README.md")
        assert os.path.isfile(readme_path)
        with open(readme_path) as f:
            content = f.read()
        assert "# My Awesome App" in content
        assert "A web application for managing tasks." in content
        assert "- Python" in content
        assert "- FastAPI" in content
        assert "- PostgreSQL" in content

        mock_git.acommit_all.assert_called_once_with(
            checkout_path,
            "Add generated README.md",
        )
        mock_git.apush_branch.assert_called_once()

    async def test_success_minimal(self, handler, mock_git, project_with_repo):
        """README generated with only name, no description or tech stack."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Minimal Project",
            },
        )

        assert "error" not in result
        assert result["committed"] is True

        import os

        readme_path = os.path.join(checkout_path, "README.md")
        with open(readme_path) as f:
            content = f.read()
        assert "# Minimal Project" in content
        assert "## Tech Stack" not in content

    async def test_missing_name(self, handler, project_with_repo):
        """Error returned when name is missing."""
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "name is required"}

    async def test_invalid_project(self, handler):
        """Error returned for nonexistent project."""
        result = await handler.execute(
            "generate_readme",
            {
                "project_id": "nonexistent",
                "name": "Test",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_commit_failure(self, handler, mock_git, project_with_repo):
        """Error returned when git commit fails."""
        project_id, _, checkout_path = project_with_repo
        mock_git.acommit_all.side_effect = GitError("commit failed")

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" in result
        assert "commit failed" in result["error"]

    async def test_push_failure_non_fatal(self, handler, mock_git, project_with_repo):
        """Push failure is non-fatal — commit succeeds but pushed is False."""
        project_id, _, checkout_path = project_with_repo
        mock_git.apush_branch.side_effect = GitError("push failed")

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["pushed"] is False

    async def test_nothing_to_commit(self, handler, mock_git, project_with_repo):
        """When commit_all returns False, result reflects nothing committed."""
        project_id, _, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = False

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" not in result
        assert result["committed"] is False
        assert result["pushed"] is False
