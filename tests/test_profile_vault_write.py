"""Tests for profile vault-write flow (Roadmap 4.1.9).

Chat commands (create, edit, delete, import) write to vault markdown files
instead of directly to the database.  The vault watcher syncs changes to DB.
For immediate feedback, commands trigger an explicit sync after writing.

Covers:
- agent_profile_to_markdown: rendering AgentProfile fields to markdown
- _split_system_prompt_suffix: round-tripping combined prompt back to sections
- Install section: parsing and round-trip through markdown
- Description in frontmatter: round-trip through markdown
- create_profile: writes vault file + syncs to DB
- edit_profile: reads vault, applies updates, writes back, syncs
- delete_profile: removes vault file + DB row
- import_profile: writes vault file from YAML import
- Vault file existence checks for duplicate detection
- Edit with vault file vs DB-only profile (migration path)
"""

from __future__ import annotations

import os

import pytest

from src.config import AppConfig
from src.models import AgentProfile
from src.orchestrator import Orchestrator
from src.profiles.parser import (
    _split_system_prompt_suffix,
    agent_profile_to_markdown,
    parse_profile,
    parsed_profile_to_agent_profile,
)


# ---------------------------------------------------------------------------
# agent_profile_to_markdown tests
# ---------------------------------------------------------------------------


class TestAgentProfileToMarkdown:
    """Test rendering AgentProfile fields to markdown."""

    def test_minimal_profile(self):
        md = agent_profile_to_markdown(id="test", name="Test Agent")
        parsed = parse_profile(md)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "test"
        assert parsed.frontmatter.name == "Test Agent"

    def test_full_profile(self):
        md = agent_profile_to_markdown(
            id="coding",
            name="Coding Agent",
            description="A general-purpose coding agent",
            model="claude-sonnet-4-6",
            permission_mode="auto",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            mcp_servers={
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                }
            },
            role="You are a software engineering agent.",
            rules="- Always run tests\n- Never commit secrets",
            reflection="After completing a task, consider what you learned.",
            install={"npm": ["eslint"], "commands": ["docker"]},
            tags=["profile", "agent-type"],
        )
        parsed = parse_profile(md)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "coding"
        assert parsed.frontmatter.name == "Coding Agent"
        assert parsed.frontmatter.extra.get("description") == "A general-purpose coding agent"
        assert parsed.config["model"] == "claude-sonnet-4-6"
        assert parsed.config["permission_mode"] == "auto"
        assert parsed.tools["allowed"] == ["Read", "Write", "Edit", "Bash"]
        assert "github" in parsed.mcp_servers
        assert parsed.role == "You are a software engineering agent."
        assert "Always run tests" in parsed.rules
        assert "consider what you learned" in parsed.reflection
        assert parsed.install == {"npm": ["eslint"], "commands": ["docker"]}
        assert parsed.frontmatter.tags == ["profile", "agent-type"]

    def test_system_prompt_suffix_fallback(self):
        """When only system_prompt_suffix is provided, it's split into sections."""
        suffix = "## Role\nYou are a reviewer.\n\n## Rules\n- Check for bugs\n\n## Reflection\nThink about it."
        md = agent_profile_to_markdown(
            id="reviewer",
            name="Reviewer",
            system_prompt_suffix=suffix,
        )
        parsed = parse_profile(md)
        assert parsed.is_valid
        assert parsed.role == "You are a reviewer."
        assert "Check for bugs" in parsed.rules
        assert "Think about it" in parsed.reflection

    def test_individual_sections_take_priority(self):
        """When individual sections AND system_prompt_suffix are provided,
        the individual sections win."""
        md = agent_profile_to_markdown(
            id="test",
            name="Test",
            system_prompt_suffix="This should be ignored",
            role="Individual role",
            rules="Individual rules",
        )
        parsed = parse_profile(md)
        assert parsed.role == "Individual role"
        assert parsed.rules == "Individual rules"

    def test_empty_fields_omitted(self):
        """Empty fields should not produce empty sections."""
        md = agent_profile_to_markdown(
            id="minimal",
            name="Minimal",
            model="",
            allowed_tools=[],
            mcp_servers={},
            install={},
        )
        assert "## Config" not in md
        assert "## Tools" not in md
        assert "## MCP Servers" not in md
        assert "## Install" not in md

    def test_description_in_frontmatter(self):
        md = agent_profile_to_markdown(
            id="test",
            name="Test",
            description="A test profile",
        )
        assert "description: A test profile" in md
        parsed = parse_profile(md)
        assert parsed.frontmatter.extra.get("description") == "A test profile"


