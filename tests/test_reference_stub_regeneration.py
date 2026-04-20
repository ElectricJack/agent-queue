"""Tests for reference stub regeneration — Roadmap 6.3.5.

Integration tests verifying the full pipeline from spec file change
to vault reference stub regeneration, covering the seven cases from
``docs/specs/design/roadmap.md`` task 6.3.5:

  (a) Changing a spec file in workspace triggers stub regeneration
      in ``vault/projects/{id}/references/``
  (b) Regenerated stub summary reflects the new content (not stale)
  (c) Stub retains Obsidian-compatible frontmatter and wikilink format
  (d) Stub file name matches source file name
  (e) Multiple spec files changed simultaneously each get their own stub
  (f) Stub generation handles large spec files (>5000 tokens) by
      summarizing effectively
  (g) Only files changed since last indexed snapshot trigger regeneration

These tests exercise the interaction between:
  - :class:`~src.workspace_spec_watcher.WorkspaceSpecWatcher` (change detection + stub writing)
  - :class:`~src.reference_stub_enricher.ReferenceStubEnricher` (LLM-based stub enrichment)
  - :class:`~src.event_bus.EventBus` (event plumbing between the two)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.event_bus import EventBus
from src.reference_stub_enricher import ReferenceStubEnricher
from src.workspace_spec_watcher import (
    SpecChange,
    WorkspaceSpecWatcher,
    derive_stub_name,
    generate_stub_content,
)


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


@dataclass
class FakeProject:
    id: str
    name: str = ""
    status: str = "active"


@dataclass
class FakeWorkspace:
    id: str
    project_id: str
    workspace_path: str
    source_type: str = "link"
    name: str | None = None
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None
    locked_at: float | None = None


class FakeDB:
    """Minimal fake database for testing."""

    def __init__(self):
        self.projects: list[FakeProject] = []
        self.workspaces: list[FakeWorkspace] = []

    async def list_projects(self, status=None) -> list[FakeProject]:
        return self.projects

    async def list_workspaces(self, project_id: str | None = None) -> list[FakeWorkspace]:
        if project_id:
            return [ws for ws in self.workspaces if ws.project_id == project_id]
        return self.workspaces


@dataclass
class FakeChatResponse:
    """Minimal ChatResponse stand-in for testing."""

    content: list

    @property
    def text_parts(self) -> list[str]:
        return [block.text for block in self.content if hasattr(block, "text")]


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeMemoryConfig:
    """Minimal MemoryConfig-like object with stub_enrichment fields."""

    stub_enrichment_enabled: bool = True
    stub_enrichment_provider: str = ""
    stub_enrichment_model: str = ""
    stub_enrichment_max_source_chars: int = 20_000
    revision_provider: str = ""
    revision_model: str = ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def fake_git():
    return MagicMock()


@pytest.fixture
def event_bus():
    """Real EventBus instance for integration tests."""
    return EventBus(env="dev", validate_events=False)


@pytest.fixture
def vault_dir(tmp_path):
    """Create a vault/projects directory structure."""
    vault_projects = tmp_path / "vault" / "projects"
    vault_projects.mkdir(parents=True)
    return str(vault_projects)


def _make_provider(response_text: str) -> AsyncMock:
    """Create a fake LLM provider that returns the given response text."""
    provider = AsyncMock()
    provider.create_message = AsyncMock(
        return_value=FakeChatResponse(content=[FakeTextBlock(text=response_text)])
    )
    return provider


def _make_enrichment_response(summary: str, decisions: str, interfaces: str) -> str:
    """Build a well-formed LLM enrichment response string."""
    return (
        f"## Summary\n{summary}\n\n## Key Decisions\n{decisions}\n\n## Key Interfaces\n{interfaces}"
    )


@pytest.fixture
def watcher(tmp_path, fake_db, event_bus, fake_git, vault_dir):
    """WorkspaceSpecWatcher wired to the real EventBus."""
    return WorkspaceSpecWatcher(
        db=fake_db,
        bus=event_bus,
        git=fake_git,
        vault_projects_dir=vault_dir,
        poll_interval_seconds=0,  # no rate-limiting in tests
        max_excerpt_lines=10,
        enabled=True,
    )


@pytest.fixture
def enricher(event_bus, vault_dir):
    """ReferenceStubEnricher wired to the real EventBus with a default provider."""
    provider = _make_provider(
        _make_enrichment_response(
            summary="This document defines the main orchestration loop.",
            decisions="- Tick-based loop at ~5s interval\n- Single agent per task",
            interfaces="- `Orchestrator.tick()` -- main loop entry\n"
            "- `Orchestrator.assign_task()` -- agent selection",
        )
    )
    return ReferenceStubEnricher(
        bus=event_bus,
        vault_projects_dir=vault_dir,
        config=FakeMemoryConfig(),
        provider=provider,
        enabled=True,
    )


def _create_workspace(tmp_path, project_id: str, files: dict[str, str]) -> str:
    """Create a workspace directory with spec/doc files.

    Returns the absolute path to the workspace root.
    """
    workspace = tmp_path / "workspaces" / project_id
    for rel_path, content in files.items():
        full_path = workspace / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
    return str(workspace)


def _read_stub(vault_dir: str, project_id: str, stub_name: str) -> str | None:
    """Read a stub file from the vault, returning None if not found."""
    stub_path = os.path.join(vault_dir, project_id, "references", stub_name)
    if not os.path.isfile(stub_path):
        return None
    with open(stub_path, encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# (a) Changing a spec file triggers stub regeneration
# ===========================================================================


class TestSpecChangeTriggersStubRegeneration:
    """6.3.5(a): Changing a spec file in workspace triggers stub
    regeneration in ``vault/projects/{id}/references/``.
    """

    @pytest.mark.asyncio
    async def test_modified_spec_regenerates_stub(self, watcher, fake_db, vault_dir, tmp_path):
        """Modifying an existing spec file causes the stub to be re-written."""
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API v1\nOriginal."})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan (takes snapshot, no changes)
        await watcher.check()

        # Modify the spec file
        time.sleep(0.05)
        api_path = os.path.join(ws, "specs", "api.md")
        with open(api_path, "w", encoding="utf-8") as f:
            f.write("# API v2\nRewritten content with new endpoints.")

        # Second scan detects the modification
        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "modified"

        # Stub should exist at the correct vault path
        stub_name = derive_stub_name("specs/api.md")
        stub_content = _read_stub(vault_dir, "proj", stub_name)
        assert stub_content is not None
        assert "API v2" in stub_content

    @pytest.mark.asyncio
    async def test_new_spec_creates_stub(self, watcher, fake_db, vault_dir, tmp_path):
        """Adding a new spec file creates a new stub in the vault."""
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Create a new spec file
        new_spec = os.path.join(ws, "specs", "database.md")
        with open(new_spec, "w", encoding="utf-8") as f:
            f.write("# Database Spec\nSchema design for the app.")

        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "created"

        # Stub should be written at vault/projects/proj/references/spec-database.md
        stub_content = _read_stub(vault_dir, "proj", "spec-database.md")
        assert stub_content is not None

    @pytest.mark.asyncio
    async def test_stub_path_under_vault_projects_references(
        self, watcher, fake_db, vault_dir, tmp_path
    ):
        """Stubs are written to vault/projects/{id}/references/."""
        ws = _create_workspace(tmp_path, "my-project", {"specs/config.md": "# Config"})
        fake_db.projects = [FakeProject(id="my-project")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="my-project", workspace_path=ws)]

        await watcher.check()  # initial scan

        # Create new file to trigger stub generation
        new_spec = os.path.join(ws, "specs", "routes.md")
        with open(new_spec, "w", encoding="utf-8") as f:
            f.write("# Routes")

        await watcher.check()

        expected_stub_path = os.path.join(vault_dir, "my-project", "references", "spec-routes.md")
        assert os.path.isfile(expected_stub_path)


# ===========================================================================
# (b) Regenerated stub summary reflects the new content (not stale)
# ===========================================================================


class TestRegeneratedStubReflectsNewContent:
    """6.3.5(b): Regenerated stub summary reflects new content, not stale."""

    @pytest.mark.asyncio
    async def test_enriched_stub_reflects_updated_source(self, enricher, vault_dir, tmp_path):
        """After source is updated and re-enriched, stub reflects the new content."""
        project_id = "proj"
        stub_name = "spec-api.md"

        # Create source v1 and its stub
        source = tmp_path / "workspace" / "specs" / "api.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# API v1\nOriginal endpoints: /users, /items", encoding="utf-8")

        # Write the initial stub with placeholders
        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)
        stub_content = generate_stub_content(
            rel_path="specs/api.md",
            abs_path=str(source),
            content_hash="hash1",
            workspace_path=str(tmp_path / "workspace"),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        # First enrichment
        result = await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )
        assert result.success is True

        # Now update the source to v2
        source.write_text(
            "# API v2\nNew endpoints: /orders, /payments, /refunds",
            encoding="utf-8",
        )

        # Create a new provider that reflects v2 content
        v2_provider = _make_provider(
            _make_enrichment_response(
                summary="API v2 with order management endpoints.",
                decisions="- RESTful design\n- Pagination on all list endpoints",
                interfaces="- `POST /orders` -- create order\n- `POST /payments` -- process payment",
            )
        )
        enricher._provider = v2_provider

        # Re-write the stub (simulating watcher regeneration)
        stub_content_v2 = generate_stub_content(
            rel_path="specs/api.md",
            abs_path=str(source),
            content_hash="hash2",
            workspace_path=str(tmp_path / "workspace"),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content_v2)

        # Re-enrich with v2
        result2 = await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )
        assert result2.success is True

        # Verify the stub reflects v2 content, not stale v1
        with open(stub_path, encoding="utf-8") as f:
            final = f.read()

        assert "API v2" in final
        assert "order management" in final
        assert "POST /orders" in final
        assert "source_hash: hash2" in final

    @pytest.mark.asyncio
    async def test_stub_not_stale_after_re_enrichment(self, enricher, vault_dir, tmp_path):
        """The enriched stub's Summary section should have real content, not a placeholder."""
        project_id = "proj"
        stub_name = "spec-orchestrator.md"

        source = tmp_path / "specs" / "orchestrator.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# Orchestrator\nManages the tick loop.", encoding="utf-8")

        # Create stub with placeholder
        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)
        stub_content = generate_stub_content(
            rel_path="specs/orchestrator.md",
            abs_path=str(source),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        # Enrich
        result = await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )
        assert result.success is True

        with open(stub_path, encoding="utf-8") as f:
            content = f.read()

        # Placeholder should be replaced
        assert "*Summary pending" not in content
        assert "*Pending LLM extraction.*" not in content
        # Real content should be present
        assert "orchestration loop" in content.lower() or "orchestrat" in content.lower()


