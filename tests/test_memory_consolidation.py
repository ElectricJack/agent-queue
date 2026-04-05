"""Tests for the memory consolidation system (Phase 5): agent tools and query patterns.

Tests cover:
- Factsheet read/write/update on MemoryManager
- Knowledge topic read/list on MemoryManager
- Cross-project factsheet search
- Plugin tool invocations (project_factsheet, project_knowledge, search_all_projects)
- MemoryContext factsheet integration
- Supervisor prompt includes factsheet guidance
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MemoryConfig
from src.memory import MemoryManager
from src.models import MemoryContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_FACTSHEET = """\
---
last_updated: "2026-04-05T14:30:00Z"
project:
  name: "Test Project"
  id: "test-proj"
urls:
  github: "https://github.com/user/test-proj"
  docs: null
tech_stack:
  language: "Python"
  framework: "FastAPI"
  key_dependencies:
    - "SQLAlchemy"
    - "pydantic"
---

# Test Project — Quick Reference

## What It Does
A test project for unit testing.
"""

SAMPLE_KNOWLEDGE = """\
# Architecture Knowledge

> Last consolidated: 2026-04-05 | Sources: 3 tasks

## Core Architecture
- Event-driven async system with SQLAlchemy backend
  - *Source: task vivid-flare (2026-03-20)*
