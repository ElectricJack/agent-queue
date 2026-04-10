"""Tests for memory audit trail in frontmatter (spec §6).

Covers:
- ``created``, ``source_task``, ``source_playbook`` written on save
- ``last_retrieved`` and ``retrieval_count`` initialized to null/0 on save
- ``retrieval_count`` incremented and ``last_retrieved`` updated on search
- ``source_playbook`` flows through save_document, _write_vault_file,
  _update_vault_file, and the plugin's cmd_memory_save
- Dedup searches do NOT trigger retrieval tracking
- Plugin search results include retrieval_count and last_retrieved
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MemoryV2Service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.model_name = "test-model"
    embedder.dimension = 384
    embedder.embed = AsyncMock(return_value=[[0.1] * 384])
    return embedder


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.count.return_value = 0
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.search.return_value = []
    store.query.return_value = []
    store.upsert.return_value = 1
    return store


@pytest.fixture
def mock_router(mock_store):
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def service(mock_embedder, mock_router, tmp_path):
    svc = MemoryV2Service(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=str(tmp_path),
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# _write_vault_file: frontmatter fields
# ---------------------------------------------------------------------------


class TestWriteVaultFileFrontmatter:
    """_write_vault_file should produce spec §6 frontmatter."""

    def test_created_field(self, service, tmp_path):
        """created: <date> appears in frontmatter."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
        )
        text = path.read_text()
        assert re.search(r"^created: \d{4}-\d{2}-\d{2}$", text, re.MULTILINE)

    def test_source_task_field(self, service, tmp_path):
        """source_task appears when provided."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
            source_task="task-abc123",
        )
        text = path.read_text()
        assert "source_task: task-abc123" in text

    def test_source_playbook_field(self, service, tmp_path):
        """source_playbook appears when provided."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
            source_playbook="task-outcome",
        )
        text = path.read_text()
        assert "source_playbook: task-outcome" in text

    def test_source_playbook_absent_when_not_provided(self, service, tmp_path):
        """source_playbook omitted when not given."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
        )
        text = path.read_text()
        assert "source_playbook" not in text

    def test_last_retrieved_initialized_null(self, service, tmp_path):
        """last_retrieved starts as null."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
        )
        text = path.read_text()
        assert "last_retrieved: null" in text

    def test_retrieval_count_initialized_zero(self, service, tmp_path):
        """retrieval_count starts at 0."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
        )
        text = path.read_text()
        assert "retrieval_count: 0" in text

    def test_all_audit_fields_present(self, service, tmp_path):
        """All five spec §6 fields appear in frontmatter when applicable."""
        path = service._write_vault_file(
            tmp_path,
            content="Test insight",
            tags=["insight"],
            source_task="task-abc123",
            source_playbook="task-outcome",
        )
        text = path.read_text()
        # Extract frontmatter
        parts = text.split("---")
        assert len(parts) >= 3
        fm = parts[1]
        assert "created:" in fm
        assert "source_task: task-abc123" in fm
        assert "source_playbook: task-outcome" in fm
        assert "last_retrieved: null" in fm
        assert "retrieval_count: 0" in fm


# ---------------------------------------------------------------------------
# _update_vault_file: source_playbook handling
# ---------------------------------------------------------------------------


class TestUpdateVaultFileSourcePlaybook:
    """_update_vault_file should add source_playbook to existing files."""

    def _make_vault_file(self, tmp_path: Path) -> Path:
        """Create a minimal vault file for testing updates."""
        content = (
            "---\n"
            "tags: [\"insight\"]\n"
            "source_task: task-001\n"
            "created: 2026-04-07\n"
            "updated: 2026-04-07\n"
            "last_retrieved: null\n"
            "retrieval_count: 0\n"
            "---\n\n"
            "Test content\n"
        )
        filepath = tmp_path / "test.md"
        filepath.write_text(content)
        return filepath

    def test_adds_source_playbook(self, service, tmp_path):
        """source_playbook is inserted when not already present."""
        filepath = self._make_vault_file(tmp_path)
        service._update_vault_file(filepath, source_playbook="reflection")
        text = filepath.read_text()
        assert "source_playbook: reflection" in text

    def test_does_not_duplicate_source_playbook(self, service, tmp_path):
        """source_playbook is not added if already present."""
        content = (
            "---\n"
            "tags: [\"insight\"]\n"
            "source_task: task-001\n"
            "source_playbook: task-outcome\n"
            "created: 2026-04-07\n"
            "updated: 2026-04-07\n"
            "---\n\n"
            "Test content\n"
        )
        filepath = tmp_path / "test.md"
        filepath.write_text(content)
        service._update_vault_file(filepath, source_playbook="reflection")
        text = filepath.read_text()
        # Original value is preserved, not overwritten
        assert "source_playbook: task-outcome" in text
        assert text.count("source_playbook") == 1


# ---------------------------------------------------------------------------
# _update_vault_retrieval_stats
# ---------------------------------------------------------------------------


class TestUpdateVaultRetrievalStats:
    """_update_vault_retrieval_stats increments count and sets date."""

    def _make_vault_file(self, tmp_path: Path, count: int = 0) -> Path:
        last_ret = "null" if count == 0 else "2026-04-01"
        content = (
            "---\n"
            "tags: [\"insight\"]\n"
            "created: 2026-04-07\n"
            "updated: 2026-04-07\n"
            f"last_retrieved: {last_ret}\n"
            f"retrieval_count: {count}\n"
            "---\n\n"
            "Test content\n"
        )
        filepath = tmp_path / "test.md"
        filepath.write_text(content)
        return filepath

    def test_increments_retrieval_count_from_zero(self, service, tmp_path):
        filepath = self._make_vault_file(tmp_path, count=0)
        service._update_vault_retrieval_stats([str(filepath)])
        text = filepath.read_text()
        assert "retrieval_count: 1" in text

    def test_increments_retrieval_count_from_nonzero(self, service, tmp_path):
        filepath = self._make_vault_file(tmp_path, count=5)
        service._update_vault_retrieval_stats([str(filepath)])
        text = filepath.read_text()
        assert "retrieval_count: 6" in text

    def test_sets_last_retrieved_date(self, service, tmp_path):
        filepath = self._make_vault_file(tmp_path, count=0)
        service._update_vault_retrieval_stats([str(filepath)])
        text = filepath.read_text()
        # Should now have a date, not null
        match = re.search(r"^last_retrieved: (\S+)$", text, re.MULTILINE)
        assert match is not None
        assert match.group(1) != "null"
        assert re.match(r"\d{4}-\d{2}-\d{2}", match.group(1))

    def test_skips_nonexistent_paths(self, service, tmp_path):
        """Non-existent paths are silently skipped."""
        filepath = self._make_vault_file(tmp_path, count=0)
        # No exception
        service._update_vault_retrieval_stats([
            str(filepath),
            str(tmp_path / "nonexistent.md"),
        ])
        text = filepath.read_text()
        assert "retrieval_count: 1" in text

    def test_skips_empty_paths(self, service, tmp_path):
        """Empty string paths are skipped."""
        service._update_vault_retrieval_stats(["", ""])

    def test_multiple_files_updated(self, service, tmp_path):
        """Multiple vault files are all updated."""
        dir_a = tmp_path / "a"
        dir_a.mkdir(parents=True, exist_ok=True)
        file1 = dir_a / "test.md"
        file1.write_text(
            "---\ntags: []\nlast_retrieved: null\nretrieval_count: 2\n---\n\nA\n"
        )

        dir_b = tmp_path / "b"
        dir_b.mkdir(parents=True, exist_ok=True)
        file2 = dir_b / "test.md"
        file2.write_text(
            "---\ntags: []\nlast_retrieved: null\nretrieval_count: 0\n---\n\nB\n"
        )

        service._update_vault_retrieval_stats([str(file1), str(file2)])

        assert "retrieval_count: 3" in file1.read_text()
        assert "retrieval_count: 1" in file2.read_text()


# ---------------------------------------------------------------------------
# search() integration with retrieval tracking
# ---------------------------------------------------------------------------


class TestSearchRetrievalTracking:
    """search() updates vault retrieval stats for returned results."""

    @pytest.mark.asyncio
    async def test_search_updates_vault_files(self, service, mock_store, tmp_path):
        """After search returns results, vault files are updated."""
        # Create a vault file that the search result will reference
        vault_file = tmp_path / "insights" / "test.md"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_text(
            "---\ntags: []\nlast_retrieved: null\nretrieval_count: 0\n---\n\nTest\n"
        )

        mock_store.search.return_value = [
            {
                "content": "Test result",
                "source": str(vault_file),
                "heading": "Test",
                "score": 0.95,
                "chunk_hash": "hash1",
                "entry_type": "document",
                "topic": "",
                "tags": "[]",
            }
        ]

        results = await service.search("proj", "query", scope="project_proj")
        assert len(results) == 1

        # Vault file should have been updated
        text = vault_file.read_text()
        assert "retrieval_count: 1" in text
        assert "last_retrieved: null" not in text

    @pytest.mark.asyncio
    async def test_search_track_retrieval_false(self, service, mock_store, tmp_path):
        """track_retrieval=False skips vault file updates."""
        vault_file = tmp_path / "insights" / "test.md"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_text(
            "---\ntags: []\nlast_retrieved: null\nretrieval_count: 0\n---\n\nTest\n"
        )

        mock_store.search.return_value = [
            {
                "content": "Test result",
                "source": str(vault_file),
                "heading": "Test",
                "score": 0.95,
                "chunk_hash": "hash1",
                "entry_type": "document",
                "topic": "",
                "tags": "[]",
            }
        ]

        results = await service.search(
            "proj", "query", scope="project_proj", track_retrieval=False
        )
        assert len(results) == 1

        # Vault file should NOT have been updated
        text = vault_file.read_text()
        assert "retrieval_count: 0" in text
        assert "last_retrieved: null" in text

    @pytest.mark.asyncio
    async def test_multiscope_search_updates_vault(self, service, mock_router, tmp_path):
        """Multi-scope search (scope=None) also updates vault files."""
        vault_file = tmp_path / "insights" / "test.md"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_text(
            "---\ntags: []\nlast_retrieved: null\nretrieval_count: 0\n---\n\nTest\n"
        )

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Multi-scope result",
                    "source": str(vault_file),
                    "heading": "Test",
                    "score": 0.9,
                    "chunk_hash": "hash1",
                    "entry_type": "document",
                    "topic": "",
                    "tags": "[]",
                    "_collection": "aq_project_test",
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        results = await service.search("proj", "query")
        assert len(results) == 1

        text = vault_file.read_text()
        assert "retrieval_count: 1" in text

    @pytest.mark.asyncio
    async def test_search_no_results_no_vault_update(self, service, mock_store):
        """When search returns no results, no vault updates happen."""
        mock_store.search.return_value = []

        with patch.object(service, "_update_vault_retrieval_stats") as mock_update:
            await service.search("proj", "query", scope="project_proj")
            mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# save_document: source_playbook flows through
# ---------------------------------------------------------------------------


class TestSaveDocumentSourcePlaybook:
    """save_document passes source_playbook to vault file."""

    @pytest.mark.asyncio
    async def test_save_includes_source_playbook(self, service, mock_store, tmp_path):
        """save_document writes source_playbook to vault frontmatter."""
        result = await service.save_document(
            "proj",
            "Test insight content",
            source_task="task-001",
            source_playbook="task-outcome",
            tags=["insight"],
        )

        assert result["source_playbook"] == "task-outcome"
        vault_path = Path(result["vault_path"])
        assert vault_path.exists()
        text = vault_path.read_text()
        assert "source_playbook: task-outcome" in text
        assert "source_task: task-001" in text
        assert "retrieval_count: 0" in text
        assert "last_retrieved: null" in text

    @pytest.mark.asyncio
    async def test_save_without_playbook(self, service, mock_store, tmp_path):
        """save_document without source_playbook omits it from frontmatter."""
        result = await service.save_document(
            "proj",
            "Test insight content",
            tags=["insight"],
        )

        assert result["source_playbook"] == ""
        vault_path = Path(result["vault_path"])
        text = vault_path.read_text()
        assert "source_playbook" not in text


# ---------------------------------------------------------------------------
# Plugin integration: cmd_memory_save with source_playbook
# ---------------------------------------------------------------------------


class TestPluginMemorySaveSourcePlaybook:
    """Plugin cmd_memory_save accepts and passes source_playbook."""

    @pytest.fixture
    def plugin(self, service):
        """Create a minimal MemoryV2Plugin for testing."""
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        p = MemoryV2Plugin.__new__(MemoryV2Plugin)
        p._service = service
        p._log = MagicMock()
        p._ctx = MagicMock()
        p._ctx.get_service = MagicMock(return_value=None)
        # Stub _infer_topic to return None (no auto-topic)
        p._infer_topic = AsyncMock(return_value=None)
        p._DEDUP_MIN_WORDS = 10
        p._DEDUP_NEAR_IDENTICAL = 0.95
        p._DEDUP_RELATED = 0.80
        p._SUMMARY_CHAR_THRESHOLD = 800
        return p

    @pytest.mark.asyncio
    async def test_cmd_memory_save_passes_source_playbook(self, plugin, tmp_path):
        """cmd_memory_save forwards source_playbook to save_document."""
        result = await plugin.cmd_memory_save({
            "project_id": "proj",
            "content": "This is a test insight that is long enough to avoid dedup skip",
            "source_task": "task-001",
            "source_playbook": "task-outcome",
        })

        assert result.get("success") is True
        assert result.get("action") == "created"
        assert result.get("source_playbook") == "task-outcome"

    @pytest.mark.asyncio
    async def test_cmd_memory_save_without_playbook(self, plugin, tmp_path):
        """cmd_memory_save works without source_playbook."""
        result = await plugin.cmd_memory_save({
            "project_id": "proj",
            "content": "This is another test insight that is long enough",
        })

        assert result.get("success") is True
        assert result.get("source_playbook") == ""


# ---------------------------------------------------------------------------
# Plugin search results include retrieval tracking fields
# ---------------------------------------------------------------------------


class TestPluginSearchResultFields:
    """Plugin search result formatters include retrieval tracking fields."""

    @pytest.fixture
    def plugin(self, service):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        p = MemoryV2Plugin.__new__(MemoryV2Plugin)
        p._service = service
        p._log = MagicMock()
        return p

    def test_format_search_result_includes_retrieval_count(self, plugin):
        result = plugin._format_search_result({
            "content": "Test",
            "source": "/path",
            "heading": "Test",
            "score": 0.9,
            "chunk_hash": "h1",
            "entry_type": "document",
            "topic": "",
            "tags": "[]",
            "retrieval_count": 5,
            "last_retrieved": 1712700000,
        })
        assert result["retrieval_count"] == 5
        assert result["last_retrieved"] == 1712700000

    def test_format_search_result_defaults_zero(self, plugin):
        """Missing retrieval fields default to 0."""
        result = plugin._format_search_result({
            "content": "Test",
            "source": "/path",
            "heading": "Test",
            "score": 0.9,
            "chunk_hash": "h1",
            "entry_type": "document",
            "topic": "",
            "tags": "[]",
        })
        assert result["retrieval_count"] == 0
        assert result["last_retrieved"] == 0

    def test_format_list_entry_includes_last_retrieved(self, plugin):
        entry = plugin._format_list_entry({
            "content": "Test content",
            "heading": "Test",
            "topic": "",
            "tags": "[]",
            "source": "/path",
            "entry_type": "document",
            "retrieval_count": 3,
            "last_retrieved": 1712700000,
            "updated_at": 1712600000,
            "chunk_hash": "h1",
        })
        assert entry["retrieval_count"] == 3
        assert entry["last_retrieved"] == 1712700000