# ===========================================================================
# (c) Stub retains Obsidian-compatible frontmatter and wikilink format
# ===========================================================================


class TestStubRetainsObsidianFormat:
    """6.3.5(c): Stub retains Obsidian-compatible frontmatter and wikilink format."""

    def test_generated_stub_has_yaml_frontmatter(self, tmp_path):
        """Generated stubs include YAML frontmatter delimited by ``---``."""
        source = tmp_path / "orchestrator.md"
        source.write_text("# Orchestrator\nContent here.")
        content = generate_stub_content(
            rel_path="specs/orchestrator.md",
            abs_path=str(source),
            content_hash="abc123def456",
            workspace_path=str(tmp_path),
            project_id="my-project",
        )
        # Must start with YAML frontmatter block
        assert content.startswith("---\n")
        lines = content.split("\n")
        # Find closing ---
        closing_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                closing_idx = i
                break
        assert closing_idx is not None, "YAML frontmatter not properly closed"

    def test_frontmatter_contains_required_tags(self, tmp_path):
        """Frontmatter includes tags, source, source_hash, and last_synced."""
        source = tmp_path / "api.md"
        source.write_text("# API")
        content = generate_stub_content(
            rel_path="specs/api.md",
            abs_path=str(source),
            content_hash="abc123def456",
            workspace_path=str(tmp_path),
            project_id="my-project",
        )
        assert "tags: [spec, reference, auto-generated]" in content
        assert f"source: {source}" in content
        assert "source_hash: abc123def456" in content
        assert "last_synced:" in content

    def test_doc_file_gets_doc_tag(self, tmp_path):
        """Files under docs/ get tag type 'doc' instead of 'spec'."""
        source = tmp_path / "guide.md"
        source.write_text("# Getting Started")
        content = generate_stub_content(
            rel_path="docs/guide.md",
            abs_path=str(source),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="proj",
        )
        assert "tags: [doc, reference, auto-generated]" in content
        assert "# Doc: Guide" in content

    def test_stub_has_all_expected_sections(self, tmp_path):
        """Stubs have Excerpt, Summary, Key Decisions, and Key Interfaces sections."""
        source = tmp_path / "spec.md"
        source.write_text("# Spec\nLine 1\nLine 2")
        content = generate_stub_content(
            rel_path="specs/spec.md",
            abs_path=str(source),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="proj",
        )
        assert "## Excerpt" in content
        assert "## Summary" in content
        assert "## Key Decisions" in content
        assert "## Key Interfaces" in content

    @pytest.mark.asyncio
    async def test_enriched_stub_preserves_frontmatter(self, enricher, vault_dir, tmp_path):
        """LLM enrichment preserves the YAML frontmatter block."""
        project_id = "proj"
        stub_name = "spec-api.md"

        source = tmp_path / "api.md"
        source.write_text("# API\nContent.", encoding="utf-8")

        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)

        stub_content = generate_stub_content(
            rel_path="specs/api.md",
            abs_path=str(source),
            content_hash="abc123def456",
            workspace_path=str(tmp_path),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )

        with open(stub_path, encoding="utf-8") as f:
            enriched = f.read()

        # Frontmatter must survive enrichment
        assert enriched.startswith("---\n")
        assert "tags: [spec, reference, auto-generated]" in enriched
        assert "source_hash: abc123def456" in enriched
        assert "last_synced:" in enriched

    @pytest.mark.asyncio
    async def test_enriched_stub_preserves_excerpt_section(self, enricher, vault_dir, tmp_path):
        """LLM enrichment preserves the Excerpt section content."""
        project_id = "proj"
        stub_name = "spec-api.md"

        source = tmp_path / "api.md"
        source.write_text("# API\nThis is the excerpt content.", encoding="utf-8")

        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)

        stub_content = generate_stub_content(
            rel_path="specs/api.md",
            abs_path=str(source),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )

        with open(stub_path, encoding="utf-8") as f:
            enriched = f.read()

        assert "## Excerpt" in enriched
        assert "This is the excerpt content." in enriched


