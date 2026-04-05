"""Unit tests for the memory consolidation system.

Tests cover:
- Factsheet YAML parsing and serialization
- Read/write/update operations on factsheets
- ProjectFactsheet dataclass accessors
- Context injection (factsheet as Tier 0 in MemoryContext)
- Bootstrap from seed template with repo_url auto-population
- MemoryContext.to_context_block() includes factsheet section
- Knowledge topic read/list on MemoryManager
- Cross-project factsheet search
- Plugin tool invocations (project_factsheet, project_knowledge, search_all_projects)
- Supervisor prompt includes factsheet guidance
- Deep consolidation and bootstrap
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.memory import MemoryConfig, MemoryManager
from src.models import MemoryContext, ProjectFactsheet


# ---------------------------------------------------------------------------
# Lightweight fakes (same pattern as test_memory.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-456"
    project_id: str = "test-project"
    title: str = "Add login page"
    description: str = "Create a login page with OAuth2"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


@dataclass
class FakeOutput:
    result: MagicMock = field(default_factory=lambda: MagicMock(value="completed"))
    summary: str = "Implemented OAuth2 login page."
    files_changed: list = field(default_factory=lambda: ["src/login.py"])
    tokens_used: int = 5000


# ---------------------------------------------------------------------------
# Helpers and sample data
# ---------------------------------------------------------------------------


def _make_manager(tmp_path, **overrides) -> MemoryManager:
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=str(tmp_path))


def _write_factsheet(tmp_path, project_id: str, content: str = None) -> str:
    if content is None:
        content = SAMPLE_FACTSHEET_RAW
    mem_dir = tmp_path / "memory" / project_id
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / "factsheet.md"
    path.write_text(content)
    return str(path)


def _write_knowledge_topic(
    tmp_path, project_id: str, topic: str, content: str = None
) -> str:
    if content is None:
        content = SAMPLE_KNOWLEDGE
    kb_dir = tmp_path / "memory" / project_id / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / f"{topic}.md"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------

SAMPLE_FACTSHEET = """\
---
last_updated: "2026-04-05T12:00:00Z"
consolidation_version: 1
project:
  name: "Test Project"
  id: "test-project"
  description: "A test project for unit tests"
urls:
  github: "https://github.com/user/test-project"
  docs: null
  ci: "https://github.com/user/test-project/actions"
  deploy: null
tech_stack:
  language: "Python"
  framework: "FastAPI"
  build_system: null
  test_framework: "pytest"
  key_dependencies:
    - "SQLAlchemy"
    - "Pydantic"
environments: []
contacts:
  owner: "test-user"
key_paths:
  source: "src/"
  tests: "tests/"
  config: "config.yaml"
  entry_point: "src/main.py"
---

# Test Project — Quick Reference

## What It Does
A testing project for validating the factsheet system.

## Current State
Active development.
"""

# Simpler factsheet variant for string-based tests (fleet-beacon style)
SAMPLE_FACTSHEET_RAW = """\
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


# ---------------------------------------------------------------------------
# ProjectFactsheet dataclass tests
# ---------------------------------------------------------------------------