# ---------------------------------------------------------------------------
# _split_system_prompt_suffix tests
# ---------------------------------------------------------------------------


class TestSplitSystemPromptSuffix:
    """Test reversing the combined system_prompt_suffix back to sections."""

    def test_empty_string(self):
        assert _split_system_prompt_suffix("") == ("", "", "")

    def test_no_markers(self):
        """Without ## markers, entire text is treated as role."""
        role, rules, reflection = _split_system_prompt_suffix("Just some text")
        assert role == "Just some text"
        assert rules == ""
        assert reflection == ""

    def test_role_only(self):
        role, rules, reflection = _split_system_prompt_suffix("## Role\nYou are a coder.")
        assert role == "You are a coder."
        assert rules == ""
        assert reflection == ""

    def test_all_three_sections(self):
        suffix = (
            "## Role\nYou are a coder.\n\n## Rules\n- Be careful\n\n## Reflection\nThink deeply."
        )
        role, rules, reflection = _split_system_prompt_suffix(suffix)
        assert role == "You are a coder."
        assert rules == "- Be careful"
        assert reflection == "Think deeply."

    def test_rules_and_reflection_only(self):
        suffix = "## Rules\n- Rule 1\n\n## Reflection\nReflect."
        role, rules, reflection = _split_system_prompt_suffix(suffix)
        assert role == ""
        assert rules == "- Rule 1"
        assert reflection == "Reflect."

    def test_round_trip(self):
        """parsed_profile_to_agent_profile → _split_system_prompt_suffix round-trip."""
        md = """\
---
id: test
name: Test
---

## Role
You are a test agent.

## Rules
- Follow the rules

## Reflection
After the task, reflect.
"""
        parsed = parse_profile(md)
        profile_dict = parsed_profile_to_agent_profile(parsed)
        suffix = profile_dict["system_prompt_suffix"]

        role, rules, reflection = _split_system_prompt_suffix(suffix)
        assert role == "You are a test agent."
        assert "Follow the rules" in rules
        assert "After the task, reflect." in reflection


# ---------------------------------------------------------------------------
# Install section parsing tests
# ---------------------------------------------------------------------------


class TestInstallSectionParsing:
    """Test the ## Install section in profile markdown."""

    def test_install_section_parsed(self):
        md = """\
---
id: test
name: Test
---

## Install
```json
{
  "npm": ["eslint"],
  "pip": ["black"],
  "commands": ["docker"]
}
```
"""
        parsed = parse_profile(md)
        assert parsed.is_valid
        assert parsed.install == {
            "npm": ["eslint"],
            "pip": ["black"],
            "commands": ["docker"],
        }

    def test_install_section_invalid_json(self):
        md = """\
---
id: test
name: Test
---

## Install
```json
{invalid json}
```
"""
        parsed = parse_profile(md)
        assert not parsed.is_valid
        assert any("Install" in e or "JSON" in e for e in parsed.errors)

    def test_install_must_be_object(self):
        md = """\
---
id: test
name: Test
---

## Install
```json
["not", "an", "object"]
```
"""
        parsed = parse_profile(md)
        assert not parsed.is_valid
        assert any("Install" in e for e in parsed.errors)

    def test_install_round_trip(self):
        """Install dict survives markdown render → parse → convert cycle."""
        install = {"npm": ["@anthropic/mcp-playwright"], "commands": ["node"]}
        md = agent_profile_to_markdown(
            id="test",
            name="Test",
            install=install,
        )
        parsed = parse_profile(md)
        assert parsed.is_valid
        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert profile_dict["install"] == install


# ---------------------------------------------------------------------------
# Description round-trip tests
# ---------------------------------------------------------------------------


class TestDescriptionRoundTrip:
    """Test that description in frontmatter round-trips correctly."""

    def test_description_in_parsed_profile_dict(self):
        md = """\
---
id: test
name: Test
description: A comprehensive test profile
---

## Role
Test role.
"""
        parsed = parse_profile(md)
        assert parsed.is_valid
        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert profile_dict["description"] == "A comprehensive test profile"

    def test_description_round_trip_through_markdown(self):
        md = agent_profile_to_markdown(
            id="test",
            name="Test",
            description="My description",
        )
        parsed = parse_profile(md)
        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert profile_dict.get("description") == "My description"


