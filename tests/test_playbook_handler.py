"""Tests for src/playbook_handler — playbook .md vault watcher handler registration."""

from __future__ import annotations

import logging
import time

import pytest

from src.playbook_handler import (
    PLAYBOOK_PATTERNS,
    derive_playbook_scope,
    on_playbook_changed,
    register_playbook_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_playbook_scope
# ---------------------------------------------------------------------------


class TestDerivePlaybookScope:
    """Tests for derive_playbook_scope — extracting scope + identifier from paths."""

    def test_system_scope(self):
        scope, identifier = derive_playbook_scope("system/playbooks/deploy.md")
        assert scope == "system"
        assert identifier is None

    def test_orchestrator_scope(self):
        scope, identifier = derive_playbook_scope("orchestrator/playbooks/routing.md")
        assert scope == "orchestrator"
        assert identifier is None

    def test_project_scope(self):
        scope, identifier = derive_playbook_scope("projects/my-app/playbooks/review.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_with_dashes(self):
        scope, identifier = derive_playbook_scope("projects/mech-fighters/playbooks/deploy.md")
        assert scope == "project"
        assert identifier == "mech-fighters"

    def test_agent_type_scope(self):
        scope, identifier = derive_playbook_scope("agent-types/coding/playbooks/quality.md")
        assert scope == "agent_type"
        assert identifier == "coding"

    def test_agent_type_scope_with_complex_name(self):
        scope, identifier = derive_playbook_scope("agent-types/review-specialist/playbooks/gate.md")
        assert scope == "agent_type"
        assert identifier == "review-specialist"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        scope, identifier = derive_playbook_scope("projects\\my-app\\playbooks\\deploy.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_unknown_top_level(self):
        """Unknown top-level directory falls through to the fallback."""
        scope, identifier = derive_playbook_scope("custom/playbooks/foo.md")
        assert scope == "custom"
        assert identifier is None


# ---------------------------------------------------------------------------
# on_playbook_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnPlaybookChanged:
    """Tests for the stub handler on_playbook_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/system/playbooks/deploy.md",
            rel_path="system/playbooks/deploy.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "Playbook" in record.message
        assert "modified" in record.message
        assert "system" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/system/playbooks/deploy.md",
                rel_path="system/playbooks/deploy.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app/playbooks/review.md",
                rel_path="projects/app/playbooks/review.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/agent-types/coding/playbooks/gate.md",
                rel_path="agent-types/coding/playbooks/gate.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed(changes)

        assert len(caplog.records) == 3
        messages = [r.message for r in caplog.records]
        assert any("system" in m and "modified" in m for m in messages)
        assert any("project/app" in m and "created" in m for m in messages)
        assert any("agent_type/coding" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_scope_correctly(self, caplog):
        """Verify scope/identifier derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/playbooks/deploy.md",
            rel_path="projects/mech-fighters/playbooks/deploy.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed([change])

        assert "project/mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_singleton_scope_label(self, caplog):
        """Singleton scopes should not include an identifier in the label."""
        change = VaultChange(
            path="/vault/orchestrator/playbooks/routing.md",
            rel_path="orchestrator/playbooks/routing.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed([change])

        msg = caplog.records[0].message
        assert "orchestrator" in msg
        assert "None" not in msg

    @pytest.mark.asyncio
    async def test_handler_receives_file_path_and_change_type(self, caplog):
        """Handler should log both the file path and the change type."""
        change = VaultChange(
            path="/vault/projects/app/playbooks/deploy.md",
            rel_path="projects/app/playbooks/deploy.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await on_playbook_changed([change])

        msg = caplog.records[0].message
        assert "created" in msg
        assert "projects/app/playbooks/deploy.md" in msg


# ---------------------------------------------------------------------------
# register_playbook_handlers
# ---------------------------------------------------------------------------


class TestRegisterPlaybookHandlers:
    """Tests for register_playbook_handlers — wiring patterns to VaultWatcher."""

    def test_registers_all_patterns(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_playbook_handlers(watcher)

        assert len(handler_ids) == len(PLAYBOOK_PATTERNS)
        assert watcher.get_handler_count() == len(PLAYBOOK_PATTERNS)

    def test_handler_ids_use_pattern_names(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_playbook_handlers(watcher)

        for pattern, hid in zip(PLAYBOOK_PATTERNS, handler_ids):
            assert hid == f"playbook:{pattern}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler IDs."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        ids1 = register_playbook_handlers(watcher)
        ids2 = register_playbook_handlers(watcher)

        assert ids1 == ids2
        assert watcher.get_handler_count() == len(PLAYBOOK_PATTERNS)


# ---------------------------------------------------------------------------
# Integration: patterns match the expected paths
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered patterns match expected playbook file paths."""

    def test_system_playbook_matches(self):
        assert VaultWatcher._matches_pattern("system/playbooks/deploy.md", "system/playbooks/*.md")

    def test_orchestrator_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "orchestrator/playbooks/routing.md", "orchestrator/playbooks/*.md"
        )

    def test_project_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/review.md", "projects/*/playbooks/*.md"
        )

    def test_agent_type_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/quality.md",
            "agent-types/*/playbooks/*.md",
        )

    def test_system_pattern_does_not_match_project(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md", "system/playbooks/*.md"
        )

    def test_project_pattern_does_not_match_agent_type(self):
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/gate.md", "projects/*/playbooks/*.md"
        )

    def test_agent_type_pattern_does_not_match_project(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md",
            "agent-types/*/playbooks/*.md",
        )

    def test_non_playbook_file_does_not_match(self):
        """A non-playbook markdown file should not match any pattern."""
        for pattern in PLAYBOOK_PATTERNS:
            assert not VaultWatcher._matches_pattern("projects/my-app/notes.md", pattern)

    def test_non_md_file_does_not_match(self):
        """A non-.md file in playbooks/ should not match."""
        for pattern in PLAYBOOK_PATTERNS:
            assert not VaultWatcher._matches_pattern("system/playbooks/deploy.yaml", pattern)

    def test_no_cross_scope_matching(self):
        """Each scope's pattern should only match its own scope."""
        assert not VaultWatcher._matches_pattern(
            "orchestrator/playbooks/route.md", "system/playbooks/*.md"
        )
        assert not VaultWatcher._matches_pattern(
            "system/playbooks/deploy.md", "orchestrator/playbooks/*.md"
        )
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/gate.md",
            "projects/*/playbooks/*.md",
        )
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/review.md",
            "agent-types/*/playbooks/*.md",
        )


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects playbook file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change -> VaultWatcher -> handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_project_playbook(self, tmp_path):
        """Create a project playbook and verify the handler is called."""
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

        watcher.register_handler("projects/*/playbooks/*.md", capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create a project playbook file
        playbook_dir = vault / "projects" / "my-app" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text("# Deploy Playbook\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/playbooks/deploy.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_system_playbook_modification(self, tmp_path):
        """Modify system playbook and verify handler dispatch."""
        vault = tmp_path / "vault"
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "notify.md"
        playbook_file.write_text("# Notify Playbook\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("system/playbooks/*.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Modify the file (need different mtime)
        time.sleep(0.05)
        playbook_file.write_text("# Notify Playbook\n- updated: true\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "system/playbooks/notify.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_detects_playbook_deletion(self, tmp_path):
        """Delete a playbook and verify deletion is dispatched."""
        vault = tmp_path / "vault"
        playbook_dir = vault / "orchestrator" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "routing.md"
        playbook_file.write_text("# Routing Playbook\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("orchestrator/playbooks/*.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Delete the file
        playbook_file.unlink()

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "orchestrator/playbooks/routing.md"
        assert dispatched[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_full_handler_with_all_patterns(self, tmp_path, caplog):
        """Register all playbook patterns via register_playbook_handlers and verify."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_playbook_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create playbook files in multiple scopes
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Deploy\n")

        (vault / "projects" / "app" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "playbooks" / "review.md").write_text("# Review\n")

        (vault / "agent-types" / "coder" / "playbooks").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "playbooks" / "gate.md").write_text("# Gate\n")

        (vault / "orchestrator" / "playbooks").mkdir(parents=True)
        (vault / "orchestrator" / "playbooks" / "route.md").write_text("# Route\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.playbook_handler"):
            await watcher.check()

        # The stub handler should have logged all 4
        handler_logs = [
            r for r in caplog.records if "Playbook" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 4

    @pytest.mark.asyncio
    async def test_non_playbook_file_not_dispatched(self, tmp_path):
        """Other .md files in the same scope should not trigger the handler."""
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("system/playbooks/*.md", capture_handler)
        await watcher.check()

        # Create a file outside playbooks/
        (vault / "system" / "notes.md").write_text("# Notes\n")

        await watcher.check()

        assert len(dispatched) == 0