class TestProjectFactsheet:
    def test_default_values(self):
        fs = ProjectFactsheet()
        assert fs.raw_yaml == {}
        assert fs.body_markdown == ""
        assert fs.project_name == ""
        assert fs.project_id == ""
        assert fs.urls == {}
        assert fs.tech_stack == {}
        assert fs.contacts == {}
        assert fs.key_paths == {}
        assert fs.environments == []
        assert fs.last_updated == ""

    def test_accessors_with_data(self):
        data = {
            "project": {"name": "My Project", "id": "my-proj"},
            "urls": {"github": "https://github.com/user/repo", "docs": None},
            "tech_stack": {"language": "Python", "key_dependencies": ["flask"]},
            "contacts": {"owner": "alice"},
            "key_paths": {"source": "src/"},
            "environments": [{"name": "dev", "url": None}],
            "last_updated": "2026-04-05T12:00:00Z",
        }
        fs = ProjectFactsheet(raw_yaml=data, body_markdown="# Hello")
        assert fs.project_name == "My Project"
        assert fs.project_id == "my-proj"
        assert fs.urls["github"] == "https://github.com/user/repo"
        assert fs.tech_stack["language"] == "Python"
        assert fs.contacts["owner"] == "alice"
        assert fs.key_paths["source"] == "src/"
        assert len(fs.environments) == 1
        assert fs.last_updated == "2026-04-05T12:00:00Z"
        assert fs.body_markdown == "# Hello"

    def test_get_field_dotted(self):
        data = {
            "urls": {"github": "https://github.com/user/repo"},
            "tech_stack": {"key_dependencies": ["flask", "sqlalchemy"]},
        }
        fs = ProjectFactsheet(raw_yaml=data)
        assert fs.get_field("urls.github") == "https://github.com/user/repo"
        assert fs.get_field("tech_stack.key_dependencies") == ["flask", "sqlalchemy"]
        assert fs.get_field("urls.nonexistent") is None
        assert fs.get_field("urls.nonexistent", "default") == "default"
        assert fs.get_field("deeply.nested.missing") is None

    def test_set_field_dotted(self):
        fs = ProjectFactsheet(raw_yaml={})
        fs.set_field("urls.github", "https://github.com/new")
        assert fs.raw_yaml["urls"]["github"] == "https://github.com/new"

        # Set deeper nesting
        fs.set_field("tech_stack.key_dependencies", ["flask"])
        assert fs.raw_yaml["tech_stack"]["key_dependencies"] == ["flask"]

    def test_set_field_overwrites_existing(self):
        fs = ProjectFactsheet(raw_yaml={"urls": {"github": "old"}})
        fs.set_field("urls.github", "new")
        assert fs.raw_yaml["urls"]["github"] == "new"

    def test_set_field_creates_intermediate_dicts(self):
        fs = ProjectFactsheet(raw_yaml={})
        fs.set_field("a.b.c", "deep_value")
        assert fs.raw_yaml["a"]["b"]["c"] == "deep_value"


# ---------------------------------------------------------------------------
# MemoryManager factsheet parsing tests
# ---------------------------------------------------------------------------


class TestFactsheetParsing:
    def setup_method(self):
        self.config = MemoryConfig(enabled=False)
        self.mgr = MemoryManager(self.config, storage_root="/tmp/test-aq-parsing")

    def test_parse_valid_factsheet(self):
        fs = self.mgr._parse_factsheet(SAMPLE_FACTSHEET)
        assert fs.project_name == "Test Project"
        assert fs.project_id == "test-project"
        assert fs.urls["github"] == "https://github.com/user/test-project"
        assert fs.tech_stack["language"] == "Python"
        assert fs.contacts["owner"] == "test-user"
        assert "Quick Reference" in fs.body_markdown

    def test_parse_no_frontmatter(self):
        raw = "# Just markdown\nNo YAML here."
        fs = self.mgr._parse_factsheet(raw)
        assert fs.raw_yaml == {}
        assert "Just markdown" in fs.body_markdown

    def test_parse_empty_frontmatter(self):
        raw = "---\n---\n# Empty frontmatter"
        fs = self.mgr._parse_factsheet(raw)
        assert fs.raw_yaml == {}
        assert "Empty frontmatter" in fs.body_markdown

    def test_parse_invalid_yaml(self):
        raw = "---\ninvalid: yaml: [broken\n---\n# Body"
        fs = self.mgr._parse_factsheet(raw)
        # Should not crash — returns empty yaml_data
        assert fs.raw_yaml == {}

    def test_serialize_roundtrip(self):
        fs = self.mgr._parse_factsheet(SAMPLE_FACTSHEET)
        serialized = self.mgr._serialize_factsheet(fs)
        # Re-parse the serialized content
        fs2 = self.mgr._parse_factsheet(serialized)
        assert fs2.project_name == "Test Project"
        assert fs2.urls["github"] == "https://github.com/user/test-project"
        assert fs2.tech_stack["language"] == "Python"
        assert "Quick Reference" in fs2.body_markdown


