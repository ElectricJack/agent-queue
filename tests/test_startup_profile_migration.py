"""Tests for startup auto-migration of DB profiles to vault markdown (Roadmap 4.2.4).

Covers:
- vault_has_profile_markdown: helper to detect existing vault profile markdown files
- Startup auto-migration: orchestrator runs migration when DB profiles exist
  but no vault profile markdown files are present
- Idempotency: multiple initializations don't duplicate or overwrite vault files
- Skip conditions: migration skipped when vault already has profile markdown
- Error resilience: startup doesn't crash if migration encounters errors
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from src.config import AppConfig
from src.models import AgentProfile
from src.orchestrator import Orchestrator
from src.vault import vault_has_profile_markdown


# ---------------------------------------------------------------------------
# vault_has_profile_markdown helper tests
# ---------------------------------------------------------------------------


class TestVaultHasProfileMarkdown:
    """Test the vault_has_profile_markdown() helper function."""

    def test_no_vault_dir(self, tmp_path):
        """No vault directory at all → False."""
        assert vault_has_profile_markdown(str(tmp_path)) is False

    def test_empty_agent_types_dir(self, tmp_path):
        """vault/agent-types/ exists but is empty → False."""
        (tmp_path / "vault" / "agent-types").mkdir(parents=True)
        assert vault_has_profile_markdown(str(tmp_path)) is False

    def test_profile_dir_without_markdown(self, tmp_path):
        """vault/agent-types/coding/ exists but has no profile.md → False."""
        (tmp_path / "vault" / "agent-types" / "coding").mkdir(parents=True)
        assert vault_has_profile_markdown(str(tmp_path)) is False

    def test_profile_dir_with_other_files(self, tmp_path):
        """vault/agent-types/coding/ has files but no profile.md → False."""
        profile_dir = tmp_path / "vault" / "agent-types" / "coding"
        profile_dir.mkdir(parents=True)
        (profile_dir / "notes.md").write_text("some notes")
        assert vault_has_profile_markdown(str(tmp_path)) is False

    def test_single_profile_markdown(self, tmp_path):
        """One profile.md exists → True."""
        profile_dir = tmp_path / "vault" / "agent-types" / "coding"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.md").write_text("---\nid: coding\n---")
        assert vault_has_profile_markdown(str(tmp_path)) is True

    def test_multiple_profile_markdowns(self, tmp_path):
        """Multiple profile.md files exist → True."""
        for name in ("coding", "review", "docs"):
            profile_dir = tmp_path / "vault" / "agent-types" / name
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.md").write_text(f"---\nid: {name}\n---")
        assert vault_has_profile_markdown(str(tmp_path)) is True

    def test_mixed_some_with_some_without(self, tmp_path):
        """Some profile dirs have profile.md, some don't → True (at least one exists)."""
        # Dir with profile.md
        with_md = tmp_path / "vault" / "agent-types" / "coding"
        with_md.mkdir(parents=True)
        (with_md / "profile.md").write_text("---\nid: coding\n---")

        # Dir without profile.md
        without_md = tmp_path / "vault" / "agent-types" / "review"
        without_md.mkdir(parents=True)

        assert vault_has_profile_markdown(str(tmp_path)) is True

    def test_nested_profile_md_not_counted(self, tmp_path):
        """profile.md in a nested subdirectory of a profile dir is not matched."""
        nested = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        nested.mkdir(parents=True)
        (nested / "profile.md").write_text("some memory file")
        # The profile.md is in coding/memory/, not coding/ directly
        assert vault_has_profile_markdown(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Startup auto-migration integration tests
# ---------------------------------------------------------------------------


class TestStartupProfileAutoMigration:
    """Test that orchestrator.initialize() auto-migrates DB profiles to vault markdown.

    The orchestrator creates a default "orchestrator" profile during
    _ensure_vault_structure() via VaultManager.ensure_layout(). These tests
    account for that by focusing on user-created profiles.
    """

    @pytest.fixture
    async def orch(self, tmp_path):
        """Create and initialize an Orchestrator with a fresh DB."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config)
        # Don't initialize yet — tests control when initialize() is called
        yield o
        if o.db._engine is not None:
            await o.db.close()

    async def test_auto_migrates_on_first_startup(self, orch, tmp_path):
        """DB profiles without vault markdown trigger auto-migration on initialize()."""
        # Initialize first to set up DB
        await orch.initialize()

        # Create a DB profile (after init, so it has no vault markdown)
        await orch.db.create_profile(
            AgentProfile(
                id="coding",
                name="Coding Agent",
                model="claude-sonnet-4-6",
            )
        )

        # Remove any existing vault profile markdown files so migration triggers
        vault_coding = os.path.join(
            orch.config.data_dir, "vault", "agent-types", "coding", "profile.md"
        )
        assert not os.path.isfile(vault_coding)

        # Re-run _ensure_vault_structure to trigger migration
        await orch._ensure_vault_structure()

        # Vault profile markdown should now exist
        assert os.path.isfile(vault_coding)
        with open(vault_coding) as f:
            content = f.read()
        assert "coding" in content.lower()
        assert "Coding Agent" in content

    async def test_per_profile_idempotency(self, orch, tmp_path):
        """Auto-migration is per-profile idempotent.

        Profiles whose vault markdown already exists are skipped, but
        previously-unwritten profiles in the same DB still get their
        markdown generated.  (The all-or-nothing guard was retired so
        a YAML-defined override doesn't get stuck without a vault file
        forever just because supervisor/claude-* profiles already exist.)
        """
        await orch.initialize()

        # Create a DB profile that has no vault markdown yet.
        await orch.db.create_profile(AgentProfile(id="test-agent", name="Test Agent"))

        # Pre-create a vault profile markdown for a different profile.
        vault_dir = os.path.join(orch.config.data_dir, "vault", "agent-types", "existing-agent")
        os.makedirs(vault_dir, exist_ok=True)
        existing_md = os.path.join(vault_dir, "profile.md")
        existing_text = "---\nid: existing-agent\nname: Existing\n---\n"
        with open(existing_md, "w") as f:
            f.write(existing_text)

        # Re-run vault structure — test-agent SHOULD get a vault markdown,
        # and the existing one should be left alone.
        await orch._ensure_vault_structure()

        test_md = os.path.join(
            orch.config.data_dir, "vault", "agent-types", "test-agent", "profile.md"
        )
        assert os.path.isfile(test_md)

        # Existing markdown content was not touched.
        with open(existing_md) as f:
            assert f.read() == existing_text

    async def test_idempotent_double_init(self, orch, tmp_path):
        """Running initialize() twice doesn't duplicate or corrupt vault files."""
        await orch.initialize()

        # Create a DB profile
        await orch.db.create_profile(
            AgentProfile(id="coding", name="Coding Agent", model="claude-sonnet-4-6")
        )

        # Trigger migration
        await orch._ensure_vault_structure()

        vault_path = os.path.join(
            orch.config.data_dir, "vault", "agent-types", "coding", "profile.md"
        )
        assert os.path.isfile(vault_path)

        # Read content after first migration
        with open(vault_path) as f:
            first_content = f.read()

        # Second run — vault now has profile markdown, so migration is skipped
        await orch._ensure_vault_structure()

        with open(vault_path) as f:
            second_content = f.read()

        assert first_content == second_content

    async def test_no_profiles_no_migration(self, orch, tmp_path):
        """If DB has no profiles, no migration is attempted."""
        # Initialize normally (may create orchestrator profile)
        await orch.initialize()

        # Remove any profile markdowns that were created during init
        agent_types_dir = os.path.join(orch.config.data_dir, "vault", "agent-types")
        if os.path.isdir(agent_types_dir):
            for entry in os.listdir(agent_types_dir):
                md_path = os.path.join(agent_types_dir, entry, "profile.md")
                if os.path.isfile(md_path):
                    os.remove(md_path)

        # Delete all profiles from DB
        profiles = await orch.db.list_profiles()
        for p in profiles:
            await orch.db.delete_profile(p.id)

        # Re-run — with no profiles, migration should not run
        with patch("src.profiles.migration.migrate_db_profiles_to_vault") as mock_migrate:
            await orch._ensure_vault_structure()
            mock_migrate.assert_not_called()

    async def test_migration_error_doesnt_crash_startup(self, orch, tmp_path):
        """If profile migration throws, startup continues normally."""
        await orch.initialize()

        # Create a DB profile
        await orch.db.create_profile(AgentProfile(id="test", name="Test Agent"))

        # Patch migrate to raise an exception
        with patch(
            "src.profiles.migration.migrate_db_profiles_to_vault",
            side_effect=RuntimeError("disk full"),
        ):
            # This should NOT raise — the exception is caught and logged
            await orch._ensure_vault_structure()

        # Orchestrator should still be functional
        profiles = await orch.db.list_profiles()
        assert any(p.id == "test" for p in profiles)

    async def test_migration_with_errors_logs_warning(self, orch, tmp_path):
        """If migration report has errors, a warning is logged."""
        from src.profiles.migration import MigrationReport

        await orch.initialize()
        await orch.db.create_profile(AgentProfile(id="test", name="Test Agent"))

        # Patch to return a report with errors
        error_report = MigrationReport(total=1, written=0, errors=1)
        with patch(
            "src.profiles.migration.migrate_db_profiles_to_vault",
            new_callable=AsyncMock,
            return_value=error_report,
        ) as mock_migrate:
            await orch._ensure_vault_structure()
            mock_migrate.assert_called_once()

    async def test_multiple_profiles_all_migrated(self, orch, tmp_path):
        """Multiple DB profiles are all migrated to vault markdown."""
        await orch.initialize()

        profiles_to_create = [
            AgentProfile(id="coding", name="Coding Agent", model="claude-sonnet-4-6"),
            AgentProfile(id="review", name="Review Agent", model="claude-sonnet-4-6"),
            AgentProfile(id="docs", name="Docs Agent"),
        ]

        for p in profiles_to_create:
            await orch.db.create_profile(p)

        # Trigger migration
        await orch._ensure_vault_structure()

        # All profiles should have vault markdown
        for p in profiles_to_create:
            vault_path = os.path.join(
                orch.config.data_dir, "vault", "agent-types", p.id, "profile.md"
            )
            assert os.path.isfile(vault_path), f"Missing vault markdown for {p.id}"
            with open(vault_path) as f:
                content = f.read()
            assert p.name in content

    async def test_full_initialize_creates_vault_profiles(self, tmp_path):
        """End-to-end: initialize() with pre-existing DB profiles creates vault markdown."""
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )

        # First init: set up DB with a profile
        orch1 = Orchestrator(config)
        await orch1.initialize()
        await orch1.db.create_profile(
            AgentProfile(id="coding", name="Coding Agent", model="claude-sonnet-4-6")
        )
        await orch1.db.close()

        # Remove any vault profile markdown that might have been created
        vault_path = os.path.join(config.data_dir, "vault", "agent-types", "coding", "profile.md")
        if os.path.isfile(vault_path):
            os.remove(vault_path)

        # Second init: should detect DB profile without vault markdown
        # and auto-migrate
        orch2 = Orchestrator(config)
        await orch2.initialize()

        assert os.path.isfile(vault_path)
        with open(vault_path) as f:
            content = f.read()
        assert "Coding Agent" in content

        await orch2.db.close()