# ===========================================================================
# (d) Stub file name matches source file name
# ===========================================================================


class TestStubFilenameMatchesSource:
    """6.3.5(d): Stub file name matches source file name."""

    def test_simple_spec_filename(self):
        """specs/api.md -> spec-api.md"""
        assert derive_stub_name("specs/api.md") == "spec-api.md"

    def test_hyphenated_filename(self):
        """specs/api-spec.md -> spec-api-spec.md"""
        assert derive_stub_name("specs/api-spec.md") == "spec-api-spec.md"

    def test_nested_spec_filename(self):
        """specs/design/vault.md -> spec-design-vault.md"""
        assert derive_stub_name("specs/design/vault.md") == "spec-design-vault.md"

    def test_docs_specs_filename(self):
        """docs/specs/design/vault.md -> spec-design-vault.md"""
        assert derive_stub_name("docs/specs/design/vault.md") == "spec-design-vault.md"

    def test_docs_filename(self):
        """docs/getting-started.md -> doc-getting-started.md"""
        assert derive_stub_name("docs/getting-started.md") == "doc-getting-started.md"

    def test_stub_written_with_correct_name(self, tmp_path):
        """The watcher writes stubs with the derived filename."""
        ws_path = str(tmp_path / "workspace")
        spec_path = tmp_path / "workspace" / "specs" / "api-spec.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("# API Spec\nContent here.")

        vault_dir = str(tmp_path / "vault" / "projects")
        os.makedirs(vault_dir, exist_ok=True)

        watcher = WorkspaceSpecWatcher(
            db=MagicMock(),
            bus=MagicMock(emit=AsyncMock()),
            git=MagicMock(),
            vault_projects_dir=vault_dir,
            poll_interval_seconds=0,
            enabled=True,
        )

        change = SpecChange(
            project_id="proj",
            workspace_path=ws_path,
            rel_path="specs/api-spec.md",
            abs_path=str(spec_path),
            operation="created",
            content_hash="abc123",
        )
        stub_path = watcher._write_stub(change)

        assert stub_path is not None
        assert os.path.basename(stub_path) == "spec-api-spec.md"

    def test_event_includes_correct_stub_name(self, tmp_path):
        """The emitted event includes the correct stub_name."""
        ws_path = str(tmp_path / "workspace")
        bus = MagicMock()
        bus.emit = AsyncMock()

        watcher = WorkspaceSpecWatcher(
            db=MagicMock(),
            bus=bus,
            git=MagicMock(),
            vault_projects_dir=str(tmp_path / "vault"),
            poll_interval_seconds=0,
            enabled=True,
        )

        # Manually call _emit_event to check the stub_name in the event data
        import asyncio

        change = SpecChange(
            project_id="proj",
            workspace_path=ws_path,
            rel_path="specs/api-spec.md",
            abs_path=os.path.join(ws_path, "specs", "api-spec.md"),
            operation="created",
            content_hash="abc123",
        )
        asyncio.new_event_loop().run_until_complete(watcher._emit_event(change))

        call_args = bus.emit.call_args
        event_data = call_args[0][1]
        assert event_data["stub_name"] == "spec-api-spec.md"