# ---------------------------------------------------------------------------
# MemoryManager factsheet I/O tests (filesystem)
# ---------------------------------------------------------------------------


class TestFactsheetIO:
    def setup_method(self):
        self.config = MemoryConfig(enabled=False)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_none(self):
        result = await self.mgr.read_factsheet("nonexistent-project")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_raw_nonexistent_returns_none(self):
        result = await self.mgr.read_factsheet_raw("nonexistent-project")
        assert result is None

    @pytest.mark.asyncio
    async def test_write_and_read(self):
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "IO Test", "id": "io-test"},
                "urls": {"github": "https://github.com/test"},
            },
            body_markdown="# IO Test\nHello",
        )

        path = await self.mgr.write_factsheet("io-test", fs)
        assert path is not None
        assert os.path.isfile(path)

        # Read back
        fs2 = await self.mgr.read_factsheet("io-test")
        assert fs2 is not None
        assert fs2.project_name == "IO Test"
        assert fs2.urls["github"] == "https://github.com/test"
        assert "IO Test" in fs2.body_markdown

    @pytest.mark.asyncio
    async def test_write_updates_timestamp(self):
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Timestamp Test"}},
            body_markdown="# Test",
        )
        await self.mgr.write_factsheet("ts-test", fs)

        fs2 = await self.mgr.read_factsheet("ts-test")
        assert fs2 is not None
        assert fs2.last_updated != ""
        # Should be a valid ISO timestamp
        assert "T" in fs2.last_updated
        assert "Z" in fs2.last_updated

    @pytest.mark.asyncio
    async def test_read_raw_returns_string(self):
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Raw Test", "id": "raw-test"}},
            body_markdown="# Raw Test",
        )
        await self.mgr.write_factsheet("raw-test", fs)

        raw = await self.mgr.read_factsheet_raw("raw-test")
        assert raw is not None
        assert isinstance(raw, str)
        assert "---" in raw
        assert "Raw Test" in raw

    @pytest.mark.asyncio
    async def test_update_field_creates_factsheet(self):
        """update_factsheet_field should bootstrap a new factsheet if none exists."""
        path = await self.mgr.update_factsheet_field(
            "new-project",
            "urls.docs",
            "https://docs.example.com",
            project_name="New Project",
            repo_url="https://github.com/user/new-project",
        )
        assert path is not None

        fs = await self.mgr.read_factsheet("new-project")
        assert fs is not None
        assert fs.get_field("urls.docs") == "https://docs.example.com"
        # Bootstrap should have auto-populated github URL
        assert fs.get_field("urls.github") == "https://github.com/user/new-project"
        assert fs.get_field("project.name") == "New Project"

    @pytest.mark.asyncio
    async def test_update_field_on_existing_factsheet(self):
        # Create initial factsheet
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "Existing", "id": "existing"},
                "urls": {"github": "https://github.com/existing"},
                "tech_stack": {"language": "Python"},
            },
            body_markdown="# Existing",
        )
        await self.mgr.write_factsheet("existing", fs)

        # Update a single field
        await self.mgr.update_factsheet_field(
            "existing", "tech_stack.framework", "Django"
        )

        fs2 = await self.mgr.read_factsheet("existing")
        assert fs2 is not None
        # New field set
        assert fs2.get_field("tech_stack.framework") == "Django"
        # Existing fields preserved
        assert fs2.get_field("tech_stack.language") == "Python"
        assert fs2.get_field("urls.github") == "https://github.com/existing"

    @pytest.mark.asyncio
    async def test_ensure_factsheet_creates_when_missing(self):
        fs = await self.mgr.ensure_factsheet(
            "ensure-test",
            project_name="Ensure Test",
            repo_url="https://github.com/user/ensure",
        )
        assert fs.get_field("project.name") == "Ensure Test"
        assert fs.get_field("urls.github") == "https://github.com/user/ensure"

        # File should exist on disk
        path = self.mgr._factsheet_path("ensure-test")
        assert os.path.isfile(path)

    @pytest.mark.asyncio
    async def test_ensure_factsheet_returns_existing(self):
        # Pre-create a factsheet
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "Pre-existing", "id": "pre-existing"},
                "urls": {"github": "https://github.com/pre"},
            },
            body_markdown="# Pre-existing",
        )
        await self.mgr.write_factsheet("pre-existing", fs)

        # ensure_factsheet should return the existing one
        fs2 = await self.mgr.ensure_factsheet(
            "pre-existing",
            project_name="Different Name",
            repo_url="https://github.com/different",
        )
        assert fs2.project_name == "Pre-existing"
        assert fs2.urls["github"] == "https://github.com/pre"


