"""Unit tests for src/memory.py — MemoryManager and MemoryConfig.

All tests mock the memsearch dependency so they run without Milvus or
embedding providers. The focus is on: graceful degradation, correct argument
forwarding, markdown formatting, per-project collection isolation, and
error resilience.

Also includes tests for the ``memory_search`` hook-engine context step
(src/hooks.py) which delegates to MemoryManager.
"""

import os
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.memory import MemoryConfig, MemoryManager, MEMSEARCH_AVAILABLE
from src.models import MemoryContext


# ---------------------------------------------------------------------------
# Lightweight fakes for Task and AgentOutput (avoid importing full models
# in unit tests — keeps the test boundary tight).
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-123"
    project_id: str = "my-project"
    title: str = "Add user auth"
    description: str = "Implement JWT authentication"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


@dataclass
class FakeOutput:
    result: MagicMock = field(default_factory=lambda: MagicMock(value="completed"))
    summary: str = "Added JWT auth with refresh tokens."
    files_changed: list = field(default_factory=lambda: ["src/auth.py", "tests/test_auth.py"])
    tokens_used: int = 12345


# ---------------------------------------------------------------------------
# MemoryConfig tests
# ---------------------------------------------------------------------------


class TestMemoryConfig:
    def test_defaults(self):
        cfg = MemoryConfig()
        assert cfg.enabled is False
        assert cfg.embedding_provider == "openai"
        assert cfg.auto_remember is True
        assert cfg.auto_recall is True
        assert cfg.recall_top_k == 5
        assert cfg.max_chunk_size == 1500

    def test_custom_values(self):
        cfg = MemoryConfig(
            enabled=True,
            embedding_provider="local",
            recall_top_k=10,
            milvus_uri="http://localhost:19530",
        )
        assert cfg.enabled is True
        assert cfg.embedding_provider == "local"
        assert cfg.recall_top_k == 10
        assert cfg.milvus_uri == "http://localhost:19530"


# ---------------------------------------------------------------------------
# MemoryManager tests
# ---------------------------------------------------------------------------