# ===========================================================================
# (e) Multiple spec files changed simultaneously get their own stubs
# ===========================================================================


class TestMultipleSimultaneousChanges:
    """6.3.5(e): Multiple spec files changed simultaneously each get
    their own stub regenerated.
    """

    @pytest.mark.asyncio
    async def test_multiple_files_each_get_stubs(self, watcher, fake_db, vault_dir, tmp_path):
        """Three files created simultaneously each produce a separate stub."""
        ws = _create_workspace(tmp_path, "proj", {"specs/placeholder.md": "# Placeholder"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Create three new spec files simultaneously
        for name, content in [
            ("api.md", "# API\nEndpoints for the application."),
            ("database.md", "# Database\nSchema design and migrations."),
            ("supervisor.md", "# Supervisor\nLLM orchestration layer."),
        ]:
            path = os.path.join(ws, "specs", name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        changes = await watcher.check()
        assert len(changes) == 3

        # Each should have its own stub
        for name in ["api", "database", "supervisor"]:
            stub_name = f"spec-{name}.md"
            stub_content = _read_stub(vault_dir, "proj", stub_name)
            assert stub_content is not None, f"Stub {stub_name} not found"

    @pytest.mark.asyncio
    async def test_multiple_files_enriched_independently(self, enricher, vault_dir, tmp_path):
        """Each file's enrichment is independent and tracked by counter."""
        project_id = "proj"

        specs = {
            "api": ("# API Spec\nREST endpoints.", "API overview with REST endpoints."),
            "db": ("# Database\nSchema.", "Database schema description."),
            "auth": ("# Auth\nAuthentication.", "Authentication system overview."),
        }

        for name, (source_content, summary) in specs.items():
            source = tmp_path / f"{name}.md"
            source.write_text(source_content, encoding="utf-8")

            stub_name = f"spec-{name}.md"
            refs_dir = os.path.join(vault_dir, project_id, "references")
            os.makedirs(refs_dir, exist_ok=True)
            stub_path = os.path.join(refs_dir, stub_name)

            stub_content = generate_stub_content(
                rel_path=f"specs/{name}.md",
                abs_path=str(source),
                content_hash=f"hash-{name}",
                workspace_path=str(tmp_path),
                project_id=project_id,
            )
            with open(stub_path, "w", encoding="utf-8") as f:
                f.write(stub_content)

            result = await enricher.enrich_stub(
                project_id=project_id,
                abs_path=str(source),
                stub_name=stub_name,
            )
            assert result.success is True, f"Enrichment failed for {name}: {result.error}"

        assert enricher.total_enriched == 3
        assert enricher.total_failed == 0

    @pytest.mark.asyncio
    async def test_mixed_operations_all_produce_stubs(self, watcher, fake_db, vault_dir, tmp_path):
        """Mix of created and modified files all get stubs."""
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/existing.md": "# Existing\nOriginal content.",
                "specs/another.md": "# Another\nOriginal.",
            },
        )
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Modify one, create a new one
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "existing.md"), "w", encoding="utf-8") as f:
            f.write("# Existing\nUpdated content.")
        with open(os.path.join(ws, "specs", "brand-new.md"), "w", encoding="utf-8") as f:
            f.write("# Brand New\nFresh spec.")

        changes = await watcher.check()
        operations = {c.rel_path: c.operation for c in changes}
        assert operations["specs/existing.md"] == "modified"
        assert operations["specs/brand-new.md"] == "created"

        # Both should have stubs
        assert _read_stub(vault_dir, "proj", "spec-existing.md") is not None
        assert _read_stub(vault_dir, "proj", "spec-brand-new.md") is not None


