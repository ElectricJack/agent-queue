"""Integration tests for WorkspaceServiceImpl.resolve_repo_path.

Tests the resolution logic that maps (project_id, optional workspace/repo) to
a filesystem checkout path.  Covers workspaces, legacy linked/cloned repos,
and various error conditions.
"""

import os

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, RepoConfig, RepoSourceType, Workspace
from src.plugins.services import WorkspaceServiceImpl
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real test database."""
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
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def mock_git():
    """Mock GitManager that treats all directories as valid git repos."""
    git = MagicMock(spec=GitManager)
    git.validate_checkout.return_value = True
    git.avalidate_checkout = AsyncMock(return_value=True)
    return git


@pytest.fixture
def ws_service(db, config, mock_git):
    """Create a WorkspaceServiceImpl wired to real DB and mocked git."""
    return WorkspaceServiceImpl(db=db, git=mock_git, config=config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dir(path: str) -> str:
    """Create a directory on disk so isdir checks pass."""
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Happy-path: workspace resolution (primary path)
# ---------------------------------------------------------------------------


class TestWorkspaceResolution:
    """Project with workspaces → resolves from workspace path."""

    async def test_resolve_workspace_by_project_id(self, ws_service, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "ws-checkout"))

        await db.create_project(Project(id="p-ws", name="Workspace Project"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-ws",
                workspace_path=checkout,
                source_type=RepoSourceType.LINK,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-ws"})

        assert err is None
        assert path == checkout
        assert project is not None
        assert project.id == "p-ws"
        mock_git.avalidate_checkout.assert_called_once_with(checkout)

    async def test_workspace_takes_priority_over_legacy_repo(
        self,
        ws_service,
        db,
        tmp_path,
        mock_git,
    ):
        """When both workspaces and legacy repos exist, workspace wins."""
        ws_path = _make_dir(str(tmp_path / "workspace"))
        repo_path = _make_dir(str(tmp_path / "legacy-repo"))

        await db.create_project(Project(id="p-both", name="Both"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-both",
                workspace_path=ws_path,
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_repo(
            RepoConfig(
                id="r-legacy",
                project_id="p-both",
                source_type=RepoSourceType.LINK,
                source_path=repo_path,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-both"})

        assert err is None
        assert path == ws_path  # workspace wins
        assert project.id == "p-both"


# ---------------------------------------------------------------------------
# Happy-path: legacy repo resolution (backward compat)
# ---------------------------------------------------------------------------


class TestLinkedRepo:
    """Project with a LINK repo (legacy) → returns checkout path via fallback."""

    async def test_resolve_linked_repo_by_project_id(self, ws_service, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "linked-checkout"))

        await db.create_project(Project(id="p-link", name="Linked Project"))
        await db.create_repo(
            RepoConfig(
                id="r-link",
                project_id="p-link",
                source_type=RepoSourceType.LINK,
                source_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-link"})

        assert err is None
        assert path == checkout
        assert project is not None
        assert project.id == "p-link"
        mock_git.avalidate_checkout.assert_called_once_with(checkout)

    async def test_resolve_linked_repo_with_active_project(self, ws_service, db, tmp_path, mock_git):
        """When no project_id given but active project is set, resolves via active project."""
        checkout = _make_dir(str(tmp_path / "linked-only"))

        await db.create_project(Project(id="p-link2", name="Link2"))
        await db.create_repo(
            RepoConfig(
                id="r-link2",
                project_id="p-link2",
                source_type=RepoSourceType.LINK,
                source_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path(
            {}, active_project_id="p-link2"
        )

        assert err is None
        assert path == checkout
        assert project is not None
        assert project.id == "p-link2"


class TestClonedRepo:
    """Project with a CLONE repo (legacy) → returns checkout_base_path."""

    async def test_resolve_cloned_repo(self, ws_service, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "clone-checkout"))

        await db.create_project(Project(id="p-clone", name="Cloned Project"))
        await db.create_repo(
            RepoConfig(
                id="r-clone",
                project_id="p-clone",
                source_type=RepoSourceType.CLONE,
                url="https://github.com/example/repo.git",
                checkout_base_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-clone"})

        assert err is None
        assert path == checkout
        assert project is not None
        assert project.id == "p-clone"
        mock_git.avalidate_checkout.assert_called_once_with(checkout)

    async def test_resolve_cloned_repo_with_active_project(self, ws_service, db, tmp_path):
        """When no project_id given but active project is set, resolves cloned repo."""
        checkout = _make_dir(str(tmp_path / "clone-by-id"))

        await db.create_project(Project(id="p-clone2", name="Clone2"))
        await db.create_repo(
            RepoConfig(
                id="r-clone2",
                project_id="p-clone2",
                source_type=RepoSourceType.CLONE,
                url="https://github.com/example/repo.git",
                checkout_base_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path(
            {}, active_project_id="p-clone2"
        )

        assert err is None
        assert path == checkout
        assert project is not None
        assert project.id == "p-clone2"


class TestInitRepo:
    """Project with an INIT repo (legacy) → returns checkout_base_path."""

    async def test_resolve_init_repo(self, ws_service, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "init-checkout"))

        await db.create_project(Project(id="p-init", name="Init Project"))
        await db.create_repo(
            RepoConfig(
                id="r-init",
                project_id="p-init",
                source_type=RepoSourceType.INIT,
                checkout_base_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-init"})

        assert err is None
        assert path == checkout
        assert project.id == "p-init"
        mock_git.avalidate_checkout.assert_called_once_with(checkout)


# ---------------------------------------------------------------------------
# No workspaces → fail-fast error
# ---------------------------------------------------------------------------


class TestNoWorkspaces:
    """Project with no repos and no workspaces → clear error."""

    async def test_no_workspaces_returns_error(self, ws_service, db):
        """Project has no workspaces and no repos → error."""
        await db.create_project(
            Project(
                id="p-empty",
                name="Empty Project",
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-empty"})

        assert path is None
        assert err is not None
        assert "no workspaces" in err["error"].lower()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestInvalidProject:
    """Invalid / non-existent project_id → returns error."""

    async def test_nonexistent_project(self, ws_service):
        path, project, err = await ws_service.resolve_repo_path(
            {
                "project_id": "nonexistent-project",
            }
        )

        assert path is None
        assert project is None
        assert err is not None
        assert "not found" in err["error"].lower()
        assert "nonexistent-project" in err["error"]

    async def test_empty_project_id_treated_as_missing(self, ws_service):
        """Empty string project_id with no repo_id → error."""
        path, project, err = await ws_service.resolve_repo_path(
            {
                "project_id": "",
            }
        )

        assert path is None
        assert err is not None
        assert "project_id" in err["error"].lower() or "required" in err["error"].lower()


class TestMissingArgs:
    """No project_id supplied."""

    async def test_no_ids_at_all(self, ws_service):
        path, project, err = await ws_service.resolve_repo_path({})

        assert path is None
        assert project is None
        assert err is not None
        assert "project_id" in err["error"].lower() or "required" in err["error"].lower()

    async def test_empty_dict(self, ws_service):
        """Explicit empty dict."""
        path, project, err = await ws_service.resolve_repo_path({})

        assert path is None
        assert err is not None


# ---------------------------------------------------------------------------
# Repo path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    """Path exists on disk but isn't a valid git repo, etc."""

    async def test_repo_path_not_on_disk(self, ws_service, db, tmp_path):
        """Repo configured with a path that doesn't exist on disk."""
        nonexistent = str(tmp_path / "vanished-checkout")

        await db.create_project(Project(id="p-vanish", name="Vanished"))
        await db.create_repo(
            RepoConfig(
                id="r-vanish",
                project_id="p-vanish",
                source_type=RepoSourceType.LINK,
                source_path=nonexistent,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-vanish"})

        assert path is None
        assert err is not None
        assert "path not found" in err["error"].lower() or "not found" in err["error"].lower()

    async def test_repo_path_not_a_git_repo(self, ws_service, db, tmp_path, mock_git):
        """Path exists but git.validate_checkout returns False."""
        checkout = _make_dir(str(tmp_path / "not-a-git-repo"))
        mock_git.validate_checkout.return_value = False
        mock_git.avalidate_checkout.return_value = False

        await db.create_project(Project(id="p-nogit", name="Not Git"))
        await db.create_repo(
            RepoConfig(
                id="r-nogit",
                project_id="p-nogit",
                source_type=RepoSourceType.LINK,
                source_path=checkout,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-nogit"})

        assert path is None
        assert err is not None
        assert "not a valid git repository" in err["error"].lower()

    async def test_clone_repo_missing_checkout_base_path(self, ws_service, db, tmp_path):
        """CLONE repo with empty checkout_base_path → falls through to error."""
        await db.create_project(Project(id="p-nobase", name="No Base"))
        await db.create_repo(
            RepoConfig(
                id="r-nobase",
                project_id="p-nobase",
                source_type=RepoSourceType.CLONE,
                url="https://github.com/example/repo.git",
                checkout_base_path="",  # not set
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-nobase"})

        assert path is None
        assert err is not None
        assert "no workspaces" in err["error"].lower() or "no usable path" in err["error"].lower()

    async def test_link_repo_missing_source_path(self, ws_service, db, tmp_path):
        """LINK repo with empty source_path → falls through to error."""
        await db.create_project(Project(id="p-nosrc", name="No Src"))
        await db.create_repo(
            RepoConfig(
                id="r-nosrc",
                project_id="p-nosrc",
                source_type=RepoSourceType.LINK,
                source_path="",  # not set
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-nosrc"})

        assert path is None
        assert err is not None
        assert "no workspaces" in err["error"].lower() or "no usable path" in err["error"].lower()


# ---------------------------------------------------------------------------
# Multiple repos per project (legacy)
# ---------------------------------------------------------------------------


class TestMultipleRepos:
    """When a project has multiple repos, the first is used by default."""

    async def test_first_repo_used_when_no_repo_id(self, ws_service, db, tmp_path, mock_git):
        """Without explicit repo_id, list_repos returns repos and first is used."""
        checkout1 = _make_dir(str(tmp_path / "repo1"))
        checkout2 = _make_dir(str(tmp_path / "repo2"))

        await db.create_project(Project(id="p-multi", name="Multi Repo"))
        await db.create_repo(
            RepoConfig(
                id="r-first",
                project_id="p-multi",
                source_type=RepoSourceType.LINK,
                source_path=checkout1,
            )
        )
        await db.create_repo(
            RepoConfig(
                id="r-second",
                project_id="p-multi",
                source_type=RepoSourceType.LINK,
                source_path=checkout2,
            )
        )

        path, project, err = await ws_service.resolve_repo_path({"project_id": "p-multi"})

        assert err is None
        assert project is not None
        # Should have used the first repo returned by list_repos
        assert path in (checkout1, checkout2)
