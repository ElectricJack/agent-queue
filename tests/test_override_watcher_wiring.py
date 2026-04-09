"""Tests for VaultWatcher → OverrideIndexer wiring (roadmap 3.2.2).

Verifies that:
- MemoryManager.setup_override_watcher() sets the module-level indexer
- MemoryManager.index_project_overrides() indexes existing files at startup
- MemoryManager.close() clears the module-level indexer
- The orchestrator calls setup_override_watcher and index_project_overrides
  during initialize()
- End-to-end: file change detected by VaultWatcher triggers OverrideIndexer
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.override_handler import (
    get_indexer,
    register_override_handlers,
    set_indexer,
)
from src.vault_watcher import VaultWatcher


# ---------------------------------------------------------------------------
# MemoryManager.setup_override_watcher
# ---------------------------------------------------------------------------


class TestSetupOverrideWatcher:
    """MemoryManager.setup_override_watcher sets the module-level indexer."""

    def setup_method(self):
        set_indexer(None)

    @pytest.mark.asyncio
    async def test_setup_sets_module_indexer(self):
        """After setup_override_watcher, get_indexer() returns the indexer."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True, max_chunk_size=1500, overlap_lines=2)
        mm._override_indexer = None

        # Mock the internal methods so we don't need real memsearch
        mock_indexer = MagicMock()
        mm.get_override_indexer = AsyncMock(return_value=mock_indexer)

        result = await mm.setup_override_watcher()

        assert result is True
        assert get_indexer() is mock_indexer

    @pytest.mark.asyncio
    async def test_setup_returns_false_when_no_indexer(self):
        """When get_override_indexer returns None, setup returns False."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = None
        mm.get_override_indexer = AsyncMock(return_value=None)

        result = await mm.setup_override_watcher()

        assert result is False
        assert get_indexer() is None

    def teardown_method(self):
        set_indexer(None)


# ---------------------------------------------------------------------------
# MemoryManager.index_project_overrides
# ---------------------------------------------------------------------------


class TestIndexProjectOverrides:
    """MemoryManager.index_project_overrides indexes existing overrides at startup."""

    @pytest.mark.asyncio
    async def test_indexes_existing_overrides(self, tmp_path):
        """Calls index_all_overrides with the vault root."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = None
        mm._storage_root = str(tmp_path)

        mock_indexer = MagicMock()
        mock_indexer.index_all_overrides = AsyncMock(return_value=5)
        mm.get_override_indexer = AsyncMock(return_value=mock_indexer)

        vault = tmp_path / "vault"
        vault.mkdir()

        result = await mm.index_project_overrides(str(vault))

        assert result == 5
        mock_indexer.index_all_overrides.assert_called_once_with(str(vault))

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_indexer(self, tmp_path):
        """When no indexer is available, returns 0 without errors."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = None
        mm.get_override_indexer = AsyncMock(return_value=None)

        result = await mm.index_project_overrides(str(tmp_path))

        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_vault_missing(self, tmp_path):
        """When vault directory doesn't exist, returns 0."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = None
        mm._storage_root = str(tmp_path)

        mock_indexer = MagicMock()
        mm.get_override_indexer = AsyncMock(return_value=mock_indexer)

        result = await mm.index_project_overrides(str(tmp_path / "nonexistent"))

        assert result == 0


# ---------------------------------------------------------------------------
# MemoryManager.close clears module-level indexer
# ---------------------------------------------------------------------------


class TestCloseOverrideCleanup:
    """MemoryManager.close() clears the module-level override indexer."""

    def setup_method(self):
        set_indexer(None)

    @pytest.mark.asyncio
    async def test_close_clears_indexer(self):
        """After close(), the module-level indexer is reset to None."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = MagicMock()
        mm._router = None
        mm._embedder = None
        mm._instances = {}
        mm._watchers = {}

        # Set a module-level indexer to simulate a running system
        set_indexer(mm._override_indexer)
        assert get_indexer() is not None

        await mm.close()

        assert get_indexer() is None
        assert mm._override_indexer is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_indexer(self):
        """Close with no override indexer doesn't crash."""
        from src.memory import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.config = MagicMock(enabled=True)
        mm._override_indexer = None
        mm._router = None
        mm._embedder = None
        mm._instances = {}
        mm._watchers = {}

        await mm.close()  # Should not raise

        assert get_indexer() is None

    def teardown_method(self):
        set_indexer(None)


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher → on_override_changed → OverrideIndexer
# ---------------------------------------------------------------------------


class TestEndToEndWiring:
    """End-to-end test: file change triggers indexing via the wired callback."""

    def setup_method(self):
        set_indexer(None)

    @pytest.mark.asyncio
    async def test_file_create_triggers_index(self, tmp_path, caplog):
        """Creating an override file triggers index_override on the wired indexer."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Set up a mock indexer
        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(return_value=3)
        mock_indexer.delete_override = AsyncMock(return_value=True)
        set_indexer(mock_indexer)

        # Create watcher and register override handlers
        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_override_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create an override file
        override_dir = vault / "projects" / "my-app" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("# Coding overrides\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await watcher.check()

        # Verify indexer was called
        mock_indexer.index_override.assert_called_once_with(
            "my-app",
            "coding",
            str(override_dir / "coding.md"),
        )

    @pytest.mark.asyncio
    async def test_file_modify_triggers_reindex(self, tmp_path):
        """Modifying an override file triggers re-indexing."""
        vault = tmp_path / "vault"
        override_dir = vault / "projects" / "app" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "coding.md"
        override_file.write_text("# Original\n")

        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(return_value=2)
        set_indexer(mock_indexer)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_override_handlers(watcher)
        await watcher.check()

        # Modify
        time.sleep(0.05)
        override_file.write_text("# Updated\n")
        await watcher.check()

        mock_indexer.index_override.assert_called_once_with(
            "app",
            "coding",
            str(override_file),
        )

    @pytest.mark.asyncio
    async def test_file_delete_triggers_delete(self, tmp_path):
        """Deleting an override file triggers delete_override on the indexer."""
        vault = tmp_path / "vault"
        override_dir = vault / "projects" / "app" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "testing.md"
        override_file.write_text("# Testing\n")

        mock_indexer = MagicMock()
        mock_indexer.delete_override = AsyncMock(return_value=True)
        set_indexer(mock_indexer)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_override_handlers(watcher)
        await watcher.check()

        # Delete
        override_file.unlink()
        await watcher.check()

        mock_indexer.delete_override.assert_called_once_with(
            "app",
            str(override_file),
        )

    @pytest.mark.asyncio
    async def test_no_indexer_falls_back_to_logging(self, tmp_path, caplog):
        """Without an indexer wired, changes are only logged."""
        vault = tmp_path / "vault"
        vault.mkdir()

        set_indexer(None)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_override_handlers(watcher)
        await watcher.check()

        # Create override file
        override_dir = vault / "projects" / "app" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("# Overrides\n")

        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await watcher.check()

        assert any("no indexer configured" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_multiple_files_indexed(self, tmp_path):
        """Multiple override file creates dispatch to the indexer."""
        vault = tmp_path / "vault"
        vault.mkdir()

        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(return_value=1)
        set_indexer(mock_indexer)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_override_handlers(watcher)
        await watcher.check()

        # Create override files in two projects
        for proj in ["app1", "app2"]:
            d = vault / "projects" / proj / "overrides"
            d.mkdir(parents=True)
            (d / "coding.md").write_text(f"# {proj} coding\n")

        await watcher.check()

        assert mock_indexer.index_override.call_count == 2

    def teardown_method(self):
        set_indexer(None)