# ---------------------------------------------------------------------------
# Full round-trip: markdown → parse → dict → markdown → parse
# ---------------------------------------------------------------------------


class TestFullRoundTrip:
    """Verify that a profile survives the full markdown → parse → render → parse cycle."""

    def test_round_trip_preserves_all_fields(self):
        original = agent_profile_to_markdown(
            id="coding",
            name="Coding Agent",
            description="General coder",
            model="claude-sonnet-4-6",
            permission_mode="auto",
            allowed_tools=["Read", "Write", "Edit"],
            mcp_servers={"gh": {"command": "npx", "args": ["gh-mcp"]}},
            role="You are a coder.",
            rules="- Write tests",
            reflection="Think about what you learned.",
            install={"pip": ["black"]},
        )

        # Parse the rendered markdown
        parsed1 = parse_profile(original)
        assert parsed1.is_valid

        # Convert to dict and re-render
        d = parsed_profile_to_agent_profile(parsed1)
        rendered = agent_profile_to_markdown(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            model=d.get("model", ""),
            permission_mode=d.get("permission_mode", ""),
            allowed_tools=d.get("allowed_tools", []),
            mcp_servers=d.get("mcp_servers", {}),
            system_prompt_suffix=d.get("system_prompt_suffix", ""),
            install=d.get("install", {}),
        )

        # Parse again
        parsed2 = parse_profile(rendered)
        assert parsed2.is_valid
        assert parsed2.frontmatter.id == "coding"
        assert parsed2.frontmatter.name == "Coding Agent"
        assert parsed2.config["model"] == "claude-sonnet-4-6"
        assert parsed2.tools["allowed"] == ["Read", "Write", "Edit"]
        assert "gh" in parsed2.mcp_servers
        assert "coder" in parsed2.role
        assert "Write tests" in parsed2.rules
        assert "what you learned" in parsed2.reflection
        assert parsed2.install == {"pip": ["black"]}


# ---------------------------------------------------------------------------
# Command handler vault-write integration tests
# ---------------------------------------------------------------------------