# ===========================================================================
# (f) Stub generation handles large spec files (>5000 tokens)
# ===========================================================================


class TestLargeSpecFileHandling:
    """6.3.5(f): Stub generation handles large spec files (>5000 tokens)
    by summarizing effectively.
    """

    @pytest.mark.asyncio
    async def test_large_file_truncated_for_llm(self, vault_dir, event_bus, tmp_path):
        """Large spec files are truncated before being sent to the LLM."""
        max_chars = 1000
        provider = _make_provider(
            _make_enrichment_response(
                summary="A very large specification covering the entire system architecture.",
                decisions="- Modular design\n- Event-driven architecture",
                interfaces="- `SystemAPI` -- main API surface",
            )
        )
        enricher = ReferenceStubEnricher(
            bus=event_bus,
            vault_projects_dir=vault_dir,
            config=FakeMemoryConfig(stub_enrichment_max_source_chars=max_chars),
            provider=provider,
            enabled=True,
            max_source_chars=max_chars,
        )

        # Create a large source file (~20k chars, well over 5000 tokens)
        source = tmp_path / "big-spec.md"
        source.write_text(
            "# Big Architecture Spec\n\n" + "This is a detailed section. " * 1000,
            encoding="utf-8",
        )

        project_id = "proj"
        stub_name = "spec-big-spec.md"
        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)
        stub_content = generate_stub_content(
            rel_path="specs/big-spec.md",
            abs_path=str(source),
            content_hash="bighash",
            workspace_path=str(tmp_path),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        result = await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )
        assert result.success is True

        # Verify the LLM received truncated content
        call_kwargs = provider.create_message.call_args.kwargs
        user_msg = call_kwargs["messages"][0]["content"]
        assert f"[Document truncated at {max_chars} characters" in user_msg

    @pytest.mark.asyncio
    async def test_large_file_enrichment_produces_valid_stub(self, vault_dir, event_bus, tmp_path):
        """Despite truncation, the enriched stub has all required sections."""
        provider = _make_provider(
            _make_enrichment_response(
                summary="Large system specification with comprehensive architecture.",
                decisions="- Microservices pattern\n- CQRS for data access",
                interfaces="- `Gateway` -- API gateway\n- `EventStore` -- event persistence",
            )
        )
        enricher = ReferenceStubEnricher(
            bus=event_bus,
            vault_projects_dir=vault_dir,
            config=FakeMemoryConfig(),
            provider=provider,
            enabled=True,
            max_source_chars=500,
        )

        source = tmp_path / "huge.md"
        source.write_text("# Huge Spec\n" + "Detail. " * 5000, encoding="utf-8")

        project_id = "proj"
        stub_name = "spec-huge.md"
        refs_dir = os.path.join(vault_dir, project_id, "references")
        os.makedirs(refs_dir, exist_ok=True)
        stub_path = os.path.join(refs_dir, stub_name)
        stub_content = generate_stub_content(
            rel_path="specs/huge.md",
            abs_path=str(source),
            content_hash="hugehash",
            workspace_path=str(tmp_path),
            project_id=project_id,
        )
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub_content)

        result = await enricher.enrich_stub(
            project_id=project_id,
            abs_path=str(source),
            stub_name=stub_name,
        )
        assert result.success is True

        with open(stub_path, encoding="utf-8") as f:
            content = f.read()

        # All sections must be present despite source truncation
        assert "## Excerpt" in content
        assert "## Summary" in content
        assert "## Key Decisions" in content
        assert "## Key Interfaces" in content
        # LLM-generated content should be present
        assert "Microservices" in content or "comprehensive" in content.lower()

    def test_excerpt_truncation_for_large_files(self, tmp_path):
        """generate_stub_content truncates excerpts for large files."""
        source = tmp_path / "long.md"
        lines = [f"Line {i}: Some detailed content about the system." for i in range(200)]
        source.write_text("\n".join(lines), encoding="utf-8")

        content = generate_stub_content(
            rel_path="specs/long.md",
            abs_path=str(source),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="proj",
            max_excerpt_lines=10,
        )

        assert "excerpt truncated" in content
        # Should include the first few lines
        assert "Line 0" in content
        assert "Line 9" in content