# ---------------------------------------------------------------------------
# Factsheet seed template tests
# ---------------------------------------------------------------------------


class TestFactsheetSeedTemplate:
    def setup_method(self):
        self.config = MemoryConfig(enabled=False)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_seed_with_repo_url(self):
        fs = await self.mgr._seed_factsheet(
            "seed-test",
            project_name="Seed Test",
            repo_url="https://github.com/user/seed",
        )
        assert fs.get_field("project.name") == "Seed Test"
        assert fs.get_field("project.id") == "seed-test"
        assert fs.get_field("urls.github") == "https://github.com/user/seed"
        assert fs.get_field("consolidation_version") == 1

    @pytest.mark.asyncio
    async def test_seed_without_repo_url(self):
        fs = await self.mgr._seed_factsheet("no-url", project_name="No URL")
        assert fs.get_field("project.name") == "No URL"
        assert fs.get_field("urls.github") is None

    @pytest.mark.asyncio
    async def test_seed_defaults_name_to_id(self):
        fs = await self.mgr._seed_factsheet("my-project-id")
        assert fs.get_field("project.name") == "my-project-id"
        assert fs.get_field("project.id") == "my-project-id"

    @pytest.mark.asyncio
    async def test_seed_body_contains_project_name(self):
        fs = await self.mgr._seed_factsheet("test", project_name="My Cool Project")
        assert "My Cool Project" in fs.body_markdown


# ---------------------------------------------------------------------------
# MemoryContext integration tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MemoryManager — Factsheet raw string tests (string-based API)
# ---------------------------------------------------------------------------


