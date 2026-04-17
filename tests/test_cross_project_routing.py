"""Tests for cross-project channel routing safeguards.

Covers:
- Cross-project warning when creating a task via implicit project_id
- edit_task with project_id to move tasks between projects
- Discord bot channel context includes other projects hint
"""

import pytest
from unittest.mock import MagicMock

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    # Create two projects
    await database.create_project(
        Project(id="agent-queue", name="Agent Queue", discord_channel_id="111")
    )
    await database.create_project(
        Project(id="mech-fighters", name="Mech Fighters", discord_channel_id="222")
    )
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
    from src.orchestrator import Orchestrator

    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Cross-project warning on create_task
# ---------------------------------------------------------------------------


class TestCrossProjectWarning:
    """When a task is created via implicit project_id (from channel context)
    and the title/description mentions another known project, a warning
    should be included in the response."""

    @pytest.mark.asyncio
    async def test_warning_when_title_mentions_other_project(self, handler):
        """Task title mentions 'mech-fighters' but created in agent-queue channel."""
        handler.set_active_project("agent-queue")
        result = await handler.execute(
            "create_task",
            {"title": "Fix mech-fighters 2D brush raycasting"},
        )
        assert "created" in result
        assert result["project_id"] == "agent-queue"
        assert "warning" in result
        assert "mech-fighters" in result["warning"]

    @pytest.mark.asyncio
    async def test_warning_when_description_mentions_other_project(self, handler):
        """Task description mentions 'mech-fighters' but created in agent-queue."""
        handler.set_active_project("agent-queue")
        result = await handler.execute(
            "create_task",
            {
                "title": "Fix raycasting bug",
                "description": "The mech-fighters project has a raycasting issue",
            },
        )
        assert "created" in result
        assert "warning" in result
        assert "mech-fighters" in result["warning"]

    @pytest.mark.asyncio
    async def test_no_warning_when_project_id_explicit(self, handler):
        """No warning when project_id is explicitly provided (user chose deliberately)."""
        handler.set_active_project("agent-queue")
        result = await handler.execute(
            "create_task",
            {
                "title": "Fix mech-fighters raycasting",
                "project_id": "agent-queue",
            },
        )
        assert "created" in result
        assert "warning" not in result

    @pytest.mark.asyncio
    async def test_no_warning_when_no_cross_project_mention(self, handler):
        """No warning when task content doesn't mention another project."""
        handler.set_active_project("agent-queue")
        result = await handler.execute(
            "create_task",
            {"title": "Fix orchestrator scheduling bug"},
        )
        assert "created" in result
        assert "warning" not in result

    @pytest.mark.asyncio
    async def test_no_warning_when_active_project_matches_mention(self, handler):
        """No warning when mention matches the active project."""
        handler.set_active_project("mech-fighters")
        result = await handler.execute(
            "create_task",
            {"title": "Fix mech-fighters raycasting"},
        )
        assert "created" in result
        assert "warning" not in result


# ---------------------------------------------------------------------------
# edit_task with project_id
# ---------------------------------------------------------------------------


class TestEditTaskProjectId:
    """edit_task should support changing a task's project_id."""

    @pytest.mark.asyncio
    async def test_move_task_to_different_project(self, handler, db):
        """Should update project_id when a valid project is specified."""
        task = Task(
            id="test-1",
            project_id="agent-queue",
            title="Fix raycasting",
            description="Wrong project",
        )
        await db.create_task(task)

        result = await handler.execute(
            "edit_task",
            {"task_id": "test-1", "project_id": "mech-fighters"},
        )
        assert "updated" in result
        assert "project_id" in result["fields"]

        # Verify the task was actually moved
        updated = await db.get_task("test-1")
        assert updated.project_id == "mech-fighters"

    @pytest.mark.asyncio
    async def test_move_task_invalid_project(self, handler, db):
        """Should return error when target project doesn't exist."""
        task = Task(
            id="test-2",
            project_id="agent-queue",
            title="Some task",
            description="desc",
        )
        await db.create_task(task)

        result = await handler.execute(
            "edit_task",
            {"task_id": "test-2", "project_id": "nonexistent"},
        )
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Bot channel context hint
# ---------------------------------------------------------------------------


class TestChannelContextHint:
    """The Discord bot should include other project names in the channel context
    so the LLM knows when to route to a different project."""

    def test_project_channels_hint_format(self):
        """Verify the hint includes other projects when available."""
        # Simulate what the bot does with _project_channels
        project_channels = {
            "agent-queue": "channel-obj-1",
            "mech-fighters": "channel-obj-2",
            "web-app": "channel-obj-3",
        }
        current_project = "agent-queue"

        other_projects = [pid for pid in project_channels if pid != current_project]
        assert "mech-fighters" in other_projects
        assert "web-app" in other_projects
        assert "agent-queue" not in other_projects

        names = ", ".join(f"`{p}`" for p in sorted(other_projects))
        hint = (
            f" Other known projects: {names}. "
            f"If the user's request is clearly about a "
            f"different project, set project_id explicitly."
        )
        assert "`mech-fighters`" in hint
        assert "`web-app`" in hint

    def test_no_hint_when_single_project(self):
        """No hint when there's only one project channel."""
        project_channels = {"agent-queue": "channel-obj-1"}
        current_project = "agent-queue"

        other_projects = [pid for pid in project_channels if pid != current_project]
        assert other_projects == []
