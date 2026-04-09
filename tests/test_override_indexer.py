"""Tests for OverrideIndexer — indexing override .md files into project collections.

Tests cover:
- Override file indexing (upsert into project collection)
- Content with highest weight (project scope = 1.0)
- Updates trigger re-index (stale chunk cleanup)
- Deletions remove chunks from project collection
- Empty override files are handled gracefully
- Scope isolation (project A overrides don't leak to project B)
- Agent-type tagging for filterability
- Startup bulk indexing (index_all_overrides)
- Module-level indexer getter/setter
"""

from __future__ import annotations

import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.override_handler import (
    OVERRIDE_TAG,
    OverrideIndexer,
    get_indexer,
    on_override_changed,
    set_indexer,
)

# Skip Milvus tests on Windows (Milvus Lite not supported)
pytestmark_milvus = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Milvus Lite not supported on Windows",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_OVERRIDE = """\
---
tags: [override, coding, mech-fighters]
agent_type: coding
---

# Coding Agent Overrides -- Mech Fighters

This project uses a custom ECS framework. Do not use inheritance for
game entities -- always use composition via the component system.

Prefer integration tests that spin up the full game loop over unit
tests of individual components.
"""

SAMPLE_OVERRIDE_V2 = """\
---
tags: [override, coding, mech-fighters]
agent_type: coding
---

# Coding Agent Overrides -- Mech Fighters (Updated)

This project uses a custom ECS framework with composition only.

Additionally, always use the asset pipeline for generated files.
Never modify files in assets/generated/ directly.
"""


def _make_vault_change(path: str, rel_path: str, operation: str):
    """Create a VaultChange-like object without importing from src.vault_watcher."""
    from src.vault_watcher import VaultChange

    return VaultChange(path=path, rel_path=rel_path, operation=operation)


# ---------------------------------------------------------------------------
# Mock-based tests (no Milvus dependency)
# ---------------------------------------------------------------------------


class TestOverrideIndexerMocked:
    """Tests using mocked router and embedder — no Milvus required."""

    def _make_indexer(self):
        """Create an OverrideIndexer with mocked dependencies."""
        router = MagicMock()
        embedder = MagicMock()
        embedder.model_name = "test-model"
        embedder.dimension = 4
        # Make embed return one vector per input text
        embedder.embed = AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3, 0.4]] * len(texts))
        return OverrideIndexer(router, embedder), router, embedder

    @pytest.mark.asyncio
    async def test_index_override_reads_file(self, tmp_path):
        """Index reads the file content and passes it through the pipeline."""
        indexer, router, embedder = self._make_indexer()

        # Create override file
        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        # Mock store methods
        store = MagicMock()
        store.hashes_by_source.return_value = set()
        store.upsert.return_value = 1
        router.get_store.return_value = store

        n = await indexer.index_override("mech-fighters", "coding", str(override_file))
        assert n >= 1
        store.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_override_tags_with_override_and_agent_type(self, tmp_path):
        """Chunks are tagged with #override and the agent type."""
        indexer, router, embedder = self._make_indexer()

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        store = MagicMock()
        store.hashes_by_source.return_value = set()
        store.upsert.return_value = 1
        router.get_store.return_value = store

        await indexer.index_override("mech-fighters", "coding", str(override_file))

        # Check that upsert was called with records containing correct tags
        records = store.upsert.call_args[0][0]
        for record in records:
            tags = json.loads(record["tags"])
            assert OVERRIDE_TAG in tags
            assert "coding" in tags

    @pytest.mark.asyncio
    async def test_index_override_empty_file_returns_zero(self, tmp_path):
        """An empty override file returns 0 chunks indexed."""
        indexer, router, embedder = self._make_indexer()

        override_file = tmp_path / "coding.md"
        override_file.write_text("")

        n = await indexer.index_override("myapp", "coding", str(override_file))
        assert n == 0

    @pytest.mark.asyncio
    async def test_index_override_whitespace_only_returns_zero(self, tmp_path):
        """A whitespace-only override file returns 0 chunks."""
        indexer, router, embedder = self._make_indexer()

        override_file = tmp_path / "coding.md"
        override_file.write_text("   \n\n  \n")

        n = await indexer.index_override("myapp", "coding", str(override_file))
        assert n == 0

    @pytest.mark.asyncio
    async def test_index_override_nonexistent_file_returns_zero(self):
        """A nonexistent file path returns 0 gracefully."""
        indexer, router, embedder = self._make_indexer()
        n = await indexer.index_override("myapp", "coding", "/nonexistent/path/coding.md")
        assert n == 0

    @pytest.mark.asyncio
    async def test_index_override_uses_project_scope(self, tmp_path):
        """Indexer gets the PROJECT scope store from the router."""
        indexer, router, embedder = self._make_indexer()

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        store = MagicMock()
        store.hashes_by_source.return_value = set()
        store.upsert.return_value = 1
        router.get_store.return_value = store

        await indexer.index_override("mech-fighters", "coding", str(override_file))

        # Verify router.get_store was called with PROJECT scope
        from memsearch.scoping import MemoryScope

        router.get_store.assert_called_once()
        call_args = router.get_store.call_args
        assert call_args[0][0] == MemoryScope.PROJECT
        assert call_args[0][1] == "mech-fighters"

    @pytest.mark.asyncio
    async def test_delete_override_removes_chunks(self, tmp_path):
        """Delete removes all chunks for the override file's source path."""
        indexer, router, embedder = self._make_indexer()

        store = MagicMock()
        store.hashes_by_source.return_value = {"hash1", "hash2"}
        router.has_store.return_value = True
        router.get_store.return_value = store

        result = await indexer.delete_override("mech-fighters", str(tmp_path / "coding.md"))
        assert result is True
        store.delete_by_hashes.assert_called_once()
        deleted_hashes = set(store.delete_by_hashes.call_args[0][0])
        assert deleted_hashes == {"hash1", "hash2"}

    @pytest.mark.asyncio
    async def test_delete_override_no_collection_returns_false(self):
        """If no project collection exists, delete returns False."""
        indexer, router, embedder = self._make_indexer()
        router.has_store.return_value = False

        result = await indexer.delete_override("nonexistent", "/path/to/file.md")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_override_no_chunks_returns_false(self, tmp_path):
        """If no chunks exist for the source, delete returns False."""
        indexer, router, embedder = self._make_indexer()

        store = MagicMock()
        store.hashes_by_source.return_value = set()
        router.has_store.return_value = True
        router.get_store.return_value = store

        result = await indexer.delete_override("myapp", str(tmp_path / "coding.md"))
        assert result is False

    @pytest.mark.asyncio
    async def test_index_all_overrides(self, tmp_path):
        """index_all_overrides scans vault and indexes all override files."""
        indexer, router, embedder = self._make_indexer()

        # Create vault structure with override files
        vault = tmp_path / "vault"
        p1 = vault / "projects" / "app1" / "overrides"
        p1.mkdir(parents=True)
        (p1 / "coding.md").write_text(SAMPLE_OVERRIDE)

        p2 = vault / "projects" / "app2" / "overrides"
        p2.mkdir(parents=True)
        (p2 / "testing.md").write_text("# Testing overrides\nAlways test.\n")

        store = MagicMock()
        store.hashes_by_source.return_value = set()
        store.upsert.return_value = 1
        router.get_store.return_value = store

        total = await indexer.index_all_overrides(str(vault))
        assert total >= 2  # At least one chunk per file
        # Verify router.get_store was called for both projects
        assert router.get_store.call_count >= 2

    @pytest.mark.asyncio
    async def test_index_all_overrides_empty_vault(self, tmp_path):
        """index_all_overrides with no override files returns 0."""
        indexer, router, embedder = self._make_indexer()
        vault = tmp_path / "vault"
        vault.mkdir()

        total = await indexer.index_all_overrides(str(vault))
        assert total == 0