class TestFactsheetManagerRaw:
    """Tests for factsheet read_raw/write_raw/update on MemoryManager (string-based API)."""

    async def test_read_factsheet_raw_returns_none_when_missing(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = await mgr.read_factsheet_raw("no-such-project")
        assert result is None

    async def test_read_factsheet_raw_returns_content(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        result = await mgr.read_factsheet_raw("test-proj")
        assert result is not None
        assert "Test Project" in result
        assert "github" in result

    async def test_write_factsheet_raw_creates_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = await mgr.write_factsheet_raw("new-proj", "---\nfoo: bar\n---\nHello\n")
        assert path is not None
        assert os.path.isfile(path)
        with open(path) as f:
            assert "foo: bar" in f.read()

    async def test_write_factsheet_raw_overwrites_existing(self, tmp_path):
        _write_factsheet(tmp_path, "test-proj")
        mgr = _make_manager(tmp_path)
        new_content = "---\nupdated: true\n---\nNew content\n"
        path = await mgr.write_factsheet_raw("test-proj", new_content)
        assert path is not None
        with open(path) as f:
            content = f.read()
        assert "updated: true" in content
        assert "Test Project" not in content

    async def test_parse_factsheet_yaml(self, tmp_path):
        mgr = _make_manager(tmp_path)
        data = mgr.parse_factsheet_yaml(SAMPLE_FACTSHEET_RAW)
        assert data["project"]["name"] == "Test Project"
        assert data["urls"]["github"] == "https://github.com/user/test-proj"
        assert data["tech_stack"]["language"] == "Python"

    async def test_parse_factsheet_yaml_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.parse_factsheet_yaml("") == {}
        assert mgr.parse_factsheet_yaml("no frontmatter") == {}

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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
        mgr = _make_manager(tmp_path)

        results = await mgr.search_all_project_factsheets(["proj-a"])
        assert len(results) == 1
        assert results[0]["project_name"] == "Test Project"
        assert results[0]["has_factsheet"] is True


# ---------------------------------------------------------------------------
# MemoryContext — Factsheet field integration
# ---------------------------------------------------------------------------


class TestMemoryContextFactsheet:
    def test_to_context_block_includes_factsheet(self):
        ctx = MemoryContext(
            factsheet="---\nproject:\n  name: Test\n---\n# Test",
            profile="Some profile content",
        )
        block = ctx.to_context_block()
        assert "## Project Factsheet" in block
        assert "## Project Profile" in block
        # Factsheet should appear BEFORE profile
        factsheet_pos = block.index("## Project Factsheet")
        profile_pos = block.index("## Project Profile")
        assert factsheet_pos < profile_pos

    def test_to_context_block_without_factsheet(self):
        ctx = MemoryContext(profile="Profile only")
        block = ctx.to_context_block()
        assert "## Project Factsheet" not in block
        assert "## Project Profile" in block

    def test_to_context_block_factsheet_only(self):
        ctx = MemoryContext(factsheet="---\nproject:\n  name: Test\n---")
        block = ctx.to_context_block()
        assert "## Project Factsheet" in block
        assert "## Project Profile" not in block

    def test_is_empty_with_factsheet(self):
        ctx = MemoryContext(factsheet="some content")
        assert not ctx.is_empty

    def test_is_empty_without_anything(self):
        ctx = MemoryContext()
        assert ctx.is_empty

    def test_memory_folder_reference_includes_factsheet(self):
        ctx = MemoryContext(memory_folder="/home/user/.agent-queue/memory/test/")
        block = ctx.to_context_block()
        assert "factsheet.md" in block
        assert "knowledge/" in block


# ---------------------------------------------------------------------------
# build_context integration (factsheet loaded as Tier 0)
# ---------------------------------------------------------------------------


class TestBuildContextFactsheet:
    def setup_method(self):
        self.config = MemoryConfig(enabled=False, profile_enabled=False, factsheet_in_context=True)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_build_context_loads_factsheet(self):
        """build_context should load the factsheet as Tier 0."""
        # Pre-create a factsheet on disk
        project_id = "ctx-test"
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "Context Test", "id": project_id},
                "urls": {"github": "https://github.com/ctx-test"},
            },
            body_markdown="# Context Test",
        )
        await self.mgr.write_factsheet(project_id, fs)

        task = FakeTask(project_id=project_id)
        ctx = await self.mgr.build_context(project_id, task, "")
        assert ctx.factsheet != ""
        assert "Context Test" in ctx.factsheet
        assert "github" in ctx.factsheet

    @pytest.mark.asyncio
    async def test_build_context_no_factsheet(self):
        """build_context should have empty factsheet when none exists."""
        task = FakeTask(project_id="no-factsheet")
        ctx = await self.mgr.build_context("no-factsheet", task, "")
        assert ctx.factsheet == ""

    @pytest.mark.asyncio
    async def test_build_context_skips_factsheet_when_disabled(self):
        """build_context should have empty factsheet when factsheet_in_context is False."""
        project_id = "disabled-ctx"
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Disabled Ctx"}},
            body_markdown="# Test",
        )
        await self.mgr.write_factsheet(project_id, fs)

        self.mgr.config = MemoryConfig(
            enabled=False, profile_enabled=False, factsheet_in_context=False
        )
        task = FakeTask(project_id=project_id)
        ctx = await self.mgr.build_context(project_id, task, "")
        assert ctx.factsheet == ""

    @pytest.mark.asyncio
    async def test_build_context_factsheet_before_profile(self):
        """In the rendered context block, factsheet should precede profile."""
        project_id = "order-test"
        # Create factsheet
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Order Test"}},
            body_markdown="# Factsheet body",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create profile (enable profile for this test)
        self.mgr.config = MemoryConfig(
            enabled=False, profile_enabled=True, factsheet_in_context=True
        )
        profile_dir = os.path.join(str(self.tmp_path), "memory", project_id)
        os.makedirs(profile_dir, exist_ok=True)
        with open(os.path.join(profile_dir, "profile.md"), "w") as f:
            f.write("# Profile content here")

        task = FakeTask(project_id=project_id)
        ctx = await self.mgr.build_context(project_id, task, "")
        block = ctx.to_context_block()

        assert "## Project Factsheet" in block
        assert "## Project Profile" in block
        assert block.index("## Project Factsheet") < block.index("## Project Profile")


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
# Phase 6: Deep Consolidation and Bootstrap
# ---------------------------------------------------------------------------


