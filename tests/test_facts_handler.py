"""Tests for src/facts_handler — facts.md vault watcher handler registration."""

from __future__ import annotations

import logging

import pytest

from src.facts_handler import (
    FACTS_PATTERNS,
    FactsChangeInfo,
    derive_scope,
    on_facts_changed,
    register_facts_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_scope
# ---------------------------------------------------------------------------


class TestDeriveScope:
    """Tests for derive_scope — extracting scope + identifier from paths."""

    def test_system_scope(self):
        scope, identifier = derive_scope("system/facts.md")
        assert scope == "system"
        assert identifier is None

    def test_orchestrator_scope(self):
        scope, identifier = derive_scope("orchestrator/facts.md")
        assert scope == "orchestrator"
        assert identifier is None

    def test_project_scope(self):
        scope, identifier = derive_scope("projects/my-app/facts.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_with_dashes(self):
        scope, identifier = derive_scope("projects/mech-fighters/facts.md")
        assert scope == "project"
        assert identifier == "mech-fighters"

    def test_agent_type_scope(self):
        scope, identifier = derive_scope("agent-types/coding/facts.md")
        assert scope == "agent_type"
        assert identifier == "coding"

    def test_agent_type_scope_with_complex_name(self):
        scope, identifier = derive_scope("agent-types/review-specialist/facts.md")
        assert scope == "agent_type"
        assert identifier == "review-specialist"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        scope, identifier = derive_scope("projects\\my-app\\facts.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_unknown_top_level(self):
        """Unknown top-level directory falls through to the fallback."""
        scope, identifier = derive_scope("custom/facts.md")
        assert scope == "custom"
        assert identifier is None


# ---------------------------------------------------------------------------
# FactsChangeInfo
# ---------------------------------------------------------------------------


class TestFactsChangeInfo:
    """Tests for the FactsChangeInfo dataclass."""

    def test_creation(self):
        info = FactsChangeInfo(
            file_path="/vault/projects/app/facts.md",
            change_type="modified",
            scope="project",
            identifier="app",
        )
        assert info.file_path == "/vault/projects/app/facts.md"
        assert info.change_type == "modified"
        assert info.scope == "project"
        assert info.identifier == "app"

    def test_frozen(self):
        info = FactsChangeInfo(
            file_path="/vault/system/facts.md",
            change_type="created",
            scope="system",
            identifier=None,
        )
        with pytest.raises(AttributeError):
            info.scope = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# on_facts_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnFactsChanged:
    """Tests for the stub handler on_facts_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/system/facts.md",
            rel_path="system/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "facts.md" in record.message
        assert "modified" in record.message
        assert "system" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/system/facts.md",
                rel_path="system/facts.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app/facts.md",
                rel_path="projects/app/facts.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/agent-types/coding/facts.md",
                rel_path="agent-types/coding/facts.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed(changes)

        assert len(caplog.records) == 3
        # Check scope derivation is correct in log messages
        messages = [r.message for r in caplog.records]
        assert any("system" in m and "modified" in m for m in messages)
        assert any("project/app" in m and "created" in m for m in messages)
        assert any("agent_type/coding" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_scope_correctly(self, caplog):
        """Verify scope/identifier derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/facts.md",
            rel_path="projects/mech-fighters/facts.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        assert "project/mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_singleton_scope_label(self, caplog):
        """Singleton scopes should not include an identifier in the label."""
        change = VaultChange(
            path="/vault/orchestrator/facts.md",
            rel_path="orchestrator/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        msg = caplog.records[0].message
        assert "orchestrator" in msg
        # Should not have "orchestrator/None"
        assert "None" not in msg


# ---------------------------------------------------------------------------
# register_facts_handlers
# ---------------------------------------------------------------------------


class TestRegisterFactsHandlers:
    """Tests for register_facts_handlers — wiring patterns to VaultWatcher."""

    def test_registers_all_patterns(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_facts_handlers(watcher)

        assert len(handler_ids) == len(FACTS_PATTERNS)
        assert watcher.get_handler_count() == len(FACTS_PATTERNS)

    def test_handler_ids_use_pattern_names(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_facts_handlers(watcher)

        for pattern, hid in zip(FACTS_PATTERNS, handler_ids):
            assert hid == f"facts:{pattern}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler IDs."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        ids1 = register_facts_handlers(watcher)
        ids2 = register_facts_handlers(watcher)

        # Same IDs — register_handler with explicit IDs replaces
        assert ids1 == ids2
        # The handler count should still be the same (overwritten, not doubled)
        assert watcher.get_handler_count() == len(FACTS_PATTERNS)


# ---------------------------------------------------------------------------
# Integration: patterns match the expected paths
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered patterns actually match fact file paths."""

    def test_system_facts_matches(self):
        assert VaultWatcher._matches_pattern("system/facts.md", "system/facts.md")

    def test_orchestrator_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "orchestrator/facts.md", "orchestrator/facts.md"
        )

    def test_project_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "projects/*/facts.md"
        )

    def test_agent_type_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "agent-types/*/facts.md"
        )

    def test_system_pattern_does_not_match_project(self):
        """The literal system pattern should not match project facts."""
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "system/facts.md"
        )

    def test_orchestrator_pattern_does_not_match_agent_type(self):
        """The literal orchestrator pattern should not match agent-type facts."""
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "orchestrator/facts.md"
        )

    def test_non_facts_file_does_not_match(self):
        """A non-facts markdown file should not match any pattern."""
        for pattern in FACTS_PATTERNS:
            assert not VaultWatcher._matches_pattern(
                "projects/my-app/notes.md", pattern
            )

    def test_no_cross_scope_matching(self):
        """Each scope's pattern should only match its own scope."""
        # system pattern does not match orchestrator
        assert not VaultWatcher._matches_pattern(
            "orchestrator/facts.md", "system/facts.md"
        )
        # project pattern does not match agent-type
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "projects/*/facts.md"
        )
        # agent-type pattern does not match project
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "agent-types/*/facts.md"
        )


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects fact file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change → VaultWatcher → handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_project_facts(self, tmp_path):
        """Create a project facts.md and verify the handler is called."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("projects/*/facts.md", capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create a project facts file
        project_dir = vault / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text("# Facts\n- key: value\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/facts.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_system_facts_modification(self, tmp_path):
        """Modify system/facts.md and verify handler dispatch."""
        vault = tmp_path / "vault"
        system_dir = vault / "system"
        system_dir.mkdir(parents=True)
        facts_file = system_dir / "facts.md"
        facts_file.write_text("# System Facts\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("*/facts.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Modify the file (need different mtime)
        import time

        time.sleep(0.05)
        facts_file.write_text("# System Facts\n- updated: true\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "system/facts.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_full_handler_with_all_patterns(self, tmp_path, caplog):
        """Register all facts patterns via register_facts_handlers and verify dispatch."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_facts_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create facts files in multiple scopes
        (vault / "system").mkdir()
        (vault / "system" / "facts.md").write_text("# System\n")

        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "facts.md").write_text("# Project\n")

        (vault / "agent-types" / "coder").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "facts.md").write_text("# Agent\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await watcher.check()

        # The stub handler should have logged all 3
        handler_logs = [
            r for r in caplog.records
            if "facts.md" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 3

    @pytest.mark.asyncio
    async def test_non_facts_file_not_dispatched(self, tmp_path):
        """Other files in the same directory should not trigger the handler."""
        vault = tmp_path / "vault"
        (vault / "system").mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("*/facts.md", capture_handler)
        await watcher.check()

        # Create a non-facts file
        (vault / "system" / "notes.md").write_text("# Notes\n")

        await watcher.check()

        assert len(dispatched) == 0