class TestMemoryManager:
    """Unit tests with mocked memsearch dependency."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    # -- Disabled / unavailable scenarios ----------------------------------

    async def test_recall_returns_empty_when_disabled(self, tmp_path):
        """MemoryManager with enabled=False returns empty list."""
        mgr = MemoryManager(MemoryConfig(enabled=False))
        task = FakeTask()
        result = await mgr.recall(task, str(tmp_path))
        assert result == []

    async def test_remember_returns_none_when_disabled(self, tmp_path):
        """MemoryManager with enabled=False skips remember."""
        mgr = MemoryManager(MemoryConfig(enabled=False))
        result = await mgr.remember(FakeTask(), FakeOutput(), str(tmp_path))
        assert result is None

    async def test_search_returns_empty_when_disabled(self, tmp_path):
        """Disabled manager returns empty search results."""
        mgr = MemoryManager(MemoryConfig(enabled=False))
        result = await mgr.search("proj", str(tmp_path), "query")
        assert result == []

    async def test_reindex_returns_zero_when_disabled(self, tmp_path):
        mgr = MemoryManager(MemoryConfig(enabled=False))
        assert await mgr.reindex("proj", str(tmp_path)) == 0

    async def test_stats_when_disabled(self, tmp_path):
        mgr = MemoryManager(MemoryConfig(enabled=False))
        stats = await mgr.stats("proj", str(tmp_path))
        assert stats["enabled"] is False

    @patch("src.memory.MEMSEARCH_AVAILABLE", False)
    async def test_recall_returns_empty_when_memsearch_not_installed(self, tmp_path):
        """Graceful degradation when memsearch package is absent."""
        mgr = self._make_manager()
        result = await mgr.recall(FakeTask(), str(tmp_path))
        assert result == []

    @patch("src.memory.MEMSEARCH_AVAILABLE", False)
    async def test_get_instance_returns_none_when_unavailable(self, tmp_path):
        mgr = self._make_manager()
        assert await mgr.get_instance("proj", str(tmp_path)) is None

    # -- Auto-recall / auto-remember flags ---------------------------------

    async def test_recall_skipped_when_auto_recall_false(self, tmp_path):
        mgr = self._make_manager(auto_recall=False)
        result = await mgr.recall(FakeTask(), str(tmp_path))
        assert result == []

    async def test_remember_skipped_when_auto_remember_false(self, tmp_path):
        mgr = self._make_manager(auto_remember=False)
        result = await mgr.remember(FakeTask(), FakeOutput(), str(tmp_path))
        assert result is None

    # -- Collection naming -------------------------------------------------

    def test_collection_name_isolation(self):
        """Each project gets a unique, Milvus-safe collection name."""
        mgr = self._make_manager()
        assert mgr._collection_name("my-project") == "aq_my_project_memory"
        assert mgr._collection_name("other project") == "aq_other_project_memory"
        assert mgr._collection_name("simple") == "aq_simple_memory"

    def test_collection_names_are_distinct(self):
        """Different project IDs produce different collection names."""
        mgr = self._make_manager()
        names = {mgr._collection_name(pid) for pid in ["proj-a", "proj-b", "proj-c"]}
        assert len(names) == 3

    # -- Memory paths ------------------------------------------------------

    def test_memory_paths_includes_notes_when_enabled(self, tmp_path):
        notes_dir = tmp_path / "notes" / "test-project"
        notes_dir.mkdir(parents=True)
        mgr = self._make_manager(storage_root=str(tmp_path), index_notes=True)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert mgr._project_memory_dir("test-project") in paths
        assert str(notes_dir) in paths

    def test_memory_paths_excludes_notes_when_disabled(self, tmp_path):
        (tmp_path / "notes" / "test-project").mkdir(parents=True)
        mgr = self._make_manager(storage_root=str(tmp_path), index_notes=False)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert len(paths) == 1
        assert "notes" not in paths[0]

    def test_memory_paths_skips_missing_notes_dir(self, tmp_path):
        """notes/ directory absent — only memory/ included."""
        mgr = self._make_manager(storage_root=str(tmp_path), index_notes=True)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert len(paths) == 1

    def test_memory_paths_includes_specs_when_enabled(self, tmp_path):
        """specs/ in workspace is included when index_specs=True."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        mgr = self._make_manager(storage_root=str(tmp_path), index_specs=True)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert str(specs_dir) in paths

    def test_memory_paths_excludes_specs_when_disabled(self, tmp_path):
        (tmp_path / "specs").mkdir()
        mgr = self._make_manager(storage_root=str(tmp_path), index_specs=False)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert not any("specs" in p for p in paths)

    def test_memory_paths_includes_docs_when_enabled(self, tmp_path):
        """docs/ in workspace is included when index_docs=True."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        mgr = self._make_manager(storage_root=str(tmp_path), index_docs=True)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert str(docs_dir) in paths

    def test_memory_paths_excludes_docs_when_disabled(self, tmp_path):
        (tmp_path / "docs").mkdir()
        mgr = self._make_manager(storage_root=str(tmp_path), index_docs=False)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert not any("docs" in p for p in paths)

    def test_memory_paths_skips_missing_specs_and_docs(self, tmp_path):
        """specs/ and docs/ absent — not included even when enabled."""
        mgr = self._make_manager(storage_root=str(tmp_path), index_specs=True, index_docs=True)
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert not any("specs" in p or "docs" in p for p in paths)

    def test_memory_paths_includes_all_sources(self, tmp_path):
        """All directories included when present and enabled."""
        (tmp_path / "notes" / "test-project").mkdir(parents=True)
        (tmp_path / "specs").mkdir()
        (tmp_path / "docs").mkdir()
        mgr = self._make_manager(
            storage_root=str(tmp_path), index_notes=True, index_specs=True, index_docs=True
        )
        paths = mgr._memory_paths("test-project", str(tmp_path))
        assert len(paths) == 4  # memory dir + notes + specs + docs

    # -- Project doc file indexing -----------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_index_project_doc_files_indexes_existing(self, MockMemSearch, tmp_path):
        """CLAUDE.md and README.md are indexed when present."""
        (tmp_path / "CLAUDE.md").write_text("# Project\nSome content")
        (tmp_path / "README.md").write_text("# Readme\nOther content")
        mgr = self._make_manager(storage_root=str(tmp_path), index_project_docs=True)
        mock_instance = AsyncMock()
        await mgr._index_project_doc_files(mock_instance, str(tmp_path))
        assert mock_instance.index_file.call_count == 2

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_index_project_doc_files_skips_missing(self, MockMemSearch, tmp_path):
        """Missing doc files are silently skipped."""
        (tmp_path / "CLAUDE.md").write_text("# Project")
        # README.md does not exist
        mgr = self._make_manager(storage_root=str(tmp_path), index_project_docs=True)
        mock_instance = AsyncMock()
        await mgr._index_project_doc_files(mock_instance, str(tmp_path))
        assert mock_instance.index_file.call_count == 1

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_index_project_doc_files_disabled(self, MockMemSearch, tmp_path):
        """No indexing when index_project_docs is False."""
        (tmp_path / "CLAUDE.md").write_text("# Project")
        mgr = self._make_manager(storage_root=str(tmp_path), index_project_docs=False)
        mock_instance = AsyncMock()
        await mgr._index_project_doc_files(mock_instance, str(tmp_path))
        mock_instance.index_file.assert_not_called()

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_index_project_doc_files_skips_unchanged(self, MockMemSearch, tmp_path):
        """Unchanged files are not re-indexed on second call."""
        (tmp_path / "CLAUDE.md").write_text("# Project")
        mgr = self._make_manager(storage_root=str(tmp_path), index_project_docs=True)
        mock_instance = AsyncMock()
        await mgr._index_project_doc_files(mock_instance, str(tmp_path))
        assert mock_instance.index_file.call_count == 1
        # Second call — mtime hasn't changed, should skip
        await mgr._index_project_doc_files(mock_instance, str(tmp_path))
        assert mock_instance.index_file.call_count == 1  # still 1

    # -- build_context project_docs tier -----------------------------------

    def test_memory_context_project_docs_tier(self):
        """project_docs field appears in context output between profile and notes."""
        ctx = MemoryContext(
            profile="My profile",
            project_docs="### CLAUDE.md\nProject conventions",
            notes="Some notes",
        )
        block = ctx.to_context_block()
        # project_docs appears after profile and before notes
        assert "## Project Documentation" in block
        profile_pos = block.index("## Project Profile")
        docs_pos = block.index("## Project Documentation")
        notes_pos = block.index("## Relevant Notes")
        assert profile_pos < docs_pos < notes_pos

    def test_memory_context_is_empty_includes_project_docs(self):
        """is_empty returns False when only project_docs is set."""
        ctx = MemoryContext(project_docs="something")
        assert not ctx.is_empty

    # -- Recall with mocked MemSearch --------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_recall_uses_task_title_and_description(self, MockMemSearch, tmp_path):
        """Search query combines title + description."""
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(return_value=[{"content": "result", "score": 0.9}])
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        task = FakeTask(title="Auth module", description="JWT implementation")
        results = await mgr.recall(task, str(tmp_path))

        mock_instance.search.assert_called_once()
        call_args = mock_instance.search.call_args
        query = call_args[0][0]
        assert "Auth module" in query
        assert "JWT implementation" in query
        assert len(results) == 1

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_recall_respects_top_k(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(return_value=[])
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager(recall_top_k=3)
        await mgr.recall(FakeTask(), str(tmp_path))
        _, kwargs = mock_instance.search.call_args
        assert kwargs["top_k"] == 3

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_recall_top_k_override(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(return_value=[])
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager(recall_top_k=5)
        await mgr.recall(FakeTask(), str(tmp_path), top_k=8)
        _, kwargs = mock_instance.search.call_args
        assert kwargs["top_k"] == 8

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_recall_handles_search_errors_gracefully(self, MockMemSearch, tmp_path):
        """Exceptions from memsearch.search don't propagate."""
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(side_effect=RuntimeError("search boom"))
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        result = await mgr.recall(FakeTask(), str(tmp_path))
        assert result == []

    # -- Remember with mocked MemSearch ------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_writes_markdown_file(self, MockMemSearch, tmp_path):
        """Task completion creates properly formatted markdown."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        task = FakeTask(id="task-abc")
        output = FakeOutput()

        path = await mgr.remember(task, output, str(tmp_path))

        assert path is not None
        assert path.endswith("task-abc.md")
        assert os.path.exists(path)

        content = open(path).read()
        assert "# Task: task-abc" in content
        assert "Add user auth" in content
        assert "src/auth.py" in content

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_indexes_file(self, MockMemSearch, tmp_path):
        """After writing markdown, index_file is called."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        path = await mgr.remember(FakeTask(), FakeOutput(), str(tmp_path))

        mock_instance.index_file.assert_called_once_with(path)

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_handles_index_error_gracefully(self, MockMemSearch, tmp_path):
        """Indexing failures don't prevent the file from being written."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock(side_effect=RuntimeError("index boom"))
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        path = await mgr.remember(FakeTask(), FakeOutput(), str(tmp_path))

        # File should still exist even though indexing failed
        assert path is not None
        assert os.path.exists(path)

    # -- Search (ad-hoc) ---------------------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_search_forwards_query(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(return_value=[{"content": "found"}])
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        results = await mgr.search("proj", str(tmp_path), "auth middleware", top_k=3)

        mock_instance.search.assert_called_once_with("auth middleware", top_k=3)
        assert len(results) == 1

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_search_handles_errors(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(side_effect=Exception("search fail"))
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        results = await mgr.search("proj", str(tmp_path), "query")
        assert results == []

    # -- Batch search ------------------------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_batch_search_multiple_queries(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(
            side_effect=[
                [{"content": "result A"}],
                [{"content": "result B"}],
            ]
        )
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        results = await mgr.batch_search(
            "proj", str(tmp_path), ["query A", "query B"], top_k=5
        )

        assert set(results.keys()) == {"query A", "query B"}
        assert len(results["query A"]) == 1
        assert len(results["query B"]) == 1
        assert results["query A"][0]["content"] == "result A"
        assert results["query B"][0]["content"] == "result B"

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_batch_search_empty_queries(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        results = await mgr.batch_search("proj", str(tmp_path), [])

        assert results == {}

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_batch_search_partial_failure(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(
            side_effect=[
                [{"content": "ok"}],
                Exception("search fail"),
                [{"content": "also ok"}],
            ]
        )
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        results = await mgr.batch_search(
            "proj", str(tmp_path), ["q1", "q2", "q3"], top_k=3
        )

        assert len(results) == 3
        assert len(results["q1"]) == 1
        assert results["q2"] == []  # failed query returns empty
        assert len(results["q3"]) == 1

    async def test_batch_search_returns_empty_when_disabled(self, tmp_path):
        mgr = MemoryManager(MemoryConfig(enabled=False))
        results = await mgr.batch_search("proj", str(tmp_path), ["q1", "q2"])
        assert results == {"q1": [], "q2": []}

    # -- Reindex -----------------------------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_calls_index_force(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(return_value=42)
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        count = await mgr.reindex("proj", str(tmp_path))
        mock_instance.index.assert_called_once_with(force=True)
        assert count == 42

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_handles_errors(self, MockMemSearch, tmp_path):
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(side_effect=RuntimeError("reindex boom"))
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        assert await mgr.reindex("proj", str(tmp_path)) == 0

    # -- Stats -------------------------------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_stats_when_enabled(self, MockMemSearch, tmp_path):
        MockMemSearch.return_value = MagicMock()

        mgr = self._make_manager()
        stats = await mgr.stats("my-project", str(tmp_path))

        assert stats["enabled"] is True
        assert stats["available"] is True
        assert stats["collection"] == "aq_my_project_memory"
        assert "milvus_uri" in stats

    @patch("src.memory.MEMSEARCH_AVAILABLE", False)
    async def test_stats_when_memsearch_unavailable(self, tmp_path):
        mgr = self._make_manager()
        stats = await mgr.stats("proj", str(tmp_path))
        assert stats["enabled"] is True
        assert stats["available"] is False

    # -- Close -------------------------------------------------------------

    async def test_close_clears_instances(self):
        mgr = self._make_manager()
        mock_instance = MagicMock()
        mgr._instances["proj"] = mock_instance
        mock_watcher = MagicMock()
        mgr._watchers["proj"] = mock_watcher

        await mgr.close()

        mock_instance.close.assert_called_once()
        mock_watcher.stop.assert_called_once()
        assert len(mgr._instances) == 0
        assert len(mgr._watchers) == 0

    async def test_close_handles_errors_gracefully(self):
        mgr = self._make_manager()
        mock_instance = MagicMock()
        mock_instance.close.side_effect = RuntimeError("close boom")
        mgr._instances["proj"] = mock_instance

        # Should not raise
        await mgr.close()
        assert len(mgr._instances) == 0

    # -- Instance caching --------------------------------------------------

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_get_instance_caches(self, MockMemSearch, tmp_path):
        """Same project returns the same MemSearch instance."""
        mock_instance = MagicMock()
        MockMemSearch.return_value = mock_instance

        mgr = self._make_manager()
        inst1 = await mgr.get_instance("proj", str(tmp_path))
        inst2 = await mgr.get_instance("proj", str(tmp_path))

        assert inst1 is inst2
        assert MockMemSearch.call_count == 1

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_get_instance_separate_projects(self, MockMemSearch, tmp_path):
        """Different projects get different MemSearch instances."""
        mgr = self._make_manager()
        await mgr.get_instance("proj-a", str(tmp_path))
        await mgr.get_instance("proj-b", str(tmp_path))
        assert MockMemSearch.call_count == 2

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_get_instance_handles_creation_error(self, MockMemSearch, tmp_path):
        MockMemSearch.side_effect = RuntimeError("init boom")

        mgr = self._make_manager()
        result = await mgr.get_instance("proj", str(tmp_path))
        assert result is None

    # -- Markdown formatting -----------------------------------------------

    def test_format_task_memory(self):
        """Verify markdown output format for task memories."""
        mgr = self._make_manager()
        task = FakeTask(
            id="task-xyz",
            title="Fix login bug",
            project_id="webapp",
        )
        task.task_type = MagicMock(value="bugfix")
        output = FakeOutput(
            summary="Fixed the login redirect loop.",
            files_changed=["src/login.py"],
            tokens_used=5000,
        )
        output.result = MagicMock(value="completed")

        md = mgr._format_task_memory(task, output)

        assert "# Task: task-xyz — Fix login bug" in md
        assert "**Project:** webapp" in md
        assert "**Type:** bugfix" in md
        assert "**Status:** completed" in md
        assert "5,000" in md  # comma-formatted tokens
        assert "## Summary" in md
        assert "Fixed the login redirect loop." in md
        assert "## Files Changed" in md
        assert "- src/login.py" in md

    def test_format_task_memory_no_files(self):
        mgr = self._make_manager()
        task = FakeTask()
        output = FakeOutput(files_changed=[], summary="")
        md = mgr._format_task_memory(task, output)
        assert "No files changed." in md

    def test_format_task_memory_no_summary(self):
        mgr = self._make_manager()
        task = FakeTask()
        output = FakeOutput(summary="")
        md = mgr._format_task_memory(task, output)
        assert "No summary available." in md

    def test_format_task_memory_no_task_type(self):
        mgr = self._make_manager()
        task = FakeTask()
        task.task_type = None
        output = FakeOutput()
        md = mgr._format_task_memory(task, output)
        assert "**Type:** unknown" in md


# ---------------------------------------------------------------------------
class TestMemoryCompaction:
    """Tests for age-based memory compaction into weekly digests."""

    def _make_manager(self, storage_root: str, **overrides) -> MemoryManager:
        cfg = MemoryConfig(
            enabled=True,
            compact_enabled=True,
            compact_recent_days=7,
            compact_archive_days=30,
            **overrides,
        )
        return MemoryManager(cfg, storage_root=storage_root)

    def _write_task_file(self, tasks_dir: str, name: str, content: str, age_days: float = 0):
        """Write a task file and set its mtime to age_days ago."""
        import time as _time

        path = os.path.join(tasks_dir, name)
        with open(path, "w") as f:
            f.write(content)
        if age_days > 0:
            mtime = _time.time() - (age_days * 86400)
            os.utime(path, (mtime, mtime))
        return path

    async def test_compact_no_tasks_dir(self, tmp_path):
        """Compaction with no tasks directory returns no_tasks status."""
        mgr = self._make_manager(str(tmp_path))
        result = await mgr.compact("proj", str(tmp_path))
        assert result["status"] == "no_tasks"
        assert result["tasks_inspected"] == 0

    async def test_compact_all_recent(self, tmp_path):
        """All recent files are kept as-is — no digests created."""
        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        os.makedirs(tasks_dir)

        self._write_task_file(tasks_dir, "task-1.md", "# Recent task", age_days=1)
        self._write_task_file(tasks_dir, "task-2.md", "# Another recent", age_days=3)

        result = await mgr.compact("proj", str(tmp_path))
        assert result["status"] == "compacted"
        assert result["tasks_inspected"] == 2
        assert result["recent_kept"] == 2
        assert result["medium_digested"] == 0
        assert result["old_removed"] == 0
        assert result["digests_created"] == 0
        assert result["files_removed"] == 0

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_compact_medium_creates_digest(self, mock_provider, tmp_path):
        """Medium-age files are LLM-summarized into weekly digests."""
        # Mock the LLM provider
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# Weekly Digest\n- Task summaries here")]
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(return_value=mock_response)
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        os.makedirs(tasks_dir)

        # Create medium-age files in the same ISO week.  We pick ages that
        # land on the same Mon–Sun window regardless of what day the test runs
        # by computing target dates that fall on Wed and Thu of the same week.
        import datetime as _dt

        today = _dt.date.today()
        # Find a Wednesday that is 8-14 days ago (guaranteed medium tier)
        days_since_wed = (today.weekday() - 2) % 7  # 0=Mon … 6=Sun; Wed=2
        target_wed = today - _dt.timedelta(days=days_since_wed + 7)  # Wed of last-last week
        target_thu = target_wed + _dt.timedelta(days=1)
        age1 = (today - target_wed).days
        age2 = (today - target_thu).days
        self._write_task_file(tasks_dir, "task-old1.md", "# Task 1\nSome work", age_days=age1)
        self._write_task_file(tasks_dir, "task-old2.md", "# Task 2\nMore work", age_days=age2)

        result = await mgr.compact("proj", str(tmp_path))
        assert result["status"] == "compacted"
        assert result["medium_digested"] == 2
        assert result["digests_created"] == 1
        assert result["files_removed"] == 0  # medium files not removed

        # Verify digest file was created
        digests_dir = os.path.join(str(tmp_path), "memory", "proj", "digests")
        digest_files = os.listdir(digests_dir)
        assert len(digest_files) == 1
        assert digest_files[0].startswith("week-")

        # Original task files still exist (medium, not old)
        assert os.path.isfile(os.path.join(tasks_dir, "task-old1.md"))
        assert os.path.isfile(os.path.join(tasks_dir, "task-old2.md"))

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_compact_old_files_removed(self, mock_provider, tmp_path):
        """Old files (> archive_days) are deleted after digesting."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# Digest\nOld work summary")]
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(return_value=mock_response)
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        os.makedirs(tasks_dir)

        # Create old files (45 days old)
        self._write_task_file(tasks_dir, "task-ancient.md", "# Ancient task", age_days=45)

        result = await mgr.compact("proj", str(tmp_path))
        assert result["old_removed"] == 1
        assert result["files_removed"] == 1
        assert result["digests_created"] == 1

        # Old file should be deleted
        assert not os.path.isfile(os.path.join(tasks_dir, "task-ancient.md"))

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_compact_mixed_tiers(self, mock_provider, tmp_path):
        """Mixed-age files are correctly classified and processed."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# Digest\nSummary")]
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(return_value=mock_response)
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        os.makedirs(tasks_dir)

        # Recent (2 days)
        self._write_task_file(tasks_dir, "recent.md", "# Recent", age_days=2)
        # Medium (15 days)
        self._write_task_file(tasks_dir, "medium.md", "# Medium", age_days=15)
        # Old (40 days)
        self._write_task_file(tasks_dir, "old.md", "# Old", age_days=40)

        result = await mgr.compact("proj", str(tmp_path))
        assert result["tasks_inspected"] == 3
        assert result["recent_kept"] == 1
        assert result["medium_digested"] == 1
        assert result["old_removed"] == 1

        # Recent file still exists
        assert os.path.isfile(os.path.join(tasks_dir, "recent.md"))
        # Medium file still exists
        assert os.path.isfile(os.path.join(tasks_dir, "medium.md"))
        # Old file removed
        assert not os.path.isfile(os.path.join(tasks_dir, "old.md"))

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_compact_skips_existing_digest(self, mock_provider, tmp_path):
        """Existing digest files are not overwritten; old files still removed."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# Digest\nNew summary")]
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(return_value=mock_response)
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        digests_dir = os.path.join(str(tmp_path), "memory", "proj", "digests")
        os.makedirs(tasks_dir)
        os.makedirs(digests_dir)

        # Create an old file
        import datetime as dt
        import time as _time

        age_days = 45
        mtime = _time.time() - (age_days * 86400)
        d = dt.date.fromtimestamp(mtime)
        iso_year, iso_week, _ = d.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"

        self._write_task_file(tasks_dir, "old-task.md", "# Old", age_days=age_days)

        # Pre-create the digest file
        digest_path = os.path.join(digests_dir, f"week-{week_key}.md")
        with open(digest_path, "w") as f:
            f.write("# Existing Digest\nAlready here")

        result = await mgr.compact("proj", str(tmp_path))

        # LLM should NOT have been called — digest already existed
        provider_instance.create_message.assert_not_called()

        # Old file should still be removed
        assert result["files_removed"] == 1
        assert not os.path.isfile(os.path.join(tasks_dir, "old-task.md"))

        # Existing digest should be preserved
        with open(digest_path) as f:
            assert "Existing Digest" in f.read()

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_summarize_batch(self, mock_provider, tmp_path):
        """_summarize_batch calls LLM with correct prompts."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# Digest\nSummarized content")]
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(return_value=mock_response)
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        result = await mgr._summarize_batch(
            ["# Task 1\nDid thing A", "# Task 2\nDid thing B"],
            "2026-W10",
        )

        assert result == "# Digest\nSummarized content"
        provider_instance.create_message.assert_called_once()

        # Verify prompt contains task count and date range
        call_kwargs = provider_instance.create_message.call_args
        user_msg = call_kwargs.kwargs.get("messages", call_kwargs[1].get("messages", [{}]))[0][
            "content"
        ]
        assert "2 tasks" in user_msg
        assert "2026-W10" in user_msg

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_summarize_batch_llm_failure(self, mock_provider, tmp_path):
        """_summarize_batch returns empty string on LLM failure."""
        provider_instance = AsyncMock()
        provider_instance.create_message = AsyncMock(side_effect=RuntimeError("LLM error"))
        mock_provider.return_value = provider_instance

        mgr = self._make_manager(str(tmp_path))
        result = await mgr._summarize_batch(["# Task 1"], "2026-W10")
        assert result == ""

    @patch("src.memory.MemoryManager._get_revision_provider")
    async def test_summarize_batch_no_provider(self, mock_provider, tmp_path):
        """_summarize_batch returns empty string when no LLM provider."""
        mock_provider.return_value = None

        mgr = self._make_manager(str(tmp_path))
        result = await mgr._summarize_batch(["# Task 1"], "2026-W10")
        assert result == ""

    async def test_compact_updates_last_compact_timestamp(self, tmp_path):
        """compact() updates the _last_compact tracking dict."""
        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        os.makedirs(tasks_dir)

        assert "proj" not in mgr._last_compact
        await mgr.compact("proj", str(tmp_path))
        assert "proj" in mgr._last_compact
        assert mgr._last_compact["proj"] > 0


class TestMemoryStatsEnhanced:
    """Tests for enhanced memory stats with age-tier breakdown."""

    def _make_manager(self, storage_root: str, **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    def _write_task_file(self, tasks_dir: str, name: str, age_days: float = 0):
        import time as _time

        path = os.path.join(tasks_dir, name)
        with open(path, "w") as f:
            f.write(f"# {name}")
        if age_days > 0:
            mtime = _time.time() - (age_days * 86400)
            os.utime(path, (mtime, mtime))

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_stats_includes_age_breakdown(self, mock_memsearch, tmp_path):
        """stats() includes task memory age-tier counts."""
        mock_instance = MagicMock()
        mock_memsearch.return_value = mock_instance

        mgr = self._make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        digests_dir = os.path.join(str(tmp_path), "memory", "proj", "digests")
        os.makedirs(tasks_dir)
        os.makedirs(digests_dir)

        # Create files at various ages
        self._write_task_file(tasks_dir, "recent.md", age_days=2)
        self._write_task_file(tasks_dir, "medium.md", age_days=15)
        self._write_task_file(tasks_dir, "old.md", age_days=40)

        # Create a digest file
        with open(os.path.join(digests_dir, "week-2026-W05.md"), "w") as f:
            f.write("# Digest")

        stats = await mgr.stats("proj", str(tmp_path))

        assert stats["task_memories"] == 3
        assert stats["task_memories_recent"] == 1
        assert stats["task_memories_medium"] == 1
        assert stats["task_memories_old"] == 1
        assert stats["digests"] == 1

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_stats_no_task_files(self, mock_memsearch, tmp_path):
        """stats() handles missing tasks directory gracefully."""
        mock_instance = MagicMock()
        mock_memsearch.return_value = mock_instance

        mgr = self._make_manager(str(tmp_path))
        # Don't create any directories

        stats = await mgr.stats("proj", str(tmp_path))
        assert stats["task_memories"] == 0
        assert stats["digests"] == 0


class TestMemoryConfigCompaction:
    """Tests for compaction-related MemoryConfig fields."""

    def test_compact_config_defaults(self):
        cfg = MemoryConfig()
        assert cfg.compact_enabled is False
        assert cfg.compact_interval_hours == 24
        assert cfg.compact_recent_days == 7
        assert cfg.compact_archive_days == 30

    def test_compact_config_custom(self):
        cfg = MemoryConfig(
            compact_enabled=True,
            compact_recent_days=14,
            compact_archive_days=60,
        )
        assert cfg.compact_enabled is True
        assert cfg.compact_recent_days == 14
        assert cfg.compact_archive_days == 60