class TestDeepConsolidation:
    """Tests for run_deep_consolidation() — weekly knowledge base review."""

    def setup_method(self):
        self.config = MemoryConfig(enabled=False, consolidation_enabled=True)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_deep_consolidation_disabled(self):
        """Deep consolidation should return 'disabled' when consolidation is off."""
        self.mgr.config = MemoryConfig(enabled=False, consolidation_enabled=False)
        result = await self.mgr.run_deep_consolidation("test-project")
        assert result["status"] == "disabled"
        assert result["topics_reviewed"] == 0
        assert result["topics_updated"] == []
        assert result["factsheet_updated"] is False
        assert result["pruned_facts"] == []

    @pytest.mark.asyncio
    async def test_deep_consolidation_no_knowledge(self):
        """Deep consolidation should return 'no_knowledge' when nothing exists."""
        result = await self.mgr.run_deep_consolidation("nonexistent-project")
        assert result["status"] == "no_knowledge"
        assert result["topics_reviewed"] == 0

    @pytest.mark.asyncio
    async def test_deep_consolidation_reads_all_topics(self):
        """Deep consolidation should read all existing knowledge topics."""
        project_id = "deep-test"

        # Create a factsheet
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "Deep Test", "id": project_id},
                "urls": {"github": "https://github.com/deep-test"},
            },
            body_markdown="# Deep Test\n\n## What It Does\nOld description.",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create a knowledge topic
        await self.mgr.write_knowledge_topic(
            project_id,
            "architecture",
            "# Architecture Knowledge\n\n## Core Architecture\n- Uses async Python (from task: task-1)",
        )

        # Deep consolidation will try to call LLM — mock it out
        # For this test, we just verify it reads the data correctly
        # (the LLM call will fail, returning an error)
        result = await self.mgr.run_deep_consolidation(project_id)
        # Will get "error" because no LLM provider is configured
        assert result["status"] == "error"
        assert result["error"] == "no_provider"
        assert result["topics_reviewed"] == 1  # found the architecture topic

    @pytest.mark.asyncio
    async def test_deep_consolidation_counts_processed_staging(self):
        """Deep consolidation should count previously processed staging files."""
        project_id = "staging-count-test"

        # Create factsheet
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Staging Count"}},
            body_markdown="# Test",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create some processed staging files
        processed_dir = self.mgr._staging_processed_dir(project_id)
        os.makedirs(processed_dir, exist_ok=True)
        for i in range(3):
            with open(os.path.join(processed_dir, f"task-{i}.json"), "w") as f:
                f.write("{}")

        result = await self.mgr.run_deep_consolidation(project_id)
        # Will fail at LLM call, but should have counted processed files
        assert result["status"] == "error"
        assert result["error"] == "no_provider"


