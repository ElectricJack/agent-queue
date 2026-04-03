"""Tests for delete_project command with channel cleanup.

Covers:
- Deleting a project with a channel ID returns it in the result
- Deleting a project without a channel returns no channel_ids
- _on_project_deleted callback is invoked
- archive_channels flag is passed through
"""

import pytest
from src.config import AppConfig, DiscordConfig
from src.command_handler import CommandHandler
from src.database import Database
from src.models import Project
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
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
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    return CommandHandler(orchestrator, config)


class TestDeleteProjectReturnsChannelIds:
    """Verify channel IDs are captured and returned before deletion."""

    async def test_returns_channel_id(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute("delete_project", {"project_id": "p-1"})

        assert "error" not in result
        assert result["deleted"] == "p-1"
        assert "channel_ids" in result
        assert result["channel_ids"]["channel"] == "111111111111111111"

    async def test_no_channel_ids_when_none_set(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("delete_project", {"project_id": "p-1"})

        assert "error" not in result
        assert "channel_ids" not in result


class TestDeleteProjectCallback:
    """Verify _on_project_deleted callback is invoked."""

    async def test_callback_invoked_with_project_id(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        deleted_ids = []
        handler._on_project_deleted = lambda pid: deleted_ids.append(pid)

        await handler.execute("delete_project", {"project_id": "p-1"})

        assert deleted_ids == ["p-1"]

    async def test_callback_not_invoked_when_none(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        handler._on_project_deleted = None

        # Should not raise even without callback
        result = await handler.execute("delete_project", {"project_id": "p-1"})
        assert "error" not in result


class TestDeleteProjectArchiveFlag:
    """Verify archive_channels flag is passed through in the result."""

    async def test_archive_flag_in_result(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute(
            "delete_project",
            {
                "project_id": "p-1",
                "archive_channels": True,
            },
        )

        assert "error" not in result
        assert result.get("archive_channels") is True

    async def test_no_archive_flag_when_not_requested(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("delete_project", {"project_id": "p-1"})

        assert "error" not in result
        assert "archive_channels" not in result


class TestDeleteProjectActuallyDeletes:
    """Verify the project is removed from the database."""

    async def test_project_removed_from_db(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("delete_project", {"project_id": "p-1"})

        assert "error" not in result
        project = await db.get_project("p-1")
        assert project is None
