"""Tests for list_projects command including channel ID fields.

Covers:
- discord_channel_id is included when set
- Neither field is included when unset
- Multiple projects with different channel configurations
"""

import pytest
from src.config import AppConfig, DiscordConfig
from src.commands.handler import CommandHandler
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
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    return CommandHandler(orchestrator, config)


class TestListProjectsChannelFields:
    """Verify channel fields are returned correctly in list_projects."""

    async def test_no_channel_ids_when_unset(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("list_projects", {})

        projects = result["projects"]
        assert len(projects) == 1
        p = projects[0]
        assert "discord_channel_id" not in p

    async def test_channel_included(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute("list_projects", {})

        p = result["projects"][0]
        assert p["discord_channel_id"] == "111111111111111111"

    async def test_mixed_projects(self, handler, db):
        """Multiple projects with different channel configurations."""
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.create_project(Project(id="p-2", name="Beta"))
        await db.create_project(Project(id="p-3", name="Gamma"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")
        await db.update_project("p-2", discord_channel_id="222222222222222222")
        # p-3 has no channel

        result = await handler.execute("list_projects", {})

        projects_by_id = {p["id"]: p for p in result["projects"]}

        assert projects_by_id["p-1"]["discord_channel_id"] == "111111111111111111"
        assert projects_by_id["p-2"]["discord_channel_id"] == "222222222222222222"
        assert "discord_channel_id" not in projects_by_id["p-3"]
