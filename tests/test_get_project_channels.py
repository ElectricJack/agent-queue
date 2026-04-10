"""Tests for the get_project_channels command.

Covers:
- Returns channel_id for a project with it set
- Returns None for unset channel
- Project not found -> error
- Returns correct structure with all expected keys
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
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    return CommandHandler(orchestrator, config)


class TestGetProjectChannelsSet:
    """When the channel is set."""

    async def test_returns_channel_id(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        result = await handler.execute("get_project_channels", {"project_id": "p-1"})

        assert "error" not in result
        assert result["project_id"] == "p-1"
        assert result["channel_id"] == "111111111111111111"


class TestGetProjectChannelsUnset:
    """When no channel is set."""

    async def test_no_channel_set(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("get_project_channels", {"project_id": "p-1"})

        assert "error" not in result
        assert result["channel_id"] is None


class TestGetProjectChannelsErrors:
    """Error conditions for get_project_channels."""

    async def test_project_not_found(self, handler):
        result = await handler.execute(
            "get_project_channels",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestGetProjectChannelsToolDefinition:
    """Verify the tool is properly defined in the TOOLS list."""

    def test_tool_exists_in_tools_list(self):
        from src.chat_agent import TOOLS

        tool_names = [t["name"] for t in TOOLS]
        assert "get_project_channels" in tool_names

    def test_tool_schema(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "get_project_channels")
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "project_id" in schema["properties"]
        assert "project_id" in schema["required"]
