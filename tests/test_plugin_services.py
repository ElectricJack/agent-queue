"""Behaviour tests for internal-plugin service boundaries."""

from unittest.mock import MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, RepoConfig, RepoSourceType, Workspace
from src.plugins.services import WorkspaceServiceImpl


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "plugins.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def service(db, tmp_path):
    git = MagicMock()
    git.slugify.side_effect = lambda value: value.lower().replace(" ", "-")
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "workspace"),
        database_path=str(tmp_path / "plugins.db"),
        data_dir=str(tmp_path / "data"),
    )
    return WorkspaceServiceImpl(db, git, config)


async def test_validate_path_accepts_linked_repo_outside_workspace_dir(db, service, tmp_path):
    external = tmp_path / "external-repo"
    external.mkdir()
    await db.create_project(Project(id="project", name="Project"))
    await db.create_repo(
        RepoConfig("repo", "project", RepoSourceType.LINK, source_path=str(external))
    )

    assert await service.validate_path(str(external / "a.py")) == str(external / "a.py")


async def test_validate_path_accepts_registered_workspace_outside_workspace_dir(
    db, service, tmp_path
):
    external = tmp_path / "external-workspace"
    external.mkdir()
    await db.create_project(Project(id="project", name="Project"))
    await db.create_workspace(Workspace("ws", "project", str(external), RepoSourceType.LINK))

    assert await service.validate_path(str(external / "a.py")) == str(external / "a.py")


async def test_validate_path_rejects_sibling_prefix_directory(service, tmp_path):
    (tmp_path / "workspace-evil").mkdir()

    assert await service.validate_path(str(tmp_path / "workspace-evil" / "secret.txt")) is None


async def test_resolve_workspace_rejects_workspace_from_another_project(db, service, tmp_path):
    path = tmp_path / "other"
    path.mkdir()
    await db.create_project(Project(id="project-b", name="B"))
    await db.create_workspace(Workspace("ws", "project-b", str(path), RepoSourceType.LINK))

    workspace, error = await service.resolve_workspace("project-a", "ws")

    assert workspace is None
    assert "different project" in error["error"]


def test_resolve_note_path_precedence_and_h1_scan(service, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "my-note.md").write_text("# Completely Different\n")
    exact = notes / "Exact Name.md"
    exact.write_text("# Exact\n")
    h1 = notes / "other.md"
    h1.write_text("# Findable By Title\n")

    assert service.resolve_note_path(str(notes), "Exact Name.md") == str(exact)
    assert service.resolve_note_path(str(notes), "Exact Name") == str(exact)
    assert service.resolve_note_path(str(notes), "My Note") == str(notes / "my-note.md")
    assert service.resolve_note_path(str(notes), "Findable By Title") == str(h1)
    assert service.resolve_note_path(str(notes), "nope") is None