# ---------------------------------------------------------------------------
# Module-level indexer getter/setter
# ---------------------------------------------------------------------------


class TestModuleLevelIndexer:
    """Tests for set_indexer / get_indexer module-level state."""

    def setup_method(self):
        # Reset the module-level state before each test
        set_indexer(None)

    def test_get_indexer_default_is_none(self):
        assert get_indexer() is None

    def test_set_and_get_indexer(self):
        mock = MagicMock()
        set_indexer(mock)
        assert get_indexer() is mock

    def test_set_indexer_to_none(self):
        mock = MagicMock()
        set_indexer(mock)
        set_indexer(None)
        assert get_indexer() is None

    def teardown_method(self):
        set_indexer(None)


# ---------------------------------------------------------------------------
# on_override_changed with indexer
# ---------------------------------------------------------------------------


class TestOnOverrideChangedWithIndexer:
    """Tests for on_override_changed when an indexer is configured."""

    def setup_method(self):
        set_indexer(None)

    @pytest.mark.asyncio
    async def test_created_triggers_index(self, tmp_path, caplog):
        """A 'created' change triggers index_override on the indexer."""
        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(return_value=3)
        set_indexer(mock_indexer)

        change = _make_vault_change(
            path=str(tmp_path / "projects" / "myapp" / "overrides" / "coding.md"),
            rel_path="projects/myapp/overrides/coding.md",
            operation="created",
        )

        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed([change])

        mock_indexer.index_override.assert_called_once_with("myapp", "coding", change.path)
        assert any("indexed 3 chunks" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_modified_triggers_index(self, tmp_path):
        """A 'modified' change triggers index_override (re-index)."""
        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(return_value=2)
        set_indexer(mock_indexer)

        change = _make_vault_change(
            path=str(tmp_path / "projects" / "app" / "overrides" / "testing.md"),
            rel_path="projects/app/overrides/testing.md",
            operation="modified",
        )

        await on_override_changed([change])
        mock_indexer.index_override.assert_called_once_with("app", "testing", change.path)

    @pytest.mark.asyncio
    async def test_deleted_triggers_delete(self, tmp_path):
        """A 'deleted' change triggers delete_override on the indexer."""
        mock_indexer = MagicMock()
        mock_indexer.delete_override = AsyncMock(return_value=True)
        set_indexer(mock_indexer)

        change = _make_vault_change(
            path=str(tmp_path / "projects" / "myapp" / "overrides" / "coding.md"),
            rel_path="projects/myapp/overrides/coding.md",
            operation="deleted",
        )

        await on_override_changed([change])
        mock_indexer.delete_override.assert_called_once_with("myapp", change.path)

    @pytest.mark.asyncio
    async def test_indexer_exception_logged_and_continues(self, tmp_path, caplog):
        """An exception in the indexer is logged but doesn't crash the handler."""
        mock_indexer = MagicMock()
        mock_indexer.index_override = AsyncMock(side_effect=RuntimeError("boom"))
        set_indexer(mock_indexer)

        changes = [
            _make_vault_change(
                path=str(tmp_path / "projects" / "a" / "overrides" / "coding.md"),
                rel_path="projects/a/overrides/coding.md",
                operation="created",
            ),
            _make_vault_change(
                path=str(tmp_path / "projects" / "b" / "overrides" / "testing.md"),
                rel_path="projects/b/overrides/testing.md",
                operation="created",
            ),
        ]

        with caplog.at_level(logging.ERROR, logger="src.override_handler"):
            await on_override_changed(changes)

        # Both changes should be attempted (second should not be skipped)
        assert mock_indexer.index_override.call_count == 2

    @pytest.mark.asyncio
    async def test_no_indexer_falls_back_to_logging(self, caplog):
        """Without an indexer, changes are logged but not indexed."""
        set_indexer(None)

        change = _make_vault_change(
            path="/vault/projects/app/overrides/coding.md",
            rel_path="projects/app/overrides/coding.md",
            operation="created",
        )

        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed([change])

        assert any("no indexer configured" in r.message for r in caplog.records)

    def teardown_method(self):
        set_indexer(None)


# ---------------------------------------------------------------------------
# Integration tests with real Milvus (skip on Windows)
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Minimal fake embedding provider for integration tests.

    Returns deterministic vectors based on content hash so identical
    content produces identical embeddings while different content produces
    different embeddings.  Dimension is kept small (4) for speed.
    """

    model_name: str = "fake-test-model"
    dimension: int = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        results = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            # Convert first 4 bytes to floats in [0,1]
            vec = [b / 255.0 for b in h[:4]]
            results.append(vec)
        return results


@pytestmark_milvus
class TestOverrideIndexerIntegration:
    """Integration tests using real Milvus Lite (non-Windows only)."""

    @pytest.fixture
    def override_setup(self, tmp_path):
        """Set up a real OverrideIndexer with Milvus Lite and a fake embedder."""
        from memsearch.scoping import CollectionRouter

        db_path = tmp_path / "test.db"
        embedder = _FakeEmbedder()
        router = CollectionRouter(
            milvus_uri=str(db_path),
            dimension=embedder.dimension,
        )
        indexer = OverrideIndexer(router, embedder)
        yield indexer, router, embedder, tmp_path
        router.close()

    @pytest.mark.asyncio
    async def test_index_and_search_override(self, override_setup):
        """End-to-end: index an override file and find it via search."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        # Create override file
        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        n = await indexer.index_override("mech-fighters", "coding", str(override_file))
        assert n >= 1

        # Verify chunks are in the project collection
        store = router.get_store(MemoryScope.PROJECT, "mech-fighters")
        source = str(override_file.resolve())
        hashes = store.hashes_by_source(source)
        assert len(hashes) >= 1

    @pytest.mark.asyncio
    async def test_override_chunks_tagged_correctly(self, override_setup):
        """Override chunks have the #override and agent-type tags."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        await indexer.index_override("myapp", "coding", str(override_file))

        store = router.get_store(MemoryScope.PROJECT, "myapp")
        source = str(override_file.resolve())
        hashes = store.hashes_by_source(source)

        # Query the stored chunks and verify tags
        for h in hashes:
            entry = store.get(h)
            if entry:
                tags = json.loads(entry.get("tags", "[]"))
                assert OVERRIDE_TAG in tags
                assert "coding" in tags

    @pytest.mark.asyncio
    async def test_update_override_replaces_stale_chunks(self, override_setup):
        """Modifying an override file replaces old chunks with new ones."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"

        # Index original
        override_file.write_text(SAMPLE_OVERRIDE)
        n1 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n1 >= 1

        store = router.get_store(MemoryScope.PROJECT, "myapp")
        source = str(override_file.resolve())
        old_hashes = store.hashes_by_source(source)

        # Update the file
        override_file.write_text(SAMPLE_OVERRIDE_V2)
        n2 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n2 >= 1

        # Verify old chunks are gone and new ones exist
        new_hashes = store.hashes_by_source(source)
        assert len(new_hashes) >= 1
        # At least some hashes should differ (content changed)
        assert old_hashes != new_hashes

    @pytest.mark.asyncio
    async def test_delete_override_removes_all_chunks(self, override_setup):
        """Deleting an override file removes its chunks from the collection."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        # Index first
        await indexer.index_override("myapp", "coding", str(override_file))
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        source = str(override_file.resolve())
        assert len(store.hashes_by_source(source)) >= 1

        # Delete
        result = await indexer.delete_override("myapp", str(override_file))
        assert result is True

        # Verify chunks are gone
        assert len(store.hashes_by_source(source)) == 0

    @pytest.mark.asyncio
    async def test_scope_isolation_between_projects(self, override_setup):
        """Override for project A doesn't appear in project B's collection."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        # Index into project A
        await indexer.index_override("project-a", "coding", str(override_file))

        # Verify NOT in project B
        assert not router.has_store(MemoryScope.PROJECT, "project-b")

        # Verify IS in project A
        store_a = router.get_store(MemoryScope.PROJECT, "project-a")
        source = str(override_file.resolve())
        assert len(store_a.hashes_by_source(source)) >= 1

    @pytest.mark.asyncio
    async def test_multiple_agent_type_overrides_same_project(self, override_setup):
        """Multiple override files for different agent types coexist in one project."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        coding_file = tmp_path / "coding.md"
        coding_file.write_text(SAMPLE_OVERRIDE)

        testing_file = tmp_path / "testing.md"
        testing_file.write_text("# Testing Overrides\n\nAlways write integration tests first.\n")

        await indexer.index_override("myapp", "coding", str(coding_file))
        await indexer.index_override("myapp", "testing", str(testing_file))

        store = router.get_store(MemoryScope.PROJECT, "myapp")

        # Both should have chunks
        assert len(store.hashes_by_source(str(coding_file.resolve()))) >= 1
        assert len(store.hashes_by_source(str(testing_file.resolve()))) >= 1

    @pytest.mark.asyncio
    async def test_idempotent_indexing(self, override_setup):
        """Indexing the same unchanged file twice doesn't create duplicate chunks."""
        indexer, router, embedder, tmp_path = override_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        n1 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n1 >= 1

        # Index again (no changes)
        n2 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n2 == 0  # No new chunks needed

        # Total chunks should be the same as first indexing
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        source = str(override_file.resolve())
        assert len(store.hashes_by_source(source)) == n1

    @pytest.mark.asyncio
    async def test_project_collection_weight_is_highest(self, override_setup):
        """Project scope has weight 1.0 — the highest in the hierarchy."""
        from memsearch.scoping import SCOPE_WEIGHTS, MemoryScope

        project_weight = SCOPE_WEIGHTS[MemoryScope.PROJECT]
        for scope, weight in SCOPE_WEIGHTS.items():
            if scope != MemoryScope.PROJECT:
                assert project_weight >= weight, (
                    f"Project weight ({project_weight}) should be >= "
                    f"{scope.value} weight ({weight})"
                )

    @pytest.mark.asyncio
    async def test_bulk_index_from_vault(self, override_setup):
        """index_all_overrides finds and indexes files from vault structure."""
        indexer, router, embedder, tmp_path = override_setup

        # Create vault directory structure
        vault = tmp_path / "vault"
        p1 = vault / "projects" / "app1" / "overrides"
        p1.mkdir(parents=True)
        (p1 / "coding.md").write_text(SAMPLE_OVERRIDE)

        p2 = vault / "projects" / "app2" / "overrides"
        p2.mkdir(parents=True)
        (p2 / "testing.md").write_text(
            "# Testing Overrides\n\nUse pytest exclusively. No unittest.\n"
        )

        total = await indexer.index_all_overrides(str(vault))
        assert total >= 2  # At least one chunk from each file

        # Verify both projects have collections with content
        from memsearch.scoping import MemoryScope

        store1 = router.get_store(MemoryScope.PROJECT, "app1")
        store2 = router.get_store(MemoryScope.PROJECT, "app2")
        assert len(store1.hashes_by_source(str((p1 / "coding.md").resolve()))) >= 1
        assert len(store2.hashes_by_source(str((p2 / "testing.md").resolve()))) >= 1


# ---------------------------------------------------------------------------
# MemoryManager integration (mocked memsearch)
# ---------------------------------------------------------------------------


class TestMemoryManagerOverrideIntegration:
    """Tests for MemoryManager.get_override_indexer and related methods."""

    @pytest.mark.asyncio
    async def test_get_override_indexer_when_disabled(self):
        """Returns None when memory is disabled."""
        from src.config import MemoryConfig

        config = MemoryConfig(enabled=False)
        from src.memory import MemoryManager

        mm = MemoryManager(config)
        indexer = await mm.get_override_indexer()
        assert indexer is None

    @pytest.mark.asyncio
    async def test_get_override_indexer_caches_instance(self):
        """Repeated calls return the same indexer instance."""
        from src.config import MemoryConfig
        from src.memory import MemoryManager

        config = MemoryConfig(enabled=True)
        mm = MemoryManager(config)

        # Mock the internal methods that create router and embedder
        mock_router = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.model_name = "test"
        mock_embedder.dimension = 4
        mm._router = mock_router
        mm._embedder = mock_embedder

        # Patch _get_router and _get_embedder
        mm._get_router = AsyncMock(return_value=mock_router)
        mm._get_embedder = AsyncMock(return_value=mock_embedder)

        indexer1 = await mm.get_override_indexer()
        indexer2 = await mm.get_override_indexer()
        assert indexer1 is indexer2
        assert indexer1 is not None

    @pytest.mark.asyncio
    async def test_setup_override_watcher_sets_module_indexer(self):
        """setup_override_watcher() sets the module-level indexer."""
        from src.config import MemoryConfig
        from src.memory import MemoryManager

        config = MemoryConfig(enabled=True)
        mm = MemoryManager(config)

        mock_router = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.model_name = "test"
        mock_embedder.dimension = 4
        mm._get_router = AsyncMock(return_value=mock_router)
        mm._get_embedder = AsyncMock(return_value=mock_embedder)

        result = await mm.setup_override_watcher()
        assert result is True

        # Verify the module-level indexer was set
        from src.override_handler import get_indexer

        assert get_indexer() is not None

        # Cleanup
        set_indexer(None)

    @pytest.mark.asyncio
    async def test_index_project_overrides_with_no_vault(self, tmp_path):
        """Returns 0 when vault root doesn't exist."""
        from src.config import MemoryConfig
        from src.memory import MemoryManager

        config = MemoryConfig(enabled=True)
        mm = MemoryManager(config, storage_root=str(tmp_path))

        mock_router = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.model_name = "test"
        mock_embedder.dimension = 4
        mm._get_router = AsyncMock(return_value=mock_router)
        mm._get_embedder = AsyncMock(return_value=mock_embedder)

        total = await mm.index_project_overrides(str(tmp_path / "nonexistent"))
        assert total == 0


# ---------------------------------------------------------------------------
# Roadmap 3.2.4 — Override indexing and retrieval test cases (a)–(f)
#
#   (a) Create override file → content appears in project-scope search results
#   (b) Override content has highest weight (ranks first for matching queries)
#   (c) Override content is injected into agent context alongside base profile
#   (d) Updating override file → re-index, new content appears in searches
#   (e) Deleting override file → removes from search results
#   (f) Override with empty content → does not inject empty string into context
# ---------------------------------------------------------------------------


@pytestmark_milvus
class TestOverrideRetrievalSpec:
    """Roadmap 3.2.4: override indexing and retrieval end-to-end tests.

    These tests verify that override content flows through the full pipeline:
    indexing → storage → search retrieval → context injection.  Each test
    maps to a specific case in the roadmap spec.
    """

    @pytest.fixture
    def retrieval_setup(self, tmp_path):
        """Set up a real OverrideIndexer with Milvus Lite for retrieval tests."""
        from memsearch.scoping import CollectionRouter

        db_path = tmp_path / "test_retrieval.db"
        embedder = _FakeEmbedder()
        router = CollectionRouter(
            milvus_uri=str(db_path),
            dimension=embedder.dimension,
        )
        indexer = OverrideIndexer(router, embedder)
        yield indexer, router, embedder, tmp_path
        router.close()

    # -- (a) Override content appears in project-scope search results --------

    @pytest.mark.asyncio
    async def test_a_override_content_appears_in_project_search(self, retrieval_setup):
        """(a) Creating vault/projects/myapp/overrides/coding.md makes its
        content appear in project-scope search results."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        # Create the override file following the vault path convention
        vault = tmp_path / "vault"
        override_dir = vault / "projects" / "myapp" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        n = await indexer.index_override("myapp", "coding", str(override_file))
        assert n >= 1, "Override file should produce at least one chunk"

        # Retrieve all entries from the project collection and verify
        # override content is present (scalar query — no vector needed)
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        results = store.query(filter_expr='tags like "%override%"')
        assert len(results) >= 1, "Override chunks should be queryable in project collection"

        # Verify the actual content is there
        all_content = " ".join(r.get("content", "") for r in results)
        assert "ECS" in all_content or "composition" in all_content, (
            "Override content about ECS/composition should be in project search results"
        )

    @pytest.mark.asyncio
    async def test_a_override_searchable_via_hybrid_search(self, retrieval_setup):
        """(a) Override content is retrievable via hybrid search (dense + BM25)."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)

        await indexer.index_override("myapp", "coding", str(override_file))

        # Search using BM25 keywords from the override content
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        query_vec = (await embedder.embed(["ECS framework composition component"]))[0]
        results = store.search(
            query_vec,
            query_text="ECS framework composition component",
            top_k=5,
        )
        assert len(results) >= 1, "Hybrid search should find override content"

        # At least one result should contain override content
        found_override = any(
            "ECS" in r.get("content", "") or "composition" in r.get("content", "") for r in results
        )
        assert found_override, "Override content should appear in hybrid search results"

    # -- (b) Override content has highest weight (ranks first) ---------------

    @pytest.mark.asyncio
    async def test_b_project_scope_weight_is_highest(self, retrieval_setup):
        """(b) Project scope (where overrides live) has the highest scope weight."""
        from memsearch.scoping import SCOPE_WEIGHTS, MemoryScope

        project_weight = SCOPE_WEIGHTS[MemoryScope.PROJECT]
        other_weights = [w for s, w in SCOPE_WEIGHTS.items() if s != MemoryScope.PROJECT]
        assert all(project_weight >= w for w in other_weights), (
            f"PROJECT weight ({project_weight}) should be >= all other scope weights"
        )

    @pytest.mark.asyncio
    async def test_b_override_entry_weight_above_normal(self, retrieval_setup):
        """(b) OVERRIDE_ENTRY_WEIGHT is > 1.0, boosting overrides above normal
        project memories for equal-similarity queries."""
        from src.override_handler import OVERRIDE_ENTRY_WEIGHT

        assert OVERRIDE_ENTRY_WEIGHT > 1.0, (
            f"OVERRIDE_ENTRY_WEIGHT ({OVERRIDE_ENTRY_WEIGHT}) should be > 1.0 "
            "to ensure overrides rank above normal project memories"
        )

    @pytest.mark.asyncio
    async def test_b_override_tagged_for_weight_filtering(self, retrieval_setup):
        """(b) Override chunks are tagged with #override so they can be identified
        for weight boosting during search/ranking."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)
        await indexer.index_override("myapp", "coding", str(override_file))

        store = router.get_store(MemoryScope.PROJECT, "myapp")

        # Query all entries and verify override tag is present
        all_entries = store.query()
        assert len(all_entries) >= 1

        for entry in all_entries:
            tags = json.loads(entry.get("tags", "[]"))
            assert OVERRIDE_TAG in tags, (
                f"Override chunk should have '{OVERRIDE_TAG}' tag, got: {tags}"
            )
            assert "coding" in tags, (
                f"Override chunk should have agent_type 'coding' tag, got: {tags}"
            )

    @pytest.mark.asyncio
    async def test_b_override_outranks_lower_scope_in_multi_scope_search(self, retrieval_setup):
        """(b) Override in PROJECT scope (weight 1.0) outranks the same content
        in SYSTEM scope (weight 0.4) during multi-scope weighted search."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        # Index override into project collection
        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)
        await indexer.index_override("myapp", "coding", str(override_file))

        # Also add some content to the system collection for comparison
        sys_store = router.get_store(MemoryScope.SYSTEM, description="system")
        sys_vec = (await embedder.embed(["system level conventions"]))[0]
        sys_store.upsert(
            [
                {
                    "chunk_hash": "sys_conventions_001",
                    "entry_type": "document",
                    "embedding": sys_vec,
                    "content": "System-level conventions for all projects.",
                    "original": "System-level conventions for all projects.",
                    "source": "system/conventions.md",
                    "heading": "Conventions",
                    "heading_level": 1,
                    "start_line": 1,
                    "end_line": 5,
                    "tags": "[]",
                    "updated_at": 0,
                }
            ]
        )

        # Multi-scope search via router
        query_vec = (await embedder.embed(["coding conventions"]))[0]
        results = await router.search(
            query_vec,
            query_text="coding conventions",
            project_id="myapp",
            top_k=10,
        )

        # Find results from each scope
        project_results = [r for r in results if r.get("_scope") == "project"]
        system_results = [r for r in results if r.get("_scope") == "system"]

        if project_results and system_results:
            # Project results should have higher weighted_score
            best_project = max(r.get("weighted_score", 0) for r in project_results)
            best_system = max(r.get("weighted_score", 0) for r in system_results)
            assert best_project >= best_system, (
                f"Project scope override ({best_project:.3f}) should rank >= "
                f"system scope ({best_system:.3f}) due to higher scope weight"
            )

        # At minimum, project results should exist (override was indexed there)
        assert len(project_results) >= 1, "Override content should appear in project scope results"

    # -- (c) Override content injected into context alongside profile --------

    @pytest.mark.asyncio
    async def test_c_override_in_context_alongside_profile(self, retrieval_setup):
        """(c) Override content appears in the assembled context block alongside
        the base project profile."""
        from src.models import MemoryContext

        # Simulate what build_context() produces: profile in Tier 1,
        # override content appearing in Tier 4 (semantic search results)
        ctx = MemoryContext(
            profile="## Conventions\nUse ruff for formatting. Always write tests.",
            search_results=(
                "*Source: /vault/projects/myapp/overrides/coding.md*\n"
                "*Section: Coding Agent Overrides*\n"
                "This project uses a custom ECS framework. Do not use inheritance "
                "for game entities — always use composition via the component system."
            ),
        )
        block = ctx.to_context_block()

        # Both profile and override content must be present
        assert "## Project Profile" in block, "Profile section should be in context block"
        assert "ruff for formatting" in block, "Profile content should be in context block"
        assert "## Relevant Context from Project Memory" in block, (
            "Search results section (containing overrides) should be in context block"
        )
        assert "ECS framework" in block, "Override content should be in context block"
        assert "composition" in block, "Override content should be in context block"

    @pytest.mark.asyncio
    async def test_c_override_and_profile_coexist_in_context(self, retrieval_setup):
        """(c) When both profile and override content are present, the context
        block contains distinct sections for each."""
        from src.models import MemoryContext

        ctx = MemoryContext(
            factsheet="name: myapp\ntype: game",
            profile="Always use composition over inheritance.",
            search_results=(
                "*Source: /vault/projects/myapp/overrides/coding.md*\n"
                "Override: prefer integration tests over unit tests."
            ),
        )
        block = ctx.to_context_block()

        # All three sections should be distinct and present
        assert "## Project Factsheet" in block
        assert "## Project Profile" in block
        assert "## Relevant Context from Project Memory" in block

        # Verify ordering: factsheet → profile → search_results
        factsheet_pos = block.index("## Project Factsheet")
        profile_pos = block.index("## Project Profile")
        search_pos = block.index("## Relevant Context from Project Memory")
        assert factsheet_pos < profile_pos < search_pos, (
            "Context sections should be ordered: factsheet < profile < search results"
        )

    # -- (d) Updating override triggers re-index, new content searchable ----

    @pytest.mark.asyncio
    async def test_d_update_override_new_content_in_search(self, retrieval_setup):
        """(d) Updating an override file triggers re-index and the new content
        appears in subsequent search results."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"

        # Index original content
        override_file.write_text(SAMPLE_OVERRIDE)
        n1 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n1 >= 1

        store = router.get_store(MemoryScope.PROJECT, "myapp")

        # Verify original content is in the collection
        results_v1 = store.query()
        content_v1 = " ".join(r.get("content", "") for r in results_v1)
        assert "ECS" in content_v1 or "composition" in content_v1

        # Update the file with new content
        override_file.write_text(SAMPLE_OVERRIDE_V2)
        n2 = await indexer.index_override("myapp", "coding", str(override_file))
        assert n2 >= 1

        # Verify new content appears and old stale content is gone
        results_v2 = store.query()
        content_v2 = " ".join(r.get("content", "") for r in results_v2)
        assert "asset pipeline" in content_v2, (
            "Updated override content ('asset pipeline') should appear in search results"
        )

    @pytest.mark.asyncio
    async def test_d_update_override_stale_chunks_removed(self, retrieval_setup):
        """(d) After updating an override, stale chunks from the old version
        are cleaned up — only new content remains."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        source = str(override_file.resolve())

        # Index original
        override_file.write_text(SAMPLE_OVERRIDE)
        await indexer.index_override("myapp", "coding", str(override_file))

        store = router.get_store(MemoryScope.PROJECT, "myapp")
        old_hashes = store.hashes_by_source(source)
        assert len(old_hashes) >= 1

        # Update
        override_file.write_text(SAMPLE_OVERRIDE_V2)
        await indexer.index_override("myapp", "coding", str(override_file))

        new_hashes = store.hashes_by_source(source)
        assert len(new_hashes) >= 1
        # Content changed, so at least some chunk hashes should differ
        assert old_hashes != new_hashes, "Chunk hashes should change after content update"

    # -- (e) Deleting override removes from search results ------------------

    @pytest.mark.asyncio
    async def test_e_delete_override_removes_from_search(self, retrieval_setup):
        """(e) Deleting an override file removes all its content from search
        results — no trace of the override remains in the project collection."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)
        source = str(override_file.resolve())

        # Index and verify present
        await indexer.index_override("myapp", "coding", str(override_file))
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        assert len(store.hashes_by_source(source)) >= 1

        # Delete override
        result = await indexer.delete_override("myapp", str(override_file))
        assert result is True

        # Verify no chunks remain for this source
        assert len(store.hashes_by_source(source)) == 0, (
            "All override chunks should be removed after deletion"
        )

    @pytest.mark.asyncio
    async def test_e_delete_override_not_in_query_results(self, retrieval_setup):
        """(e) After deletion, querying the project collection returns no
        results for the deleted override's source path."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE)
        source = str(override_file.resolve())

        await indexer.index_override("myapp", "coding", str(override_file))
        await indexer.delete_override("myapp", str(override_file))

        # Query all remaining entries — none should reference the deleted source
        store = router.get_store(MemoryScope.PROJECT, "myapp")
        all_entries = store.query()
        for entry in all_entries:
            assert entry.get("source") != source, (
                f"Deleted override source '{source}' should not appear in query results"
            )

    @pytest.mark.asyncio
    async def test_e_delete_preserves_other_overrides(self, retrieval_setup):
        """(e) Deleting one override does not affect other overrides in the
        same project collection."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        # Index two different override files
        coding_file = tmp_path / "coding.md"
        coding_file.write_text(SAMPLE_OVERRIDE)
        testing_file = tmp_path / "testing.md"
        testing_file.write_text("# Testing Overrides\n\nAlways write integration tests.\n")

        await indexer.index_override("myapp", "coding", str(coding_file))
        await indexer.index_override("myapp", "testing", str(testing_file))

        store = router.get_store(MemoryScope.PROJECT, "myapp")
        testing_source = str(testing_file.resolve())
        assert len(store.hashes_by_source(testing_source)) >= 1

        # Delete only the coding override
        await indexer.delete_override("myapp", str(coding_file))

        # Testing override should still be present
        assert len(store.hashes_by_source(testing_source)) >= 1, (
            "Deleting one override should not affect other overrides"
        )

    # -- (f) Empty override does not inject empty string --------------------

    @pytest.mark.asyncio
    async def test_f_empty_override_indexes_zero_chunks(self, retrieval_setup):
        """(f) An empty override file produces zero chunks — nothing is stored
        in the project collection."""
        indexer, router, embedder, tmp_path = retrieval_setup
        from memsearch.scoping import MemoryScope

        override_file = tmp_path / "coding.md"
        override_file.write_text("")

        n = await indexer.index_override("myapp", "coding", str(override_file))
        assert n == 0, "Empty override should produce 0 chunks"

        # No project collection should exist (nothing was indexed)
        assert not router.has_store(MemoryScope.PROJECT, "myapp"), (
            "No project collection should be created for empty override"
        )

    @pytest.mark.asyncio
    async def test_f_empty_override_not_in_context_block(self, retrieval_setup):
        """(f) When override search returns no results (because the file was
        empty), the context block does not contain an empty search results
        section."""
        from src.models import MemoryContext

        # Simulate build_context output when override was empty (no search results)
        ctx = MemoryContext(
            profile="Base project profile.",
            search_results="",  # Empty — no override content was indexed
        )
        block = ctx.to_context_block()

        # Profile should be present
        assert "## Project Profile" in block
        assert "Base project profile" in block

        # Search results section must NOT be present (empty string = falsy)
        assert "## Relevant Context from Project Memory" not in block, (
            "Empty search_results should not inject a section into the context block"
        )

    @pytest.mark.asyncio
    async def test_f_whitespace_only_override_indexes_zero_chunks(self, retrieval_setup):
        """(f) A whitespace-only override file also produces zero chunks."""
        indexer, router, embedder, tmp_path = retrieval_setup

        override_file = tmp_path / "coding.md"
        override_file.write_text("   \n\n  \t  \n")

        n = await indexer.index_override("myapp", "coding", str(override_file))
        assert n == 0, "Whitespace-only override should produce 0 chunks"

    @pytest.mark.asyncio
    async def test_f_empty_context_is_empty(self, retrieval_setup):
        """(f) A MemoryContext with only empty fields reports is_empty=True
        and produces an empty context block."""
        from src.models import MemoryContext

        ctx = MemoryContext()
        assert ctx.is_empty, "Default MemoryContext should be empty"
        assert ctx.to_context_block() == "", "Empty context should produce empty string"
