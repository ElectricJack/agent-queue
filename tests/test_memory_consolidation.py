"""Unit tests for the memory consolidation system — Phase 1: Project Factsheet.

Tests cover:
- Factsheet YAML parsing and serialization
- Read/write/update operations on factsheets
- ProjectFactsheet dataclass accessors
- Context injection (factsheet as Tier 0 in MemoryContext)
- Bootstrap from seed template with repo_url auto-population
- MemoryContext.to_context_block() includes factsheet section
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock

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
# Sample factsheet content for test fixtures
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


# ---------------------------------------------------------------------------
# build_context integration (factsheet loaded as Tier 0)
# ---------------------------------------------------------------------------


class TestBuildContextFactsheet:
    def setup_method(self):
        self.config = MemoryConfig(enabled=False, profile_enabled=False)

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
            enabled=False, profile_enabled=True
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
