"""Tests for create_project with auto-channel creation integration.

Covers:
- _cmd_create_project() returns auto_create_channels flag
- Explicit auto_create_channels=True overrides config (config disabled)
- Explicit auto_create_channels=False overrides config (config enabled)
- Default behaviour falls back to per_project_channels.auto_create config
- Project is created correctly regardless of auto-channel flag
- LLM tool definition includes auto_create_channels parameter
"""

import pytest
from src.config import AppConfig, DiscordConfig, PerProjectChannelsConfig
from src.commands.handler import CommandHandler
from src.database import Database
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config_auto_create_off(tmp_path):
    """Config with per_project_channels.auto_create = False (default)."""
    return AppConfig(
        discord=DiscordConfig(
            bot_token="test-token",
            guild_id="123",
            per_project_channels=PerProjectChannelsConfig(auto_create=False),
        ),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def config_auto_create_on(tmp_path):
    """Config with per_project_channels.auto_create = True."""
    return AppConfig(
        discord=DiscordConfig(
            bot_token="test-token",
            guild_id="123",
            per_project_channels=PerProjectChannelsConfig(auto_create=True),
        ),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler_auto_off(db, config_auto_create_off):
    orchestrator = Orchestrator(config_auto_create_off)
    orchestrator.db = db
    return CommandHandler(orchestrator, config_auto_create_off)


@pytest.fixture
async def handler_auto_on(db, config_auto_create_on):
    orchestrator = Orchestrator(config_auto_create_on)
    orchestrator.db = db
    return CommandHandler(orchestrator, config_auto_create_on)


# ---------------------------------------------------------------------------
# Basic project creation (unchanged behaviour)
# ---------------------------------------------------------------------------


class TestBasicProjectCreation:
    """Ensure project creation still works correctly with the new field."""

    async def test_creates_project_in_db(self, handler_auto_off, db):
        result = await handler_auto_off.execute("create_project", {"name": "My App"})

        assert "error" not in result
        assert result["created"] == "my-app"
        assert result["name"] == "My App"

        project = await db.get_project("my-app")
        assert project is not None
        assert project.name == "My App"

    async def test_project_id_normalisation(self, handler_auto_off):
        result = await handler_auto_off.execute("create_project", {"name": "Hello World App"})
        assert result["created"] == "hello-world-app"

    async def test_custom_credit_weight(self, handler_auto_off, db):
        result = await handler_auto_off.execute(
            "create_project", {"name": "Weighted", "credit_weight": 2.5}
        )
        assert "error" not in result
        project = await db.get_project("weighted")
        assert project.credit_weight == 2.5

    async def test_custom_max_concurrent_agents(self, handler_auto_off, db):
        result = await handler_auto_off.execute(
            "create_project", {"name": "Concurrent", "max_concurrent_agents": 5}
        )
        assert "error" not in result
        project = await db.get_project("concurrent")
        assert project.max_concurrent_agents == 5


# ---------------------------------------------------------------------------
# auto_create_channels flag in result
# ---------------------------------------------------------------------------


class TestAutoCreateChannelsFlag:
    """Verify auto_create_channels is returned and respects config/overrides."""

    async def test_default_false_when_config_off(self, handler_auto_off):
        """When config auto_create is False and no explicit arg, result is False."""
        result = await handler_auto_off.execute("create_project", {"name": "NoChannels"})
        assert "error" not in result
        assert result["auto_create_channels"] is False

    async def test_default_true_when_config_on(self, handler_auto_on):
        """When config auto_create is True and no explicit arg, result is True."""
        result = await handler_auto_on.execute("create_project", {"name": "WithChannels"})
        assert "error" not in result
        assert result["auto_create_channels"] is True

    async def test_explicit_true_overrides_config_off(self, handler_auto_off):
        """Explicit auto_create_channels=True overrides config=False."""
        result = await handler_auto_off.execute(
            "create_project",
            {"name": "ForceChannels", "auto_create_channels": True},
        )
        assert "error" not in result
        assert result["auto_create_channels"] is True

    async def test_explicit_false_overrides_config_on(self, handler_auto_on):
        """Explicit auto_create_channels=False overrides config=True."""
        result = await handler_auto_on.execute(
            "create_project",
            {"name": "SkipChannels", "auto_create_channels": False},
        )
        assert "error" not in result
        assert result["auto_create_channels"] is False

    async def test_explicit_none_falls_back_to_config(self, handler_auto_on):
        """Passing auto_create_channels=None is treated as 'not supplied'."""
        result = await handler_auto_on.execute(
            "create_project",
            {"name": "DefaultBehaviour", "auto_create_channels": None},
        )
        assert "error" not in result
        # None is falsy but we check `is not None` so it falls through to config
        assert result["auto_create_channels"] is True


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------


class TestResultStructure:
    """Verify the result dict has all expected fields."""

    async def test_result_contains_all_fields(self, handler_auto_off):
        result = await handler_auto_off.execute("create_project", {"name": "Complete"})

        assert "created" in result
        assert "name" in result
        assert "auto_create_channels" in result

    async def test_result_types(self, handler_auto_off):
        result = await handler_auto_off.execute("create_project", {"name": "Types"})

        assert isinstance(result["created"], str)
        assert isinstance(result["name"], str)
        assert isinstance(result["auto_create_channels"], bool)


# ---------------------------------------------------------------------------
# LLM tool definition
# ---------------------------------------------------------------------------


class TestToolDefinition:
    """Verify the create_project tool includes auto_create_channels."""

    def test_tool_has_auto_create_channels_param(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "create_project")
        props = tool["input_schema"]["properties"]
        assert "auto_create_channels" in props
        assert props["auto_create_channels"]["type"] == "boolean"

    def test_auto_create_channels_not_required(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "create_project")
        required = tool["input_schema"].get("required", [])
        assert "auto_create_channels" not in required

    def test_tool_description_mentions_channels(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "create_project")
        assert "channel" in tool["description"].lower()
