"""Tests for the inline mcp_servers → registry migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.profiles.mcp_inline_migration import (
    migrate_vault_profiles,
    migrate_yaml_config_profiles,
)
from src.profiles.mcp_registry import McpRegistry, parse_server_markdown
from src.profiles.parser import parse_profile


LEGACY_SYSTEM_PROFILE = """---
id: coding
name: Coding Agent
---

# Coding Agent

## Role
You write code.

## Config
```json
{
  "model": "claude-sonnet-4-6"
}
```

## MCP Servers
```json
{
  "playwright": {
    "command": "npx",
    "args": ["@anthropic/mcp-playwright"],
    "env": {"DEBUG": "1"}
  },
  "internal-api": {
    "type": "http",
    "url": "https://api.internal/mcp",
    "headers": {"X-Token": "abc"}
  }
}
```
"""


LEGACY_PROJECT_PROFILE = """---
id: project:acme:coding
name: Acme Coding
---

# Acme Coding

## MCP Servers
```json
{
  "gmail": {
    "command": "npx",
    "args": ["gmail-mcp"],
    "env": {"GMAIL_ACCOUNT": "x@y.z"}
  }
}
```
"""


ALREADY_MIGRATED = """---
id: reviewer
name: Reviewer
---

# Reviewer

## MCP Servers
```json
["playwright"]
```
"""


@pytest.fixture
def vault_with_legacy(tmp_path: Path) -> Path:
    """Build a vault with legacy + migrated profiles for the migration to chew on."""
    vault = tmp_path / "data" / "vault"
    sys_dir = vault / "agent-types" / "coding"
    proj_dir = vault / "projects" / "acme" / "agent-types" / "coding"
    migrated_dir = vault / "agent-types" / "reviewer"
    sys_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    migrated_dir.mkdir(parents=True)
    (sys_dir / "profile.md").write_text(LEGACY_SYSTEM_PROFILE)
    (proj_dir / "profile.md").write_text(LEGACY_PROJECT_PROFILE)
    (migrated_dir / "profile.md").write_text(ALREADY_MIGRATED)
    return vault


# ---------------------------------------------------------------------------
# vault profile migration
# ---------------------------------------------------------------------------


class TestMigrateVaultProfiles:
    def test_extracts_system_and_project_inline_configs(self, vault_with_legacy: Path):
        registry = McpRegistry()
        report = migrate_vault_profiles(str(vault_with_legacy), registry=registry)

        assert report.profiles_examined == 3
        assert report.profiles_migrated == 2
        # Already-migrated reviewer was untouched.
        migrated_ids = {m.profile_id for m in report.migrated_profiles}
        assert "reviewer" not in migrated_ids
        assert "coding" in migrated_ids
        assert "project:acme:coding" in migrated_ids

        # Three inline entries extracted: 2 system + 1 project.
        names_by_scope = {(e.project_id, e.name) for e in report.extracted}
        assert (None, "playwright") in names_by_scope
        assert (None, "internal-api") in names_by_scope
        assert ("acme", "gmail") in names_by_scope

    def test_writes_registry_files_with_correct_content(self, vault_with_legacy: Path):
        report = migrate_vault_profiles(str(vault_with_legacy))

        # System scope playwright file written
        pw_path = vault_with_legacy / "mcp-servers" / "playwright.md"
        assert pw_path.is_file()
        parsed = parse_server_markdown(pw_path.read_text())
        assert parsed.is_valid
        assert parsed.config.command == "npx"
        assert parsed.config.args == ["@anthropic/mcp-playwright"]
        assert parsed.config.env == {"DEBUG": "1"}

        # System scope HTTP file
        http_path = vault_with_legacy / "mcp-servers" / "internal-api.md"
        parsed = parse_server_markdown(http_path.read_text())
        assert parsed.is_valid
        assert parsed.config.transport == "http"
        assert parsed.config.url == "https://api.internal/mcp"
        assert parsed.config.headers == {"X-Token": "abc"}

        # Project scope file
        gmail_path = vault_with_legacy / "projects" / "acme" / "mcp-servers" / "gmail.md"
        assert gmail_path.is_file()
        parsed = parse_server_markdown(gmail_path.read_text())
        assert parsed.config.env == {"GMAIL_ACCOUNT": "x@y.z"}

        # Discard the report; we asserted directly on the files.
        _ = report

    def test_rewrites_profile_md_with_list_form(self, vault_with_legacy: Path):
        migrate_vault_profiles(str(vault_with_legacy))

        # The coding profile should now have list[str] form
        rewritten = (vault_with_legacy / "agent-types" / "coding" / "profile.md").read_text()
        parsed = parse_profile(rewritten)
        assert parsed.is_valid
        assert parsed.mcp_servers_legacy is None  # no longer dict-shaped
        assert sorted(parsed.mcp_servers) == ["internal-api", "playwright"]

    def test_idempotent_on_already_migrated(self, vault_with_legacy: Path):
        migrate_vault_profiles(str(vault_with_legacy))
        # Run again — should be a no-op now (no profiles need migration).
        report = migrate_vault_profiles(str(vault_with_legacy))
        assert report.profiles_migrated == 0
        assert report.extracted == []

    def test_does_not_clobber_existing_registry_file(self, vault_with_legacy: Path):
        # Pre-create a registry file with different content.
        sys_mcp = vault_with_legacy / "mcp-servers"
        sys_mcp.mkdir(parents=True, exist_ok=True)
        custom = "---\nname: playwright\ntransport: stdio\ncommand: my-custom\n---\n"
        (sys_mcp / "playwright.md").write_text(custom)

        report = migrate_vault_profiles(str(vault_with_legacy))

        # Skip action recorded for the existing file
        actions = {(e.name, e.action) for e in report.extracted}
        assert ("playwright", "skipped") in actions

        # Custom content preserved
        assert (sys_mcp / "playwright.md").read_text() == custom

        # Profile.md still got rewritten so it references "playwright" by name
        rewritten = (vault_with_legacy / "agent-types" / "coding" / "profile.md").read_text()
        parsed = parse_profile(rewritten)
        assert "playwright" in parsed.mcp_servers

    def test_updates_in_memory_registry(self, vault_with_legacy: Path):
        registry = McpRegistry()
        migrate_vault_profiles(str(vault_with_legacy), registry=registry)
        assert registry.get("playwright") is not None
        assert registry.get("playwright").transport == "stdio"
        assert registry.get("internal-api") is not None
        assert registry.get("internal-api").transport == "http"
        assert registry.get("gmail", project_id="acme") is not None

    def test_malformed_inline_entry_logged_not_fatal(self, tmp_path: Path):
        vault = tmp_path / "data" / "vault"
        d = vault / "agent-types" / "x"
        d.mkdir(parents=True)
        (d / "profile.md").write_text(
            """---