"""


def _make_manager(tmp_path, **overrides) -> MemoryManager:
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=str(tmp_path))


def _write_factsheet(tmp_path, project_id: str, content: str = SAMPLE_FACTSHEET) -> str:
    mem_dir = tmp_path / "memory" / project_id
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / "factsheet.md"
    path.write_text(content)
    return str(path)


def _write_knowledge_topic(
    tmp_path, project_id: str, topic: str, content: str = SAMPLE_KNOWLEDGE
) -> str:
    kb_dir = tmp_path / "memory" / project_id / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / f"{topic}.md"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# MemoryManager — Factsheet tests
# ---------------------------------------------------------------------------


class TestFactsheetManager:
    """Tests for factsheet read/write/update on MemoryManager."""

    async def test_read_factsheet_returns_none_when_missing(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = await mgr.read_factsheet("no-such-project")
        assert result is None

    async def test_read_factsheet_returns_content(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        result = await mgr.read_factsheet("test-proj")
        assert result is not None
        assert "Test Project" in result
        assert "github" in result

    async def test_write_factsheet_creates_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = await mgr.write_factsheet("new-proj", "---\nfoo: bar\n---\nHello\n")
        assert path is not None
        assert os.path.isfile(path)
        with open(path) as f:
            assert "foo: bar" in f.read()

    async def test_write_factsheet_overwrites_existing(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        new_content = "---\nupdated: true\n---\nNew content\n"
        path = await mgr.write_factsheet("test-proj", new_content)
        assert path is not None
        with open(path) as f:
            content = f.read()
        assert "updated: true" in content
        assert "Test Project" not in content

    async def test_parse_factsheet_yaml(self, tmp_path):
        mgr = _make_manager(tmp_path)
        data = mgr.parse_factsheet_yaml(SAMPLE_FACTSHEET)
        assert data["project"]["name"] == "Test Project"
        assert data["urls"]["github"] == "https://github.com/user/test-proj"
        assert data["tech_stack"]["language"] == "Python"

    async def test_parse_factsheet_yaml_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.parse_factsheet_yaml("") == {}
        assert mgr.parse_factsheet_yaml("no frontmatter") == {}

    async def test_update_factsheet_field_creates_new(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = await mgr.update_factsheet_field(
            "new-proj", "urls.github", "https://github.com/user/new-proj"
        )
        assert path is not None
        content = await mgr.read_factsheet("new-proj")
        assert content is not None
        data = mgr.parse_factsheet_yaml(content)
        assert data["urls"]["github"] == "https://github.com/user/new-proj"

    async def test_update_factsheet_field_merges(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        path = await mgr.update_factsheet_field(
            "test-proj", "urls.docs", "https://docs.example.com"
        )
        assert path is not None
        content = await mgr.read_factsheet("test-proj")
        data = mgr.parse_factsheet_yaml(content)
        # Original github URL preserved
        assert data["urls"]["github"] == "https://github.com/user/test-proj"
        # New docs URL added
        assert data["urls"]["docs"] == "https://docs.example.com"

    async def test_update_factsheet_field_preserves_body(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        await mgr.update_factsheet_field("test-proj", "tech_stack.language", "Rust")
        content = await mgr.read_factsheet("test-proj")
        assert "Quick Reference" in content  # markdown body preserved

    async def test_factsheet_path(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = mgr._factsheet_path("my-proj")
        assert path.endswith("memory/my-proj/factsheet.md")


# ---------------------------------------------------------------------------
# MemoryManager — Knowledge Base tests
# ---------------------------------------------------------------------------


class TestKnowledgeBaseManager:
    """Tests for knowledge topic read/list on MemoryManager."""

    async def test_read_knowledge_topic_returns_none_when_missing(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = await mgr.read_knowledge_topic("test-proj", "architecture")
        assert result is None

    async def test_read_knowledge_topic_returns_content(self, tmp_path):
        _write_knowledge_topic(tmp_path, "test-proj", "architecture")
        mgr = _make_manager(tmp_path)
        result = await mgr.read_knowledge_topic("test-proj", "architecture")
        assert result is not None
        assert "Event-driven async system" in result

    async def test_read_knowledge_topic_sanitizes_path(self, tmp_path):
        """Prevent directory traversal via topic name."""
        mgr = _make_manager(tmp_path)
        result = await mgr.read_knowledge_topic("test-proj", "../../../etc/passwd")
        assert result is None

    async def test_list_knowledge_topics_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        topics = await mgr.list_knowledge_topics("test-proj")
        # Should list configured topics, all without content
        assert len(topics) == len(mgr.config.knowledge_topics)
        assert all(not t["has_content"] for t in topics)

    async def test_list_knowledge_topics_with_content(self, tmp_path):
        _write_knowledge_topic(tmp_path, "test-proj", "architecture")
        _write_knowledge_topic(tmp_path, "test-proj", "gotchas")
        mgr = _make_manager(tmp_path)
        topics = await mgr.list_knowledge_topics("test-proj")

        topic_map = {t["topic"]: t for t in topics}
        assert topic_map["architecture"]["has_content"] is True
        assert topic_map["architecture"]["size_bytes"] > 0
        assert topic_map["gotchas"]["has_content"] is True
        assert topic_map["deployment"]["has_content"] is False

    async def test_list_knowledge_topics_includes_extras(self, tmp_path):
        """Extra topic files on disk (not in config) should be included."""
        _write_knowledge_topic(tmp_path, "test-proj", "custom-topic")
        mgr = _make_manager(tmp_path)
        topics = await mgr.list_knowledge_topics("test-proj")
        topic_names = [t["topic"] for t in topics]
        assert "custom-topic" in topic_names
        custom = next(t for t in topics if t["topic"] == "custom-topic")
        assert custom.get("extra") is True

    async def test_knowledge_dir_path(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = mgr._knowledge_dir("my-proj")
        assert path.endswith("memory/my-proj/knowledge")


# ---------------------------------------------------------------------------
# MemoryManager — Cross-project search
# ---------------------------------------------------------------------------


class TestCrossProjectSearch:
    """Tests for search_all_project_factsheets."""

    async def test_search_by_text_query(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        _write_factsheet(
            tmp_path,
            "proj-b",
            "---\nproject:\n  name: Other Project\ntech_stack:\n  language: Rust\n---\n",
        )
        mgr = _make_manager(tmp_path)

        # Search for Python — only proj-a has it
        results = await mgr.search_all_project_factsheets(
            ["proj-a", "proj-b"], query="Python"
        )
        assert len(results) == 1
        assert results[0]["project_id"] == "proj-a"

    async def test_search_by_field(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        _write_factsheet(
            tmp_path,
            "proj-b",
            "---\nurls:\n  github: https://github.com/user/proj-b\n---\n",
        )
        mgr = _make_manager(tmp_path)

        results = await mgr.search_all_project_factsheets(
            ["proj-a", "proj-b"], field="urls.github"
        )
        assert len(results) == 2
        urls = {r["project_id"]: r["field_value"] for r in results}
        assert urls["proj-a"] == "https://github.com/user/test-proj"
        assert urls["proj-b"] == "https://github.com/user/proj-b"

    async def test_search_field_missing(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        mgr = _make_manager(tmp_path)

        # Field that doesn't exist
        results = await mgr.search_all_project_factsheets(
            ["proj-a"], field="environments.production"
        )
        assert len(results) == 0

    async def test_search_no_factsheets(self, tmp_path):
        mgr = _make_manager(tmp_path)
        results = await mgr.search_all_project_factsheets(
            ["no-such-proj"], query="anything"
        )
        assert len(results) == 0

    async def test_search_no_query_returns_summary(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        mgr = _make_manager(tmp_path)

        results = await mgr.search_all_project_factsheets(["proj-a"])
        assert len(results) == 1
        assert results[0]["project_name"] == "Test Project"
        assert results[0]["has_factsheet"] is True


# ---------------------------------------------------------------------------
# MemoryContext — Factsheet field integration
# ---------------------------------------------------------------------------


class TestMemoryContextFactsheet:
    """Tests for the factsheet field in MemoryContext."""

    def test_factsheet_appears_first_in_context_block(self):
        ctx = MemoryContext(
            factsheet="---\nproject:\n  name: Test\n---\n",
            profile="# Profile\nSome profile content",
        )
        block = ctx.to_context_block()
        # Factsheet should appear before profile
        fs_idx = block.index("Project Factsheet")
        prof_idx = block.index("Project Profile")
        assert fs_idx < prof_idx

    def test_factsheet_not_empty(self):
        ctx = MemoryContext(factsheet="some content")
        assert not ctx.is_empty

    def test_context_block_without_factsheet(self):
        ctx = MemoryContext(profile="# Profile\nContent")
        block = ctx.to_context_block()
        assert "Factsheet" not in block

    def test_memory_reference_includes_factsheet_path(self):
        ctx = MemoryContext(memory_folder="/home/user/.agent-queue/memory/test-proj/")
        block = ctx.to_context_block()
        assert "factsheet.md" in block
        assert "knowledge/" in block


# ---------------------------------------------------------------------------
# build_context — Factsheet tier integration
# ---------------------------------------------------------------------------


class TestBuildContextFactsheet:
    """Test that build_context includes factsheet as Tier 0."""

    async def test_build_context_includes_factsheet(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path, factsheet_in_context=True)

        @dataclass
        class FakeTask:
            project_id: str = "test-proj"
            title: str = "Test task"
            description: str = "A test task"

        ctx = await mgr.build_context("test-proj", FakeTask(), str(tmp_path))
        assert ctx.factsheet != ""
        assert "Test Project" in ctx.factsheet

    async def test_build_context_skips_factsheet_when_disabled(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path, factsheet_in_context=False)

        @dataclass
        class FakeTask:
            project_id: str = "test-proj"
            title: str = "Test task"
            description: str = "A test task"

        ctx = await mgr.build_context("test-proj", FakeTask(), str(tmp_path))
        assert ctx.factsheet == ""


# ---------------------------------------------------------------------------
# Memory paths — Knowledge directory indexing
# ---------------------------------------------------------------------------


class TestMemoryPathsKnowledge:
    """Test that knowledge/ directory is included in memory paths when enabled."""

    def test_memory_paths_includes_knowledge_when_enabled(self, tmp_path):
        kb_dir = tmp_path / "memory" / "test-proj" / "knowledge"
        kb_dir.mkdir(parents=True)
        mgr = _make_manager(tmp_path, index_knowledge=True)
        paths = mgr._memory_paths("test-proj", str(tmp_path))
        assert str(kb_dir) in paths

    def test_memory_paths_excludes_knowledge_when_disabled(self, tmp_path):
        kb_dir = tmp_path / "memory" / "test-proj" / "knowledge"
        kb_dir.mkdir(parents=True)
        mgr = _make_manager(tmp_path, index_knowledge=False)
        paths = mgr._memory_paths("test-proj", str(tmp_path))
        assert str(kb_dir) not in paths

    def test_memory_paths_skips_missing_knowledge_dir(self, tmp_path):
        mgr = _make_manager(tmp_path, index_knowledge=True)
        paths = mgr._memory_paths("test-proj", str(tmp_path))
        # Only memory dir, no knowledge dir
        knowledge_paths = [p for p in paths if "knowledge" in p]
        assert len(knowledge_paths) == 0


# ---------------------------------------------------------------------------
# Config — New fields
# ---------------------------------------------------------------------------


class TestConsolidationConfig:
    """Test new consolidation config fields."""

    def test_default_values(self):
        cfg = MemoryConfig()
        assert cfg.consolidation_enabled is False
        assert cfg.fact_extraction_enabled is True
        assert cfg.index_knowledge is True
        assert cfg.factsheet_in_context is True
        assert cfg.consolidation_schedule == "0 3 * * *"
        assert cfg.deep_consolidation_schedule == "0 4 * * 0"
        assert "architecture" in cfg.knowledge_topics
        assert "gotchas" in cfg.knowledge_topics
        assert "decisions" in cfg.knowledge_topics
        assert len(cfg.knowledge_topics) == 7

    def test_custom_topics(self):
        cfg = MemoryConfig(knowledge_topics=("custom-a", "custom-b"))
        assert cfg.knowledge_topics == ("custom-a", "custom-b")


# ---------------------------------------------------------------------------
# Plugin tool handler tests (MemoryPlugin)
# ---------------------------------------------------------------------------


def _try_import_memory_plugin():
    """Try to import MemoryPlugin, skip tests if import chain fails."""
    try:
        from src.plugins.internal.memory import MemoryPlugin
        return MemoryPlugin
    except (ImportError, ModuleNotFoundError):
        return None


_MemoryPlugin = _try_import_memory_plugin()
_skip_plugin = pytest.mark.skipif(
    _MemoryPlugin is None,
    reason="MemoryPlugin import chain unavailable (e.g. tomllib on Python <3.11)",
)


def _make_plugin(tmp_path):
    """Create a MemoryPlugin with mocked services."""
    plugin = _MemoryPlugin()
    plugin._db = MagicMock()
    plugin._mem = _make_manager(tmp_path)
    plugin._ctx = MagicMock()
    # Mock _require_workspace to return a path
    async def mock_require_ws(project_id):
        return str(tmp_path), None
    plugin._require_workspace = mock_require_ws
    return plugin


@_skip_plugin
class TestMemoryPluginFactsheetTool:
    """Tests for the project_factsheet command handler."""

    async def test_factsheet_view_missing(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet({"project_id": "test-proj"})
        assert result["factsheet"] is None
        assert "message" in result

    async def test_factsheet_view_existing(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet(
            {"project_id": "test-proj", "action": "view"}
        )
        assert result["factsheet"] is not None
        assert "Test Project" in result["factsheet"]
        assert result["yaml_data"]["project"]["name"] == "Test Project"

    async def test_factsheet_update_with_content(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet({
            "project_id": "test-proj",
            "action": "update",
            "content": "---\nfoo: bar\n---\nNew content\n",
        })
        assert result["status"] == "factsheet_updated"
        assert result["path"] is not None

    async def test_factsheet_update_with_field_updates(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet({
            "project_id": "test-proj",
            "action": "update",
            "updates": {"urls.docs": "https://docs.example.com"},
        })
        assert result["status"] == "factsheet_fields_updated"
        assert "urls.docs" in result["fields_updated"]

    async def test_factsheet_update_requires_content_or_updates(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet({
            "project_id": "test-proj",
            "action": "update",
        })
        assert "error" in result

    async def test_factsheet_requires_project_id(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_factsheet({})
        assert "error" in result
        assert "project_id" in result["error"]


@_skip_plugin
class TestMemoryPluginKnowledgeTool:
    """Tests for the project_knowledge command handler."""

    async def test_knowledge_list(self, tmp_path):
        _write_knowledge_topic(tmp_path, "test-proj", "architecture")
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_knowledge({
            "project_id": "test-proj",
            "action": "list",
        })
        assert result["total"] > 0
        assert result["with_content"] >= 1

    async def test_knowledge_read_existing(self, tmp_path):
        _write_knowledge_topic(tmp_path, "test-proj", "architecture")
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_knowledge({
            "project_id": "test-proj",
            "action": "read",
            "topic": "architecture",
        })
        assert result["content"] is not None
        assert "Event-driven" in result["content"]

    async def test_knowledge_read_missing(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_knowledge({
            "project_id": "test-proj",
            "action": "read",
            "topic": "deployment",
        })
        assert result["content"] is None
        assert "message" in result

    async def test_knowledge_read_requires_topic(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_knowledge({
            "project_id": "test-proj",
            "action": "read",
        })
        assert "error" in result

    async def test_knowledge_requires_project_id(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_project_knowledge({})
        assert "error" in result


@_skip_plugin
class TestMemoryPluginSearchAllProjects:
    """Tests for the search_all_projects command handler."""

    async def test_search_requires_query_or_field(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        result = await plugin.cmd_search_all_projects({})
        assert "error" in result

    async def test_search_by_query(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        plugin = _make_plugin(tmp_path)

        # Mock list_projects to return our test project
        @dataclass
        class FakeProject:
            id: str
            status: str = "ACTIVE"

        plugin._db.list_projects = AsyncMock(
            return_value=[FakeProject(id="proj-a")]
        )

        result = await plugin.cmd_search_all_projects({"query": "Python"})
        assert result["matches"] >= 1
        assert result["projects_searched"] == 1

    async def test_search_by_field(self, tmp_path):
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET)
        _write_factsheet(
            tmp_path,
            "proj-b",
            "---\nurls:\n  github: https://github.com/user/proj-b\n---\n",
        )
        plugin = _make_plugin(tmp_path)

        @dataclass
        class FakeProject:
            id: str
            status: str = "ACTIVE"

        plugin._db.list_projects = AsyncMock(
            return_value=[FakeProject(id="proj-a"), FakeProject(id="proj-b")]
        )

        result = await plugin.cmd_search_all_projects({"field": "urls.github"})
        assert result["matches"] == 2

    async def test_search_no_projects(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        plugin._db.list_projects = AsyncMock(return_value=[])

        result = await plugin.cmd_search_all_projects({"query": "Python"})
        assert result["results"] == []
        assert "No active projects" in result.get("message", "")


# ---------------------------------------------------------------------------
# Supervisor prompt — Factsheet guidance
# ---------------------------------------------------------------------------


class TestSupervisorPromptFactsheetGuidance:
    """Test that the supervisor prompt references factsheet and knowledge base."""

    def test_supervisor_prompt_mentions_factsheet(self):
        prompt_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "src",
            "prompts",
            "supervisor_system.md",
        )
        with open(prompt_path) as f:
            content = f.read()

        # Verify factsheet guidance is present
        assert "project_factsheet" in content
        assert "project_knowledge" in content
        assert "search_all_projects" in content
        assert "Factsheet First" in content or "factsheet" in content.lower()

    def test_supervisor_prompt_metadata_lookup_order(self):
        prompt_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "src",
            "prompts",
            "supervisor_system.md",
        )
        with open(prompt_path) as f:
            content = f.read()

        # Factsheet should be mentioned before creating tasks for metadata
        assert content.index("factsheet") >= 0  # factsheet is mentioned
        assert "Create a task" in content or "create a task" in content
        # The guidance should mention checking factsheet before creating tasks
        assert "Never create a task for a metadata question" in content or \
               "factsheet" in content.lower()