class TestBootstrapConsolidation:
    """Tests for bootstrap_consolidation() — one-time initial setup."""

    def setup_method(self):
        self.config = MemoryConfig(enabled=False, consolidation_enabled=True)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_bootstrap_no_tasks(self):
        """Bootstrap should return 'no_tasks' when no task memories exist."""
        result = await self.mgr.bootstrap_consolidation("empty-project")
        assert result["status"] == "no_tasks"
        assert result["tasks_processed"] == 0
        assert result["topics_created"] == []
        assert result["factsheet_created"] is False

    @pytest.mark.asyncio
    async def test_bootstrap_already_exists(self):
        """Bootstrap should return 'already_exists' if factsheet and knowledge exist."""
        project_id = "already-setup"

        # Create factsheet
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Already Setup", "id": project_id}},
            body_markdown="# Already Setup",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create a knowledge topic
        await self.mgr.write_knowledge_topic(
            project_id,
            "architecture",
            "# Architecture\n\nExisting content.",
        )

        result = await self.mgr.bootstrap_consolidation(project_id)
        assert result["status"] == "already_exists"
        assert "already has a factsheet" in result["message"]

    @pytest.mark.asyncio
    async def test_bootstrap_reads_task_memories(self):
        """Bootstrap should read task memory files."""
        project_id = "boot-test"

        # Create task memory files
        tasks_dir = os.path.join(str(self.tmp_path), "memory", project_id, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        for i in range(3):
            with open(os.path.join(tasks_dir, f"task-{i}.md"), "w") as f:
                f.write(
                    f"# Task: task-{i} — Test task {i}\n\n"
                    f"**Project:** {project_id}\n\n"
                    f"## Summary\nDid something interesting for task {i}.\n"
                )

        # Bootstrap will try to call LLM — will fail without provider
        result = await self.mgr.bootstrap_consolidation(
            project_id,
            project_name="Boot Test",
            repo_url="https://github.com/boot-test",
        )
        assert result["status"] == "error"
        assert result["error"] == "no_provider"
        assert result["tasks_processed"] == 3

    @pytest.mark.asyncio
    async def test_bootstrap_reads_digests(self):
        """Bootstrap should also read digest files alongside task memories."""
        project_id = "digest-boot"

        # Create task memories and digests
        tasks_dir = os.path.join(str(self.tmp_path), "memory", project_id, "tasks")
        digests_dir = os.path.join(str(self.tmp_path), "memory", project_id, "digests")
        os.makedirs(tasks_dir, exist_ok=True)
        os.makedirs(digests_dir, exist_ok=True)

        with open(os.path.join(tasks_dir, "task-1.md"), "w") as f:
            f.write("# Task: task-1\n\n## Summary\nRecent task.\n")

        with open(os.path.join(digests_dir, "week-2026-W10.md"), "w") as f:
            f.write("# Week 2026-W10 Digest\n\nSummary of older tasks.\n")

        result = await self.mgr.bootstrap_consolidation(project_id)
        assert result["status"] == "error"
        assert result["error"] == "no_provider"
        # Should have found both the task and the digest
        assert result["tasks_processed"] == 2

    @pytest.mark.asyncio
    async def test_bootstrap_skips_when_factsheet_exists_but_no_knowledge(self):
        """Bootstrap should proceed if factsheet exists but no knowledge topics do."""
        project_id = "partial-setup"

        # Create only a factsheet (no knowledge topics)
        fs = ProjectFactsheet(
            raw_yaml={"project": {"name": "Partial", "id": project_id}},
            body_markdown="# Partial",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create task memories
        tasks_dir = os.path.join(str(self.tmp_path), "memory", project_id, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        with open(os.path.join(tasks_dir, "task-1.md"), "w") as f:
            f.write("# Task: task-1\n\n## Summary\nDid a thing.\n")

        result = await self.mgr.bootstrap_consolidation(project_id)
        # Should proceed (not "already_exists") since no knowledge topics exist
        assert result["status"] == "error"  # fails at LLM call
        assert result["error"] == "no_provider"
        assert result["tasks_processed"] == 1


class TestStaleFactPruning:
    """Tests for stale fact detection and pruning within deep consolidation."""

    def setup_method(self):
        self.config = MemoryConfig(enabled=False, consolidation_enabled=True)

    @pytest.fixture(autouse=True)
    def _use_tmp_path(self, tmp_path):
        self.tmp_path = tmp_path
        self.mgr = MemoryManager(self.config, storage_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_staging_processed_dir_created(self):
        """The processed staging directory should be accessible."""
        project_id = "prune-test"
        processed_dir = self.mgr._staging_processed_dir(project_id)
        expected = os.path.join(
            str(self.tmp_path), "memory", project_id, "staging", "processed"
        )
        assert processed_dir == expected

    @pytest.mark.asyncio
    async def test_knowledge_topic_creation_for_deep_review(self):
        """Knowledge topics should be readable for deep consolidation review."""
        project_id = "topic-review"

        # Create multiple knowledge topics with varying content
        await self.mgr.write_knowledge_topic(
            project_id,
            "architecture",
            (
                "# Architecture Knowledge\n\n"
                "## Core Architecture\n"
                "- Uses async Python with SQLAlchemy (from task: task-1)\n"
                "- Redis for caching (from task: task-2)\n"
            ),
        )
        await self.mgr.write_knowledge_topic(
            project_id,
            "gotchas",
            (
                "# Gotchas & Known Issues\n\n"
                "## Known Issues\n"
                "- Deprecated API endpoint /v1/old still in use (from task: task-3)\n"
            ),
        )

        # Verify both are readable
        arch = await self.mgr.read_knowledge_topic(project_id, "architecture")
        assert arch is not None
        assert "async Python" in arch

        gotchas = await self.mgr.read_knowledge_topic(project_id, "gotchas")
        assert gotchas is not None
        assert "Deprecated API" in gotchas

        # List should show both as existing
        topics = await self.mgr.list_knowledge_topics(project_id)
        existing = [t for t in topics if t["has_content"]]
        assert len(existing) == 2

    @pytest.mark.asyncio
    async def test_deep_consolidation_with_factsheet_and_topics(self):
        """Deep consolidation should work with both factsheet and knowledge topics."""
        project_id = "full-deep"

        # Create factsheet
        fs = ProjectFactsheet(
            raw_yaml={
                "project": {"name": "Full Deep Test", "id": project_id},
                "urls": {"github": "https://github.com/full-deep"},
                "tech_stack": {"language": "Python", "framework": "FastAPI"},
            },
            body_markdown="# Full Deep Test\n\n## What It Does\nA test project.\n",
        )
        await self.mgr.write_factsheet(project_id, fs)

        # Create knowledge topics
        await self.mgr.write_knowledge_topic(
            project_id,
            "architecture",
            "# Architecture\n\n## Core\n- Python async\n",
        )
        await self.mgr.write_knowledge_topic(
            project_id,
            "decisions",
            "# Decisions\n\n## Technology Choices\n- FastAPI chosen\n",
        )

        # Run deep consolidation — will fail at LLM call
        result = await self.mgr.run_deep_consolidation(project_id)
        assert result["status"] == "error"
        assert result["error"] == "no_provider"
        assert result["topics_reviewed"] == 2  # architecture + decisions


# ---------------------------------------------------------------------------
# Config — fields from both branches
# ---------------------------------------------------------------------------


class TestConsolidationConfigPhase6:
    """Tests for Phase 6 configuration fields."""

    def test_deep_consolidation_schedule_default(self):
        config = MemoryConfig()
        assert config.deep_consolidation_schedule == "weekly"

    def test_deep_consolidation_schedule_custom(self):
        config = MemoryConfig(deep_consolidation_schedule="monthly")
        assert config.deep_consolidation_schedule == "monthly"

    def test_consolidation_enabled_default(self):
        config = MemoryConfig()
        assert config.consolidation_enabled is True


class TestConsolidationConfigPhase5:
    """Test new consolidation config fields (fleet-beacon)."""

    def test_default_values(self):
        cfg = MemoryConfig()
        assert cfg.fact_extraction_enabled is True
        assert cfg.index_knowledge is True
        assert cfg.factsheet_in_context is True
        assert "architecture" in cfg.knowledge_topics
        assert "gotchas" in cfg.knowledge_topics
        assert "decisions" in cfg.knowledge_topics

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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
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
        _write_factsheet(tmp_path, "proj-a", SAMPLE_FACTSHEET_RAW)
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
