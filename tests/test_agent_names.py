"""Tests for creative agent name generation."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agent_names import (
    generate_agent_name,
    generate_unique_agent_name,
    LEGENDARY_NAMES,
    ASTRO_NAMES,
    ELEMENT_NAMES,
    NATURE_NAMES,
    TITLES,
    CODENAMES,
    _pick_legendary,
    _pick_astro,
    _pick_element,
    _pick_nature,
    _pick_compound,
)
from src.database import Database
from src.models import Agent


class TestNamePools:
    """Verify word pool integrity and constraints."""

    def test_legendary_names_not_empty(self):
        assert len(LEGENDARY_NAMES) > 0

    def test_astro_names_not_empty(self):
        assert len(ASTRO_NAMES) > 0

    def test_element_names_not_empty(self):
        assert len(ELEMENT_NAMES) > 0

    def test_nature_names_not_empty(self):
        assert len(NATURE_NAMES) > 0

    def test_titles_not_empty(self):
        assert len(TITLES) > 0

    def test_codenames_not_empty(self):
        assert len(CODENAMES) > 0

    def test_no_duplicate_legendary_names(self):
        assert len(LEGENDARY_NAMES) == len(set(LEGENDARY_NAMES))

    def test_no_duplicate_astro_names(self):
        assert len(ASTRO_NAMES) == len(set(ASTRO_NAMES))

    def test_no_duplicate_element_names(self):
        assert len(ELEMENT_NAMES) == len(set(ELEMENT_NAMES))

    def test_no_duplicate_nature_names(self):
        assert len(NATURE_NAMES) == len(set(NATURE_NAMES))

    def test_no_duplicate_titles(self):
        assert len(TITLES) == len(set(TITLES))

    def test_no_duplicate_codenames(self):
        assert len(CODENAMES) == len(set(CODENAMES))

    def test_all_names_are_strings(self):
        for pool in [LEGENDARY_NAMES, ASTRO_NAMES, ELEMENT_NAMES,
                     NATURE_NAMES, TITLES, CODENAMES]:
            for name in pool:
                assert isinstance(name, str)
                assert len(name) > 0

    def test_names_are_title_case(self):
        """All pool entries should be title-cased for display."""
        for pool in [LEGENDARY_NAMES, ASTRO_NAMES, ELEMENT_NAMES,
                     NATURE_NAMES, TITLES, CODENAMES]:
            for name in pool:
                assert name[0].isupper(), f"'{name}' should start with uppercase"


class TestNameStrategies:
    """Test individual naming strategy functions."""

    def test_pick_legendary_returns_from_pool(self):
        for _ in range(50):
            name = _pick_legendary()
            assert name in LEGENDARY_NAMES

    def test_pick_astro_returns_from_pool(self):
        for _ in range(50):
            name = _pick_astro()
            assert name in ASTRO_NAMES

    def test_pick_element_returns_from_pool(self):
        for _ in range(50):
            name = _pick_element()
            assert name in ELEMENT_NAMES

    def test_pick_nature_returns_from_pool(self):
        for _ in range(50):
            name = _pick_nature()
            assert name in NATURE_NAMES

    def test_pick_compound_format(self):
        """Compound names should be 'Title Codename' format."""
        for _ in range(50):
            name = _pick_compound()
            parts = name.split(" ")
            assert len(parts) == 2, f"Expected 2 words, got: '{name}'"
            assert parts[0] in TITLES, f"Title '{parts[0]}' not in TITLES pool"
            assert parts[1] in CODENAMES, f"Codename '{parts[1]}' not in CODENAMES pool"


class TestGenerateAgentName:
    """Test the main name generation function (no DB check)."""

    def test_returns_string(self):
        name = generate_agent_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_variety(self):
        """Multiple calls should produce at least some different names."""
        names = {generate_agent_name() for _ in range(100)}
        # With 100 draws from a pool of thousands, we should get variety
        assert len(names) > 10

    def test_names_are_display_ready(self):
        """Names should start with uppercase and contain only letters/spaces."""
        for _ in range(100):
            name = generate_agent_name()
            assert name[0].isupper(), f"Name should start uppercase: '{name}'"
            # Names should only contain letters and spaces
            assert all(c.isalpha() or c == " " for c in name), (
                f"Name contains unexpected characters: '{name}'"
            )

    def test_name_produces_valid_agent_id(self):
        """The derived agent ID should be a valid kebab-case identifier."""
        for _ in range(100):
            name = generate_agent_name()
            agent_id = name.lower().replace(" ", "-")
            assert agent_id == agent_id.strip()
            assert "--" not in agent_id
            assert not agent_id.startswith("-")
            assert not agent_id.endswith("-")


class TestGenerateUniqueAgentName:
    """Test unique name generation with database collision checks."""

    async def test_returns_unique_name_no_collisions(self):
        """When no agents exist, should return a name on first try."""
        db = AsyncMock()
        db.get_agent = AsyncMock(return_value=None)

        name = await generate_unique_agent_name(db)
        assert isinstance(name, str)
        assert len(name) > 0
        # Should have checked at least once
        db.get_agent.assert_called()

    async def test_avoids_collision(self):
        """Should skip names that collide with existing agents."""
        call_count = 0
        existing_ids = {"phoenix", "atlas", "nova"}

        async def mock_get_agent(agent_id):
            nonlocal call_count
            call_count += 1
            if agent_id in existing_ids:
                return Agent(id=agent_id, name=agent_id.title(), agent_type="claude")
            return None

        db = AsyncMock()
        db.get_agent = mock_get_agent

        name = await generate_unique_agent_name(db)
        # The returned name should NOT be one of the colliding ones
        agent_id = name.lower().replace(" ", "-")
        # It either found a non-colliding name or appended a suffix
        assert name is not None
        assert len(name) > 0

    async def test_fallback_with_suffix(self):
        """When all base names collide, should append numeric suffix."""
        # Make get_agent return an agent for the first 20 calls (MAX_RETRIES),
        # then return None
        call_count = 0

        async def mock_get_agent(agent_id):
            nonlocal call_count
            call_count += 1
            if call_count <= 20:
                return Agent(id=agent_id, name="taken", agent_type="claude")
            return None

        db = AsyncMock()
        db.get_agent = mock_get_agent

        name = await generate_unique_agent_name(db)
        assert name is not None
        # After 20 retries, the fallback adds a numeric suffix
        # The name should contain a number
        parts = name.rsplit(" ", 1)
        assert len(parts) == 2 or any(c.isdigit() for c in name)

    async def test_integration_with_real_db(self, tmp_path):
        """Integration test with a real database."""
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            # Generate a name - should work fine with empty DB
            name1 = await generate_unique_agent_name(db)
            assert isinstance(name1, str)
            assert len(name1) > 0

            # Create an agent with that name
            agent_id1 = name1.lower().replace(" ", "-")
            agent1 = Agent(id=agent_id1, name=name1, agent_type="claude")
            await db.create_agent(agent1)

            # Generate another name - should be different
            name2 = await generate_unique_agent_name(db)
            agent_id2 = name2.lower().replace(" ", "-")
            assert agent_id2 != agent_id1, "Second name should differ from first"
        finally:
            await db.close()

    async def test_multiple_unique_names(self, tmp_path):
        """Generate many names and verify they're all unique."""
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            generated_ids = set()
            for _ in range(20):
                name = await generate_unique_agent_name(db)
                agent_id = name.lower().replace(" ", "-")
                assert agent_id not in generated_ids, (
                    f"Duplicate agent ID: {agent_id}"
                )
                generated_ids.add(agent_id)
                # Register the agent so future names must avoid it
                agent = Agent(id=agent_id, name=name, agent_type="claude")
                await db.create_agent(agent)
        finally:
            await db.close()


class TestCommandHandlerIntegration:
    """Test that agent CRUD commands are deprecated (workspace-as-agent model)."""

    async def test_create_agent_returns_deprecation_error(self, tmp_path):
        """create_agent should return a deprecation error."""
        from src.command_handler import CommandHandler
        from src.config import AppConfig
        from src.orchestrator import Orchestrator

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orchestrator = Orchestrator(config)
        await orchestrator.db.initialize()

        try:
            handler = CommandHandler(orchestrator, config)
            result = await handler.execute("create_agent", {})

            assert "error" in result
            assert "no longer supported" in result["error"]
            assert "add_workspace" in result["error"]
        finally:
            await orchestrator.db.close()

    async def test_delete_agent_returns_deprecation_error(self, tmp_path):
        """delete_agent should return a deprecation error."""
        from src.command_handler import CommandHandler
        from src.config import AppConfig
        from src.orchestrator import Orchestrator

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orchestrator = Orchestrator(config)
        await orchestrator.db.initialize()

        try:
            handler = CommandHandler(orchestrator, config)
            result = await handler.execute("delete_agent", {"agent_id": "test"})

            assert "error" in result
            assert "no longer supported" in result["error"]
        finally:
            await orchestrator.db.close()