# ===========================================================================
# (g) Only changed files trigger regeneration (snapshot-based detection)
# ===========================================================================


class TestOnlyChangedFilesTriggered:
    """6.3.5(g): Only files changed since last indexed snapshot trigger
    regeneration. The watcher uses content-hash + mtime comparison
    (functionally equivalent to git-diff-based detection).
    """

    @pytest.mark.asyncio
    async def test_unchanged_files_not_regenerated(self, watcher, fake_db, vault_dir, tmp_path):
        """Files that haven't changed are not re-scanned as changes."""
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API\nEndpoints.",
                "specs/database.md": "# Database\nSchema.",
                "specs/supervisor.md": "# Supervisor\nLLM loop.",
            },
        )
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Modify only one file
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w", encoding="utf-8") as f:
            f.write("# API v2\nUpdated endpoints.")

        changes = await watcher.check()

        # Only the changed file should trigger
        assert len(changes) == 1
        assert changes[0].rel_path == "specs/api.md"
        assert changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_content_hash_detects_actual_changes(self, watcher, fake_db, tmp_path):
        """Content-hash comparison detects actual content changes."""
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API\nOriginal."})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Write different content
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w", encoding="utf-8") as f:
            f.write("# API v2\nDifferent content here.")

        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].content_hash != ""  # Hash of new content

    @pytest.mark.asyncio
    async def test_no_changes_no_regeneration(self, watcher, fake_db, tmp_path):
        """If no files changed, check() returns empty and no stubs are written."""
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                "specs/db.md": "# DB",
            },
        )
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        initial_written = watcher.total_stubs_written

        # Initial scan
        await watcher.check()

        # Second scan with no changes
        changes = await watcher.check()
        assert changes == []
        # No new stubs should have been written after initial scan
        assert watcher.total_stubs_written == initial_written

    @pytest.mark.asyncio
    async def test_only_matching_patterns_detected(self, watcher, fake_db, tmp_path):
        """Only spec/doc files matching the configured patterns trigger changes."""
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                "src/main.py": "print('hello')",
            },
        )
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Modify both files
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w", encoding="utf-8") as f:
            f.write("# API v2")
        with open(os.path.join(ws, "src", "main.py"), "w", encoding="utf-8") as f:
            f.write("print('updated')")

        changes = await watcher.check()
        # Only the spec file should be detected
        assert len(changes) == 1
        assert changes[0].rel_path == "specs/api.md"

    @pytest.mark.asyncio
    async def test_snapshot_tracks_correct_state(self, watcher, fake_db, tmp_path):
        """The watcher snapshot accurately reflects the last indexed state."""
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API v1",
                "specs/db.md": "# DB v1",
            },
        )
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        snapshot = watcher.get_snapshot("proj")
        assert snapshot is not None
        assert "specs/api.md" in snapshot
        assert "specs/db.md" in snapshot

        # Modify one file
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w", encoding="utf-8") as f:
            f.write("# API v2")

        # After second scan, snapshot should reflect new state
        changes = await watcher.check()
        assert len(changes) == 1

        updated_snapshot = watcher.get_snapshot("proj")
        # The hash should have changed for api.md
        assert (
            updated_snapshot["specs/api.md"].content_hash != snapshot["specs/api.md"].content_hash
        )
        # But not for db.md
        assert updated_snapshot["specs/db.md"].content_hash == snapshot["specs/db.md"].content_hash