class TestCommandVaultWrite:
    """Test that profile commands write to vault and sync to DB."""

    @pytest.fixture
    async def handler(self, tmp_path):
        from src.command_handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler
        await orch.db.close()

    async def test_create_writes_vault_file(self, handler):
        """create_profile should write a markdown file to the vault."""
        result = await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer", "model": "claude-sonnet-4-6"},
        )
        assert result.get("created") == "reviewer"

        vault_path = handler._vault_profile_path("reviewer")
        assert os.path.isfile(vault_path)

        # Verify the file content is valid markdown
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "reviewer"
        assert parsed.config["model"] == "claude-sonnet-4-6"

    async def test_create_syncs_to_db(self, handler):
        """create_profile should sync the vault file to DB immediately."""
        await handler.execute(
            "create_profile",
            {
                "id": "coder",
                "name": "Coder",
                "allowed_tools": ["Read", "Write"],
            },
        )
        profile = await handler.db.get_profile("coder")
        assert profile is not None
        assert profile.name == "Coder"
        assert profile.allowed_tools == ["Read", "Write"]

    async def test_edit_updates_vault_file(self, handler):
        """edit_profile should update the vault markdown file."""
        await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer"},
        )
        vault_path = handler._vault_profile_path("reviewer")

        await handler.execute(
            "edit_profile",
            {
                "profile_id": "reviewer",
                "name": "Senior Reviewer",
                "model": "claude-sonnet-4-6",
            },
        )

        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.frontmatter.name == "Senior Reviewer"
        assert parsed.config["model"] == "claude-sonnet-4-6"

    async def test_edit_syncs_to_db(self, handler):
        """edit_profile should sync updated file to DB."""
        await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer"},
        )
        await handler.execute(
            "edit_profile",
            {"profile_id": "reviewer", "allowed_tools": ["Read", "Grep"]},
        )
        profile = await handler.db.get_profile("reviewer")
        assert profile.allowed_tools == ["Read", "Grep"]

    async def test_edit_preserves_existing_fields(self, handler):
        """edit_profile should preserve fields not being updated."""
        await handler.execute(
            "create_profile",
            {
                "id": "coder",
                "name": "Coder",
                "model": "claude-sonnet-4-6",
                "allowed_tools": ["Read", "Write"],
            },
        )
        await handler.execute(
            "edit_profile",
            {"profile_id": "coder", "description": "Updated description"},
        )

        profile = await handler.db.get_profile("coder")
        assert profile.model == "claude-sonnet-4-6"
        assert profile.allowed_tools == ["Read", "Write"]
        assert profile.description == "Updated description"

    async def test_delete_removes_vault_file(self, handler):
        """delete_profile should remove the vault markdown file."""
        await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer"},
        )
        vault_path = handler._vault_profile_path("reviewer")
        assert os.path.isfile(vault_path)

        await handler.execute("delete_profile", {"profile_id": "reviewer"})
        assert not os.path.isfile(vault_path)

    async def test_delete_removes_db_row(self, handler):
        """delete_profile should also remove the DB row."""
        await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer"},
        )
        await handler.execute("delete_profile", {"profile_id": "reviewer"})
        profile = await handler.db.get_profile("reviewer")
        assert profile is None

    async def test_duplicate_detection_via_vault(self, handler):
        """Creating a profile with an existing vault file should fail."""
        await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Reviewer"},
        )
        result = await handler.execute(
            "create_profile",
            {"id": "reviewer", "name": "Another Reviewer"},
        )
        assert "error" in result
        assert "already exists" in result["error"]

    async def test_import_writes_vault_file(self, handler):
        """import_profile should write a vault markdown file."""
        yaml_text = """
agent_profile:
  id: imported
  name: "Imported Profile"
  allowed_tools: [Read, Write]
  model: "claude-sonnet-4-6"
"""
        result = await handler.execute("import_profile", {"source": yaml_text})
        assert result.get("imported") is True

        vault_path = handler._vault_profile_path("imported")
        assert os.path.isfile(vault_path)

        # Verify the file is valid and has the right content
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "imported"
        assert parsed.config["model"] == "claude-sonnet-4-6"

    async def test_import_syncs_to_db(self, handler):
        """import_profile should sync the vault file to DB."""
        yaml_text = """
agent_profile:
  id: imported
  name: "Imported Profile"
  allowed_tools: [Read, Write]
"""
        await handler.execute("import_profile", {"source": yaml_text})
        profile = await handler.db.get_profile("imported")
        assert profile is not None
        assert profile.name == "Imported Profile"
        assert profile.allowed_tools == ["Read", "Write"]

    async def test_edit_db_only_profile_creates_vault_file(self, handler):
        """Editing a profile that only exists in DB should create a vault file."""
        # Create profile directly in DB (simulating config.yaml sync)
        await handler.db.create_profile(
            AgentProfile(
                id="legacy",
                name="Legacy Profile",
                model="claude-sonnet-4-6",
                allowed_tools=["Read"],
            )
        )
        vault_path = handler._vault_profile_path("legacy")
        assert not os.path.isfile(vault_path)

        # Edit via command — should create vault file
        result = await handler.execute(
            "edit_profile",
            {"profile_id": "legacy", "description": "Now vault-backed"},
        )
        assert result.get("updated") == "legacy"
        assert os.path.isfile(vault_path)

        # Verify the vault file contains both old and new data
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.frontmatter.name == "Legacy Profile"
        assert parsed.config["model"] == "claude-sonnet-4-6"
        assert parsed.frontmatter.extra.get("description") == "Now vault-backed"

    async def test_create_with_install(self, handler):
        """create_profile with install should round-trip through vault."""
        result = await handler.execute(
            "create_profile",
            {
                "id": "docker-dev",
                "name": "Docker Dev",
                "install": {"commands": ["docker"], "npm": ["eslint"]},
            },
        )
        assert result.get("created") == "docker-dev"

        profile = await handler.db.get_profile("docker-dev")
        assert profile.install == {"commands": ["docker"], "npm": ["eslint"]}

    async def test_create_with_system_prompt_suffix(self, handler):
        """system_prompt_suffix should be split into sections in the vault file."""
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Reviewer",
                "system_prompt_suffix": "## Role\nYou review code.\n\n## Rules\n- Be thorough",
            },
        )

        vault_path = handler._vault_profile_path("reviewer")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.role == "You review code."
        assert "Be thorough" in parsed.rules
