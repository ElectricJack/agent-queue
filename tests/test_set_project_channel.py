"""Tests for the set_project_channel command.

Covers:
- Link a channel to a project
- Project not found -> error
- DB state is updated correctly
- Setting a new channel replaces the previous one
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


class TestSetProjectChannel:
    """Link a channel to a project."""

    async def test_link_channel(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute(
            "set_project_channel",
            {
                "project_id": "p-1",
                "channel_id": "111111111111111111",
            },
        )

        assert "error" not in result
        assert result["project_id"] == "p-1"
        assert result["channel_id"] == "111111111111111111"
        assert result["status"] == "linked"

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "111111111111111111"

    async def test_replace_existing_channel(self, handler, db):
        """Setting a new channel replaces the old one."""
        await db.create_project(Project(id="p-1", name="Alpha"))

        await handler.execute(
            "set_project_channel",
            {
                "project_id": "p-1",
                "channel_id": "111111111111111111",
            },
        )
        result = await handler.execute(
            "set_project_channel",
            {
                "project_id": "p-1",
                "channel_id": "999999999999999999",
            },
        )

        assert "error" not in result
        project = await db.get_project("p-1")
        assert project.discord_channel_id == "999999999999999999"


class TestSetProjectChannelErrors:
    """Error conditions for set_project_channel."""

    async def test_project_not_found(self, handler):
        result = await handler.execute(
            "set_project_channel",
            {
                "project_id": "nonexistent",
                "channel_id": "111111111111111111",
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestEditProjectChannelToolDefinition:
    """Verify channel editing is available via edit_project in TOOLS list.

    The standalone set_project_channel tool was removed; channel linking is
    now done through edit_project's discord_channel_id parameter.
    """

    def test_edit_project_has_channel_field(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if isinstance(t, dict) and t["name"] == "edit_project")
        schema = tool["input_schema"]
        assert "discord_channel_id" in schema["properties"]

    def test_set_project_channel_not_in_core_tools(self):
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        core_names = {t["name"] for t in registry.get_core_tools()}
        assert "set_project_channel" not in core_names

    def test_set_project_channel_description_mentions_deprecated(self):
        from src.tools import _ALL_TOOL_DEFINITIONS

        defn = next(
            (d for d in _ALL_TOOL_DEFINITIONS if d["name"] == "set_project_channel"),
            None,
        )
        assert defn is not None
        assert "deprecated" in defn["description"].lower()