id: x
name: X
---

## MCP Servers
```json
{
  "broken": {"command": ""},
  "good": {"command": "ls"}
}
```
"""
        )
        report = migrate_vault_profiles(str(vault))
        # The good one was extracted; broken one is reported as an error
        names = {e.name for e in report.extracted}
        assert "good" in names
        assert "broken" not in names
        assert any("broken" in err for err in report.errors)

    def test_empty_vault(self, tmp_path: Path):
        report = migrate_vault_profiles(str(tmp_path / "vault"))
        assert report.profiles_examined == 0
        assert report.profiles_migrated == 0


# ---------------------------------------------------------------------------
# YAML config migration (extract only, no rewrite)
# ---------------------------------------------------------------------------


class _ConfigProfile:
    """Minimal stand-in for AgentProfileConfig."""

    def __init__(self, id: str, mcp_servers):
        self.id = id
        self.mcp_servers = mcp_servers


class TestMigrateYamlConfigProfiles:
    def test_extracts_inline_yaml_to_system_scope(self, tmp_path: Path):
        vault = tmp_path / "data" / "vault"
        vault.mkdir(parents=True)
        registry = McpRegistry()

        profiles = [
            _ConfigProfile(
                "yaml-coding",
                {
                    "playwright": {
                        "command": "npx",
                        "args": ["@anthropic/mcp-playwright"],
                    }
                },
            ),
        ]

        report = migrate_yaml_config_profiles(profiles, str(vault), registry=registry)

        assert report.profiles_migrated == 1
        path = vault / "mcp-servers" / "playwright.md"
        assert path.is_file()
        assert registry.get("playwright") is not None

    def test_skips_already_migrated_yaml(self, tmp_path: Path):
        vault = tmp_path / "data" / "vault"
        vault.mkdir(parents=True)
        # mcp_servers already a list[str] — nothing to extract
        profiles = [_ConfigProfile("clean", ["agent-queue"])]
        report = migrate_yaml_config_profiles(profiles, str(vault))
        assert report.profiles_migrated == 0
        assert report.extracted == []

    def test_handles_missing_field(self, tmp_path: Path):
        vault = tmp_path / "data" / "vault"
        vault.mkdir(parents=True)
        profiles = [_ConfigProfile("no-mcp", None)]
        report = migrate_yaml_config_profiles(profiles, str(vault))
        assert report.profiles_migrated == 0
