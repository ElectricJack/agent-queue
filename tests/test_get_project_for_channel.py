"""Tests for the get_project_for_channel command (reverse lookup).

Covers:
- Finds project via its channel
- Returns nulls when no project matches
- Missing channel_id -> error
- Channel ID is normalised to string
- First match wins when multiple projects exist
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


class TestReverseLookup:
    """Find a project by its channel."""

    async def test_finds_project_by_channel(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": "111111111111111111",
            },
        )

        assert "error" not in result
        assert result["project_id"] == "p-1"
        assert result["project_name"] == "Alpha"
        assert result["channel_id"] == "111111111111111111"


class TestReverseLookupNoMatch:
    """No project matches the channel."""

    async def test_returns_nulls_for_unknown_channel(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": "999999999999999999",
            },
        )

        assert "error" not in result
        assert result["project_id"] is None
        assert result["project_name"] is None
        assert result["channel_id"] == "999999999999999999"

    async def test_returns_nulls_when_no_projects_exist(self, handler):
        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": "111111111111111111",
            },
        )

        assert "error" not in result
        assert result["project_id"] is None


class TestReverseLookupMultipleProjects:
    """Multiple projects with different channel assignments."""

    async def test_finds_correct_project_among_many(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.create_project(Project(id="p-2", name="Beta"))
        await db.create_project(Project(id="p-3", name="Gamma"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")
        await db.update_project("p-2", discord_channel_id="222222222222222222")
        await db.update_project("p-3", discord_channel_id="333333333333333333")

        # Find p-2 by its channel
        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": "222222222222222222",
            },
        )
        assert result["project_id"] == "p-2"

        # Find p-3 by its channel
        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": "333333333333333333",
            },
        )
        assert result["project_id"] == "p-3"


class TestReverseLookupErrors:
    """Error conditions for get_project_for_channel."""

    async def test_missing_channel_id(self, handler):
        result = await handler.execute("get_project_for_channel", {})

        assert "error" in result
        assert "channel_id" in result["error"].lower()

    async def test_channel_id_normalised_to_string(self, handler, db):
        """Integer channel IDs should be normalised to strings for matching."""
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute(
            "get_project_for_channel",
            {
                "channel_id": 111111111111111111,  # int, not str
            },
        )

        assert "error" not in result
        assert result["project_id"] == "p-1"


class TestReverseLookupToolDefinition:
    """Verify the tool is properly defined in the TOOLS list."""

    def test_tool_exists_in_tools_list(self):
        from src.chat_agent import TOOLS

        tool_names = [t["name"] for t in TOOLS]
        assert "get_project_for_channel" in tool_names

    def test_tool_schema(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "get_project_for_channel")
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "channel_id" in schema["properties"]
        assert "channel_id" in schema["required"]
