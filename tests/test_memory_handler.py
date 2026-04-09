"""Tests for src/memory_handler — memory.md vault watcher handler registration."""

from __future__ import annotations

import logging
import time

import pytest

from src.memory_handler import (
    MEMORY_PATTERNS,
    MemoryChangeInfo,
    derive_memory_scope,
    on_memory_changed,
    register_memory_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_memory_scope
# ---------------------------------------------------------------------------


class TestDeriveMemoryScope:
    """Tests for derive_memory_scope — extracting scope + identifier from paths."""

    def test_system_scope(self):
        scope, identifier = derive_memory_scope("system/memory/global-conventions.md")
        assert scope == "system"
        assert identifier is None

    def test_orchestrator_scope(self):
        scope, identifier = derive_memory_scope("orchestrator/memory/project-notes.md")
        assert scope == "orchestrator"
        assert identifier is None

    def test_project_scope(self):
        scope, identifier = derive_memory_scope("projects/my-app/memory/architecture.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_with_dashes(self):
        scope, identifier = derive_memory_scope(
            "projects/mech-fighters/memory/knowledge/arch.md"
        )
        assert scope == "project"
        assert identifier == "mech-fighters"

    def test_project_scope_nested_subdir(self):
        scope, identifier = derive_memory_scope(
            "projects/my-app/memory/knowledge/deep/nested.md"
        )
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_insights_subdir(self):
        scope, identifier = derive_memory_scope(
            "projects/my-app/memory/insights/task-learnings.md"
        )
        assert scope == "project"
        assert identifier == "my-app"

    def test_agent_type_scope(self):
        scope, identifier = derive_memory_scope(
            "agent-types/coding/memory/async-patterns.md"
        )
        assert scope == "agent_type"
        assert identifier == "coding"

    def test_agent_type_scope_with_complex_name(self):
        scope, identifier = derive_memory_scope(
            "agent-types/review-specialist/memory/review-tips.md"
        )
        assert scope == "agent_type"
        assert identifier == "review-specialist"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        scope, identifier = derive_memory_scope(
            "projects\\my-app\\memory\\knowledge\\arch.md"
        )
        assert scope == "project"
        assert identifier == "my-app"

    def test_unknown_top_level(self):
        """Unknown top-level directory falls through to the fallback."""
        scope, identifier = derive_memory_scope("custom/memory/notes.md")
        assert scope == "custom"
        assert identifier is None


# ---------------------------------------------------------------------------
# MemoryChangeInfo
# ---------------------------------------------------------------------------


class TestMemoryChangeInfo:
    """Tests for the MemoryChangeInfo dataclass."""

    def test_creation(self):
        info = MemoryChangeInfo(
            file_path="/vault/projects/app/memory/arch.md",
            change_type="modified",
            scope="project",
            identifier="app",
        )
        assert info.file_path == "/vault/projects/app/memory/arch.md"
        assert info.change_type == "modified"
        assert info.scope == "project"
        assert info.identifier == "app"

    def test_frozen(self):
        info = MemoryChangeInfo(
            file_path="/vault/system/memory/global.md",
            change_type="created",
            scope="system",
            identifier=None,
        )
        with pytest.raises(AttributeError):
            info.scope = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# on_memory_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnMemoryChanged:
    """Tests for the stub handler on_memory_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/system/memory/global.md",
            rel_path="system/memory/global.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await on_memory_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "memory.md" in record.message
        assert "modified" in record.message
        assert "system" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/system/memory/global.md",
                rel_path="system/memory/global.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app/memory/arch.md",
                rel_path="projects/app/memory/arch.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/agent-types/coding/memory/patterns.md",
                rel_path="agent-types/coding/memory/patterns.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await on_memory_changed(changes)

        assert len(caplog.records) == 3
        messages = [r.message for r in caplog.records]
        assert any("system" in m and "modified" in m for m in messages)
        assert any("project/app" in m and "created" in m for m in messages)
        assert any("agent_type/coding" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await on_memory_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_scope_correctly(self, caplog):
        """Verify scope/identifier derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/memory/knowledge/arch.md",
            rel_path="projects/mech-fighters/memory/knowledge/arch.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await on_memory_changed([change])

        assert "project/mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_singleton_scope_label(self, caplog):
        """Singleton scopes should not include an identifier in the label."""
        change = VaultChange(
            path="/vault/orchestrator/memory/project-notes.md",
            rel_path="orchestrator/memory/project-notes.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await on_memory_changed([change])

        msg = caplog.records[0].message
        assert "orchestrator" in msg
        # Should not have "orchestrator/None"
        assert "None" not in msg


# ---------------------------------------------------------------------------
# register_memory_handlers
# ---------------------------------------------------------------------------


class TestRegisterMemoryHandlers:
    """Tests for register_memory_handlers — wiring patterns to VaultWatcher."""

    def test_registers_all_patterns(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_memory_handlers(watcher)

        assert len(handler_ids) == len(MEMORY_PATTERNS)
        assert watcher.get_handler_count() == len(MEMORY_PATTERNS)

    def test_handler_ids_use_pattern_names(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_memory_handlers(watcher)

        for pattern, hid in zip(MEMORY_PATTERNS, handler_ids):
            assert hid == f"memory:{pattern}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler IDs."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        ids1 = register_memory_handlers(watcher)
        ids2 = register_memory_handlers(watcher)

        # Same IDs — register_handler with explicit IDs replaces
        assert ids1 == ids2
        # The handler count should still be the same (overwritten, not doubled)
        assert watcher.get_handler_count() == len(MEMORY_PATTERNS)


# ---------------------------------------------------------------------------
# Integration: patterns match the expected paths
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered patterns actually match memory file paths."""

    def test_system_memory_matches(self):
        assert VaultWatcher._matches_pattern(
            "system/memory/global-conventions.md", "system/memory/*.md"
        )

    def test_orchestrator_memory_matches(self):
        assert VaultWatcher._matches_pattern(
            "orchestrator/memory/project-notes.md", "orchestrator/memory/*.md"
        )

    def test_agent_type_memory_matches(self):
        assert VaultWatcher._matches_pattern(
            "agent-types/coding/memory/async-patterns.md",
            "agent-types/*/memory/*.md",
        )

    def test_project_memory_flat_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/architecture.md",
            "projects/*/memory/**/*.md",
        )

    def test_project_memory_knowledge_subdir_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/knowledge/arch.md",
            "projects/*/memory/**/*.md",
        )

    def test_project_memory_insights_subdir_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/insights/task-learnings.md",
            "projects/*/memory/**/*.md",
        )

    def test_project_memory_deeply_nested_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/knowledge/deep/nested/file.md",
            "projects/*/memory/**/*.md",
        )

    def test_system_pattern_does_not_match_project(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/memory/arch.md", "system/memory/*.md"
        )

    def test_orchestrator_pattern_does_not_match_agent_type(self):
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/memory/patterns.md", "orchestrator/memory/*.md"
        )

    def test_non_md_file_does_not_match(self):
        """Non-.md files should not match any pattern."""
        for pattern in MEMORY_PATTERNS:
            assert not VaultWatcher._matches_pattern(
                "system/memory/notes.txt", pattern
            )

    def test_non_memory_dir_does_not_match(self):
        """Files outside memory/ directories should not match."""
        for pattern in MEMORY_PATTERNS:
            assert not VaultWatcher._matches_pattern(
                "system/playbooks/deploy.md", pattern
            )

    def test_no_cross_scope_matching(self):
        """Each scope's pattern should only match its own scope."""
        # system pattern does not match orchestrator
        assert not VaultWatcher._matches_pattern(
            "orchestrator/memory/notes.md", "system/memory/*.md"
        )
        # project pattern does not match agent-type
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/memory/patterns.md",
            "projects/*/memory/**/*.md",
        )
        # agent-type pattern does not match project
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/memory/arch.md",
            "agent-types/*/memory/*.md",
        )


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects memory file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change → VaultWatcher → handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_system_memory(self, tmp_path):
        """Create a system memory file and verify the handler is called."""
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

        watcher.register_handler("system/memory/*.md", capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create system memory directory and file
        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "global-conventions.md").write_text(
            "# Conventions\n"
        )

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "system/memory/global-conventions.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_project_memory_nested(self, tmp_path):
        """Create a nested project memory file and verify dispatch."""
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

        watcher.register_handler("projects/*/memory/**/*.md", capture_handler)

        # Initial snapshot
        await watcher.check()

        # Create nested project memory file
        mem_dir = vault / "projects" / "my-app" / "memory" / "knowledge"
        mem_dir.mkdir(parents=True)
        (mem_dir / "arch.md").write_text("# Architecture\n")

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/memory/knowledge/arch.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_modification(self, tmp_path):
        """Modify an existing memory file and verify dispatch."""
        vault = tmp_path / "vault"
        mem_dir = vault / "orchestrator" / "memory"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "project-notes.md"
        mem_file.write_text("# Notes\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("orchestrator/memory/*.md", capture_handler)

        # Initial snapshot includes existing file
        await watcher.check()

        # Modify the file (need different mtime)
        time.sleep(0.05)
        mem_file.write_text("# Notes\n- updated: true\n")

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "orchestrator/memory/project-notes.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_full_handler_with_all_patterns(self, tmp_path, caplog):
        """Register all memory patterns via register_memory_handlers and verify."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_memory_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create memory files in multiple scopes
        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "conventions.md").write_text("# System\n")

        (vault / "orchestrator" / "memory").mkdir(parents=True)
        (vault / "orchestrator" / "memory" / "notes.md").write_text("# Orch\n")

        (vault / "agent-types" / "coding" / "memory").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "memory" / "patterns.md").write_text(
            "# Patterns\n"
        )

        (vault / "projects" / "app" / "memory" / "knowledge").mkdir(parents=True)
        (
            vault / "projects" / "app" / "memory" / "knowledge" / "arch.md"
        ).write_text("# Arch\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.memory_handler"):
            await watcher.check()

        # The stub handler should have logged all 4
        handler_logs = [
            r
            for r in caplog.records
            if "memory.md" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 4

    @pytest.mark.asyncio
    async def test_non_memory_file_not_dispatched(self, tmp_path):
        """Files outside memory/ directories should not trigger the handler."""
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

        watcher.register_handler("system/memory/*.md", capture_handler)
        await watcher.check()

        # Create a non-memory file
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Deploy\n")

        await watcher.check()

        assert len(dispatched) == 0
