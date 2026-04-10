"""Tests for the set_control_interface command (chat agent tool).

Covers:
- Valid project + valid channel name -> success
- Missing channel name in guild_channels -> error
- Missing project -> error
- Pre-resolved channel ID path (_resolved_channel_id)
- Missing project_id -> error
- Missing channel_name -> error
- No guild context -> error
- Channel name normalisation (leading '#' stripped)
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
    # Replace the orchestrator's DB with the test DB so we share state.
    orchestrator.db = db
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Guild channels fixture (simulates Discord guild text channels)
# ---------------------------------------------------------------------------
GUILD_CHANNELS = [
    {"id": 111111111111111111, "name": "general"},
    {"id": 222222222222222222, "name": "my-project-control"},
    {"id": 333333333333333333, "name": "other-channel"},
]


class TestSetControlInterfaceValidProject:
    """Test with a valid project and a channel that exists in the guild."""

    async def test_valid_project_and_channel(self, handler, db):
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "channel_name": "my-project-control",
                "guild_channels": GUILD_CHANNELS,
            },
        )

        assert "error" not in result
        assert result["project_id"] == "p-1"
        assert result["channel_id"] == "222222222222222222"
        assert result["status"] == "linked"

        # Verify the DB was actually updated.
        project = await db.get_project("p-1")
        assert project.discord_channel_id == "222222222222222222"

    async def test_channel_name_with_hash_prefix(self, handler, db):
        """Leading '#' in channel_name should be stripped automatically."""
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "channel_name": "#my-project-control",
                "guild_channels": GUILD_CHANNELS,
            },
        )

        assert "error" not in result
        assert result["channel_id"] == "222222222222222222"

    async def test_pre_resolved_channel_id(self, handler, db):
        """When _resolved_channel_id is supplied (Discord slash cmd), skip lookup."""
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "channel_name": "some-channel",
                "_resolved_channel_id": "999999999999999999",
            },
        )

        assert "error" not in result
        assert result["channel_id"] == "999999999999999999"


class TestSetControlInterfaceMissingChannel:
    """Test with a channel name that doesn't exist in the guild."""

    async def test_channel_not_found(self, handler, db):
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "channel_name": "nonexistent-channel",
                "guild_channels": GUILD_CHANNELS,
            },
        )

        assert "error" in result
        assert "nonexistent-channel" in result["error"]

    async def test_no_guild_context(self, handler, db):
        """Without guild_channels or _resolved_channel_id, we can't resolve."""
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "channel_name": "my-project-control",
            },
        )

        assert "error" in result
        assert "guild context" in result["error"].lower() or "channel_id" in result["error"]


class TestSetControlInterfaceMissingProject:
    """Test with a project that doesn't exist."""

    async def test_project_not_found(self, handler, db):
        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "nonexistent-project",
                "channel_name": "my-project-control",
                "_resolved_channel_id": "222222222222222222",
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_missing_project_id(self, handler):
        result = await handler.execute(
            "set_control_interface",
            {
                "channel_name": "my-project-control",
                "guild_channels": GUILD_CHANNELS,
            },
        )

        assert "error" in result
        assert "project_id" in result["error"].lower()

    async def test_missing_channel_name(self, handler, db):
        await db.create_project(Project(id="p-1", name="alpha"))

        result = await handler.execute(
            "set_control_interface",
            {
                "project_id": "p-1",
                "guild_channels": GUILD_CHANNELS,
            },
        )

        assert "error" in result
        assert "channel_name" in result["error"].lower()


class TestSetControlInterfaceToolDefinition:
    """Verify set_control_interface is not a core supervisor tool.

    Channel linking is now done through edit_project's discord_channel_id
    parameter. The backend command handler method still exists for internal
    use (e.g. create_channel_for_project delegates to it) and is available
    via MCP, but the supervisor LLM should not see it as a core tool.
    """

    def test_tool_not_in_core_tools(self):
        from src.tool_registry import ToolRegistry

        registry = ToolRegistry()
        core_names = {t["name"] for t in registry.get_core_tools()}
        assert "set_control_interface" not in core_names

    def test_tool_description_mentions_deprecated(self):
        from src.tool_registry import _ALL_TOOL_DEFINITIONS

        defn = next(
            (d for d in _ALL_TOOL_DEFINITIONS if d["name"] == "set_control_interface"),
            None,
        )
        assert defn is not None, "set_control_interface should have an explicit tool definition"
        assert "deprecated" in defn["description"].lower()