# ===========================================================================
# Integration: Watcher → EventBus → Enricher pipeline
# ===========================================================================


class TestWatcherEnricherIntegration:
    """Integration test: watcher detects change, emits event, enricher subscribes."""

    @pytest.mark.asyncio
    async def test_full_pipeline_event_flow(
        self, watcher, enricher, event_bus, fake_db, vault_dir, tmp_path
    ):
        """Watcher detects change → emits event → enricher receives and enriches."""
        project_id = "proj"
        ws = _create_workspace(
            tmp_path,
            project_id,
            {"specs/orchestrator.md": "# Orchestrator\nManages the tick loop."},
        )
        fake_db.projects = [FakeProject(id=project_id)]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id=project_id, workspace_path=ws)]

        # Subscribe the enricher to events
        enricher.subscribe()

        # Initial scan
        await watcher.check()

        # Create a new spec file
        new_spec = os.path.join(ws, "specs", "database.md")
        with open(new_spec, "w", encoding="utf-8") as f:
            f.write("# Database\nSchema design and migration strategy.")

        # Second scan: watcher detects new file, writes stub, emits event
        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "created"

        # Verify the stub was written by the watcher
        stub_name = derive_stub_name("specs/database.md")
        stub_content = _read_stub(vault_dir, project_id, stub_name)
        assert stub_content is not None
        assert "## Summary" in stub_content

        # The enricher should have been triggered via the event bus
        assert enricher.total_enriched == 1

        # After enrichment, the stub should have LLM-generated content
        enriched_content = _read_stub(vault_dir, project_id, stub_name)
        assert enriched_content is not None
        assert "*Summary pending" not in enriched_content

    @pytest.mark.asyncio
    async def test_pipeline_with_multiple_changes(
        self, watcher, enricher, event_bus, fake_db, vault_dir, tmp_path
    ):
        """Multiple changes each flow through the full pipeline."""
        project_id = "proj"
        ws = _create_workspace(
            tmp_path,
            project_id,
            {"specs/placeholder.md": "# Placeholder"},
        )
        fake_db.projects = [FakeProject(id=project_id)]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id=project_id, workspace_path=ws)]

        enricher.subscribe()

        # Initial scan
        await watcher.check()

        # Create two new spec files
        for name in ["api", "config"]:
            path = os.path.join(ws, "specs", f"{name}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {name.title()}\nContent for {name}.")

        changes = await watcher.check()
        assert len(changes) == 2

        # Both should have been enriched
        assert enricher.total_enriched == 2

    @pytest.mark.asyncio
    async def test_deleted_event_does_not_trigger_enrichment(
        self, watcher, enricher, event_bus, fake_db, vault_dir, tmp_path
    ):
        """Deleted spec files do not trigger enrichment."""
        project_id = "proj"
        ws = _create_workspace(
            tmp_path,
            project_id,
            {"specs/api.md": "# API"},
        )
        fake_db.projects = [FakeProject(id=project_id)]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id=project_id, workspace_path=ws)]

        enricher.subscribe()

        # Initial scan, then create and detect a file
        await watcher.check()
        new_spec = os.path.join(ws, "specs", "temp.md")
        with open(new_spec, "w", encoding="utf-8") as f:
            f.write("# Temp Spec")
        await watcher.check()

        enriched_before = enricher.total_enriched

        # Now delete the file
        os.remove(new_spec)
        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "deleted"

        # Enricher should NOT have been triggered for the deletion
        assert enricher.total_enriched == enriched_before
