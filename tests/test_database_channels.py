"""Tests for database-level channel field operations.

Covers:
- Project creation with channel field (default None)
- Updating discord_channel_id
- Clearing channel field (set to None)
- Channel field survives project update of other fields
"""

import pytest
from src.database import Database
from src.models import Project
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    yield database
    await database.close()


class TestProjectChannelDefaults:
    """Channel field defaults to None on creation."""

    async def test_new_project_has_no_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        project = await db.get_project("p-1")
        assert project.discord_channel_id is None


class TestUpdateDiscordChannelId:
    """Set and update the channel."""

    async def test_set_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        await db.update_project("p-1", discord_channel_id="111111111111111111")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "111111111111111111"

    async def test_replace_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        await db.update_project("p-1", discord_channel_id="999999999999999999")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "999999999999999999"

    async def test_clear_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        await db.update_project("p-1", discord_channel_id=None)

        project = await db.get_project("p-1")
        assert project.discord_channel_id is None

    async def test_other_project_fields_preserve_channel(self, db):
        """Updating name/weight shouldn't affect the channel field."""
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        await db.update_project("p-1", name="Alpha Updated", credit_weight=5.0)

        project = await db.get_project("p-1")
        assert project.name == "Alpha Updated"
        assert project.credit_weight == 5.0
        assert project.discord_channel_id == "111111111111111111"
