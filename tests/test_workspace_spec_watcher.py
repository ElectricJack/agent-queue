"""Tests for src/workspace_spec_watcher — workspace spec/doc change detector."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workspace_spec_watcher import (
    DEFAULT_SPEC_PATTERNS,
    SpecChange,
    SpecFileState,
    WorkspaceSpecWatcher,
    compute_content_hash,
    derive_stub_name,
    generate_stub_content,
    matches_any_pattern,
)


# ---------------------------------------------------------------------------
# Fixtures
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
    """Minimal fake database for testing WorkspaceSpecWatcher."""

    def __init__(self):
        self.projects: list[FakeProject] = []
        self.workspaces: list[FakeWorkspace] = []

    async def list_projects(self, status=None) -> list[FakeProject]:
        return self.projects

    async def list_workspaces(self, project_id: str | None = None) -> list[FakeWorkspace]:
        if project_id:
            return [ws for ws in self.workspaces if ws.project_id == project_id]
        return self.workspaces


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def fake_bus():
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def fake_git():
    return MagicMock()


@pytest.fixture
def watcher(tmp_path, fake_db, fake_bus, fake_git):
    """Create a WorkspaceSpecWatcher with a tmp vault projects directory."""
    vault_projects = str(tmp_path / "vault" / "projects")
    os.makedirs(vault_projects, exist_ok=True)
    return WorkspaceSpecWatcher(
        db=fake_db,
        bus=fake_bus,
        git=fake_git,
        vault_projects_dir=vault_projects,
        poll_interval_seconds=0,  # No rate limiting in tests
        max_excerpt_lines=10,
        enabled=True,
    )


def _create_workspace(tmp_path, project_id: str, files: dict[str, str]) -> str:
    """Create a workspace directory with the given spec/doc files.

    Parameters
    ----------
    tmp_path:
        Base temporary directory.
    project_id:
        Project identifier (used as subdirectory name).
    files:
        Mapping of relative paths to file content.

    Returns
    -------
    str
        Absolute path to the workspace root.
    """
    workspace = tmp_path / "workspaces" / project_id
    for rel_path, content in files.items():
        full_path = workspace / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
    return str(workspace)


# ---------------------------------------------------------------------------
# derive_stub_name
# ---------------------------------------------------------------------------


class TestDeriveStubName:
    """Tests for derive_stub_name — filename derivation from workspace paths."""

    def test_simple_spec(self):
        assert derive_stub_name("specs/orchestrator.md") == "spec-orchestrator.md"

    def test_nested_spec(self):
        assert derive_stub_name("specs/design/vault.md") == "spec-design-vault.md"

    def test_docs_specs(self):
        assert derive_stub_name("docs/specs/design/vault.md") == "spec-design-vault.md"

    def test_docs_file(self):
        assert derive_stub_name("docs/getting-started.md") == "doc-getting-started.md"

    def test_docs_nested(self):
        assert derive_stub_name("docs/api/endpoints.md") == "doc-api-endpoints.md"

    def test_unknown_prefix(self):
        """Files not under specs/ or docs/ get 'ref-' prefix."""
        assert derive_stub_name("other/readme.md") == "ref-other-readme.md"

    def test_backslash_normalisation(self):
        assert derive_stub_name("specs\\design\\vault.md") == "spec-design-vault.md"

    def test_single_file_in_specs(self):
        assert derive_stub_name("specs/api.md") == "spec-api.md"

    def test_deeply_nested(self):
        assert derive_stub_name("docs/a/b/c/d.md") == "doc-a-b-c-d.md"


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


class TestComputeContentHash:
    """Tests for compute_content_hash — SHA-256 prefix."""

    def test_returns_12_char_hex(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Hello World")
        h = compute_content_hash(str(f))
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.md"
        f1.write_text("Hello")
        f2 = tmp_path / "b.md"
        f2.write_text("World")
        assert compute_content_hash(str(f1)) != compute_content_hash(str(f2))

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.md"
        f1.write_text("Same content")
        f2 = tmp_path / "b.md"
        f2.write_text("Same content")
        assert compute_content_hash(str(f1)) == compute_content_hash(str(f2))

    def test_nonexistent_file_returns_empty(self):
        assert compute_content_hash("/nonexistent/file.md") == ""


# ---------------------------------------------------------------------------
# matches_any_pattern
# ---------------------------------------------------------------------------


class TestMatchesAnyPattern:
    """Tests for glob pattern matching against workspace-relative paths."""

    def test_specs_toplevel(self):
        assert matches_any_pattern("specs/orchestrator.md", DEFAULT_SPEC_PATTERNS)

    def test_specs_nested(self):
        assert matches_any_pattern("specs/design/vault.md", DEFAULT_SPEC_PATTERNS)

    def test_docs_specs_nested(self):
        assert matches_any_pattern("docs/specs/design/vault.md", DEFAULT_SPEC_PATTERNS)

    def test_docs_toplevel(self):
        assert matches_any_pattern("docs/getting-started.md", DEFAULT_SPEC_PATTERNS)

    def test_docs_nested(self):
        assert matches_any_pattern("docs/api/endpoints.md", DEFAULT_SPEC_PATTERNS)

    def test_no_match_src_file(self):
        assert not matches_any_pattern("src/main.py", DEFAULT_SPEC_PATTERNS)

    def test_no_match_root_md(self):
        assert not matches_any_pattern("README.md", DEFAULT_SPEC_PATTERNS)

    def test_no_match_non_md(self):
        assert not matches_any_pattern("specs/schema.json", DEFAULT_SPEC_PATTERNS)

    def test_custom_patterns(self):
        patterns = ("*.md",)
        assert matches_any_pattern("README.md", patterns)
        assert not matches_any_pattern("src/main.py", patterns)

    def test_backslash_normalisation(self):
        assert matches_any_pattern("specs\\design\\vault.md", DEFAULT_SPEC_PATTERNS)


# ---------------------------------------------------------------------------
# generate_stub_content
# ---------------------------------------------------------------------------


class TestGenerateStubContent:
    """Tests for vault reference stub generation."""

    def test_contains_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Test\nContent here")
        content = generate_stub_content(
            rel_path="specs/test.md",
            abs_path=str(f),
            content_hash="abc123def456",
            workspace_path=str(tmp_path),
            project_id="my-app",
        )
        assert "tags: [spec, reference, auto-generated]" in content
        assert f"source: {f}" in content
        assert "source_hash: abc123def456" in content
        assert "last_synced:" in content

    def test_spec_title(self, tmp_path):
        f = tmp_path / "orchestrator.md"
        f.write_text("# Orchestrator")
        content = generate_stub_content(
            rel_path="specs/orchestrator.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
        )
        assert "# Spec: Orchestrator" in content

    def test_doc_title(self, tmp_path):
        f = tmp_path / "getting-started.md"
        f.write_text("# Getting Started")
        content = generate_stub_content(
            rel_path="docs/getting-started.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
        )
        assert "# Doc: Getting Started" in content

    def test_excerpt_included(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Title\n\nLine 1\nLine 2\nLine 3")
        content = generate_stub_content(
            rel_path="specs/test.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
            max_excerpt_lines=5,
        )
        assert "# Title" in content
        assert "Line 1" in content
        assert "Line 2" in content

    def test_excerpt_truncated(self, tmp_path):
        f = tmp_path / "long.md"
        lines = [f"Line {i}" for i in range(100)]
        f.write_text("\n".join(lines))
        content = generate_stub_content(
            rel_path="specs/long.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
            max_excerpt_lines=5,
        )
        assert "excerpt truncated" in content

    def test_placeholder_sections(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Test")
        content = generate_stub_content(
            rel_path="specs/test.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
        )
        assert "## Summary" in content
        assert "## Key Decisions" in content
        assert "## Key Interfaces" in content
        assert "LLM enrichment" in content

    def test_project_id_in_content(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Test")
        content = generate_stub_content(
            rel_path="specs/test.md",
            abs_path=str(f),
            content_hash="abc123",
            workspace_path=str(tmp_path),
            project_id="my-app",
        )
        assert "my-app" in content


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — scanning
# ---------------------------------------------------------------------------


class TestWatcherScanning:
    """Tests for _scan_workspace — file state tracking."""

    def test_initial_scan_no_changes(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        changes = watcher._scan_workspace("proj", ws)
        # First scan: takes snapshot, no changes reported
        assert changes == []

    def test_second_scan_no_changes(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)  # initial
        changes = watcher._scan_workspace("proj", ws)  # second
        assert changes == []

    def test_detects_new_file(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)  # initial

        # Create a new spec file
        new_file = os.path.join(ws, "specs", "database.md")
        with open(new_file, "w") as f:
            f.write("# Database")

        changes = watcher._scan_workspace("proj", ws)
        assert len(changes) == 1
        assert changes[0].operation == "created"
        assert changes[0].rel_path == "specs/database.md"
        assert changes[0].project_id == "proj"

    def test_detects_modification(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)  # initial

        # Modify the file
        time.sleep(0.05)  # ensure different mtime
        api_file = os.path.join(ws, "specs", "api.md")
        with open(api_file, "w") as f:
            f.write("# API v2\n\nUpdated content")

        changes = watcher._scan_workspace("proj", ws)
        assert len(changes) == 1
        assert changes[0].operation == "modified"
        assert changes[0].rel_path == "specs/api.md"

    def test_detects_deletion(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)  # initial

        # Delete the file
        os.remove(os.path.join(ws, "specs", "api.md"))

        changes = watcher._scan_workspace("proj", ws)
        assert len(changes) == 1
        assert changes[0].operation == "deleted"
        assert changes[0].rel_path == "specs/api.md"
        assert changes[0].content_hash == ""

    def test_detects_multiple_changes(self, watcher, tmp_path):
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                "specs/db.md": "# DB",
            },
        )
        watcher._scan_workspace("proj", ws)  # initial

        # Create new, modify existing, delete one
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w") as f:
            f.write("# API v2")
        os.remove(os.path.join(ws, "specs", "db.md"))
        os.makedirs(os.path.join(ws, "docs"), exist_ok=True)
        with open(os.path.join(ws, "docs", "guide.md"), "w") as f:
            f.write("# Guide")

        changes = watcher._scan_workspace("proj", ws)
        operations = {c.rel_path: c.operation for c in changes}
        assert operations["specs/api.md"] == "modified"
        assert operations["specs/db.md"] == "deleted"
        assert operations["docs/guide.md"] == "created"

    def test_ignores_non_matching_files(self, watcher, tmp_path):
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                "src/main.py": "print('hello')",
                "README.md": "# Readme",
            },
        )
        watcher._scan_workspace("proj", ws)  # initial

        # Only specs/api.md should be tracked
        snapshot = watcher.get_snapshot("proj")
        assert "specs/api.md" in snapshot
        assert "src/main.py" not in snapshot
        assert "README.md" not in snapshot

    def test_skips_hidden_directories(self, watcher, tmp_path):
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                ".git/config": "gitconfig",
                "specs/.hidden/secret.md": "secret",
            },
        )
        watcher._scan_workspace("proj", ws)

        snapshot = watcher.get_snapshot("proj")
        assert "specs/api.md" in snapshot
        assert ".git/config" not in snapshot

    def test_nonexistent_workspace_returns_empty(self, watcher):
        changes = watcher._scan_workspace("proj", "/nonexistent/path")
        assert changes == []


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — stub writing
# ---------------------------------------------------------------------------


class TestStubWriting:
    """Tests for _write_stub — vault reference stub file creation."""

    def test_writes_stub_file(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API Spec"})
        change = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="abc123def456",
        )
        stub_path = watcher._write_stub(change)

        assert stub_path is not None
        assert os.path.isfile(stub_path)
        assert stub_path.endswith("spec-api.md")

        content = open(stub_path).read()
        assert "source_hash: abc123def456" in content
        assert "# Spec: Api" in content

    def test_creates_references_directory(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "new-proj", {"specs/api.md": "# API"})
        change = SpecChange(
            project_id="new-proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="abc123",
        )
        watcher._write_stub(change)

        refs_dir = os.path.join(watcher._vault_projects_dir, "new-proj", "references")
        assert os.path.isdir(refs_dir)

    def test_overwrites_existing_stub(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API v1"})
        change1 = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="hash1",
        )
        watcher._write_stub(change1)

        # Update source and write again
        with open(os.path.join(ws, "specs", "api.md"), "w") as f:
            f.write("# API v2")

        change2 = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="modified",
            content_hash="hash2",
        )
        stub_path = watcher._write_stub(change2)

        content = open(stub_path).read()
        assert "source_hash: hash2" in content
        assert "# API v2" in content

    def test_stub_counter_increments(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        change = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="abc123",
        )
        assert watcher.total_stubs_written == 0
        watcher._write_stub(change)
        assert watcher.total_stubs_written == 1


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — stub deletion
# ---------------------------------------------------------------------------


class TestStubDeletion:
    """Tests for _delete_stub — removing stubs when source is deleted."""

    def test_deletes_existing_stub(self, watcher, tmp_path):
        # Create a stub first
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        change = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="abc123",
        )
        stub_path = watcher._write_stub(change)
        assert os.path.isfile(stub_path)

        # Now delete it
        delete_change = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="deleted",
            content_hash="",
        )
        assert watcher._delete_stub(delete_change) is True
        assert not os.path.isfile(stub_path)
        assert watcher.total_stubs_deleted == 1

    def test_delete_nonexistent_stub(self, watcher, tmp_path):
        change = SpecChange(
            project_id="proj",
            workspace_path="/some/path",
            rel_path="specs/missing.md",
            abs_path="/some/path/specs/missing.md",
            operation="deleted",
            content_hash="",
        )
        assert watcher._delete_stub(change) is False
        assert watcher.total_stubs_deleted == 0


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — event emission
# ---------------------------------------------------------------------------


class TestEventEmission:
    """Tests for _emit_event — EventBus integration."""

    @pytest.mark.asyncio
    async def test_emits_workspace_spec_changed(self, watcher, fake_bus, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        change = SpecChange(
            project_id="proj",
            workspace_path=ws,
            rel_path="specs/api.md",
            abs_path=os.path.join(ws, "specs", "api.md"),
            operation="created",
            content_hash="abc123",
        )
        await watcher._emit_event(change)

        fake_bus.emit.assert_called_once()
        call_args = fake_bus.emit.call_args
        assert call_args[0][0] == "workspace.spec.changed"
        data = call_args[0][1]
        assert data["project_id"] == "proj"
        assert data["rel_path"] == "specs/api.md"
        assert data["operation"] == "created"
        assert data["content_hash"] == "abc123"
        assert data["stub_name"] == "spec-api.md"

    @pytest.mark.asyncio
    async def test_emit_handles_bus_error(self, watcher, fake_bus, tmp_path):
        """Event emission errors should not propagate."""
        fake_bus.emit.side_effect = RuntimeError("bus error")

        change = SpecChange(
            project_id="proj",
            workspace_path="/path",
            rel_path="specs/api.md",
            abs_path="/path/specs/api.md",
            operation="modified",
            content_hash="abc123",
        )
        # Should not raise
        await watcher._emit_event(change)


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — full check() integration
# ---------------------------------------------------------------------------


class TestFullCheck:
    """Integration tests for the full check() cycle."""

    @pytest.mark.asyncio
    async def test_check_disabled_returns_empty(self, watcher):
        watcher.enabled = False
        changes = await watcher.check()
        assert changes == []

    @pytest.mark.asyncio
    async def test_check_no_projects_returns_empty(self, watcher, fake_db):
        fake_db.projects = []
        changes = await watcher.check()
        assert changes == []

    @pytest.mark.asyncio
    async def test_check_initial_scan_no_changes(self, watcher, fake_db, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        changes = await watcher.check()
        assert changes == []

    @pytest.mark.asyncio
    async def test_check_detects_new_file(self, watcher, fake_db, fake_bus, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Create new spec file
        os.makedirs(os.path.join(ws, "docs"), exist_ok=True)
        with open(os.path.join(ws, "docs", "guide.md"), "w") as f:
            f.write("# Guide")

        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "created"
        assert changes[0].rel_path == "docs/guide.md"

        # Verify stub was written
        stub_path = watcher.get_stub_path("proj", "docs/guide.md")
        assert os.path.isfile(stub_path)

        # Verify event was emitted
        assert fake_bus.emit.called

    @pytest.mark.asyncio
    async def test_check_rate_limited(self, tmp_path, fake_db, fake_bus, fake_git):
        """check() should respect poll_interval_seconds."""
        vault_projects = str(tmp_path / "vault" / "projects")
        os.makedirs(vault_projects, exist_ok=True)
        w = WorkspaceSpecWatcher(
            db=fake_db,
            bus=fake_bus,
            git=fake_git,
            vault_projects_dir=vault_projects,
            poll_interval_seconds=3600,  # 1 hour
            enabled=True,
        )

        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # First check runs
        await w.check()
        assert w.get_tracked_project_count() == 1

        # Subsequent check is rate-limited (returns early)
        with open(os.path.join(ws, "specs", "new.md"), "w") as f:
            f.write("# New Spec")

        changes = await w.check()
        assert changes == []  # Rate-limited

    @pytest.mark.asyncio
    async def test_check_multiple_projects(self, watcher, fake_db, tmp_path):
        ws1 = _create_workspace(tmp_path, "proj1", {"specs/api.md": "# API"})
        ws2 = _create_workspace(tmp_path, "proj2", {"docs/guide.md": "# Guide"})

        fake_db.projects = [
            FakeProject(id="proj1"),
            FakeProject(id="proj2"),
        ]
        fake_db.workspaces = [
            FakeWorkspace(id="ws1", project_id="proj1", workspace_path=ws1),
            FakeWorkspace(id="ws2", project_id="proj2", workspace_path=ws2),
        ]

        # Initial scan
        await watcher.check()
        assert watcher.get_tracked_project_count() == 2

    @pytest.mark.asyncio
    async def test_check_writes_stub_on_modification(self, watcher, fake_db, fake_bus, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API v1"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan
        await watcher.check()

        # Modify file
        time.sleep(0.05)
        with open(os.path.join(ws, "specs", "api.md"), "w") as f:
            f.write("# API v2\n\nUpdated content")

        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "modified"

        # Stub should exist with updated content
        stub_path = watcher.get_stub_path("proj", "specs/api.md")
        assert os.path.isfile(stub_path)
        content = open(stub_path).read()
        assert "# API v2" in content

    @pytest.mark.asyncio
    async def test_check_deletes_stub_on_source_deletion(
        self, watcher, fake_db, fake_bus, tmp_path
    ):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [FakeWorkspace(id="ws1", project_id="proj", workspace_path=ws)]

        # Initial scan and create file
        await watcher.check()

        # Create a new file, scan again to create a stub for it
        new_file = os.path.join(ws, "specs", "database.md")
        with open(new_file, "w") as f:
            f.write("# Database")
        await watcher.check()

        stub_path = watcher.get_stub_path("proj", "specs/database.md")
        assert os.path.isfile(stub_path)

        # Delete the source file
        os.remove(new_file)
        changes = await watcher.check()

        assert len(changes) == 1
        assert changes[0].operation == "deleted"
        assert not os.path.isfile(stub_path)

    @pytest.mark.asyncio
    async def test_check_project_with_no_workspaces(self, watcher, fake_db, tmp_path):
        """Projects with no workspaces should be silently skipped."""
        fake_db.projects = [FakeProject(id="proj-no-ws")]
        fake_db.workspaces = []

        changes = await watcher.check()
        assert changes == []

    @pytest.mark.asyncio
    async def test_check_nonexistent_workspace_skipped(self, watcher, fake_db, tmp_path):
        """Workspaces with non-existent paths should be skipped."""
        fake_db.projects = [FakeProject(id="proj")]
        fake_db.workspaces = [
            FakeWorkspace(
                id="ws1",
                project_id="proj",
                workspace_path="/nonexistent/path",
            )
        ]

        changes = await watcher.check()
        assert changes == []


# ---------------------------------------------------------------------------
# WorkspaceSpecWatcher — introspection
# ---------------------------------------------------------------------------


class TestIntrospection:
    """Tests for introspection methods."""

    def test_get_stub_path(self, watcher):
        path = watcher.get_stub_path("proj", "specs/orchestrator.md")
        assert path.endswith(os.path.join("proj", "references", "spec-orchestrator.md"))

    def test_get_tracked_file_count(self, watcher, tmp_path):
        ws = _create_workspace(
            tmp_path,
            "proj",
            {
                "specs/api.md": "# API",
                "specs/db.md": "# DB",
                "docs/guide.md": "# Guide",
            },
        )
        watcher._scan_workspace("proj", ws)
        assert watcher.get_tracked_file_count() == 3

    def test_get_tracked_project_count(self, watcher, tmp_path):
        ws1 = _create_workspace(tmp_path, "proj1", {"specs/api.md": "# API"})
        ws2 = _create_workspace(tmp_path, "proj2", {"docs/guide.md": "# Guide"})
        watcher._scan_workspace("proj1", ws1)
        watcher._scan_workspace("proj2", ws2)
        assert watcher.get_tracked_project_count() == 2

    def test_get_snapshot(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)
        snap = watcher.get_snapshot("proj")
        assert snap is not None
        assert "specs/api.md" in snap
        assert isinstance(snap["specs/api.md"], SpecFileState)

    def test_get_snapshot_unknown_project(self, watcher):
        assert watcher.get_snapshot("unknown") is None

    def test_clear_snapshot(self, watcher, tmp_path):
        ws = _create_workspace(tmp_path, "proj", {"specs/api.md": "# API"})
        watcher._scan_workspace("proj", ws)
        assert watcher.get_snapshot("proj") is not None

        watcher.clear_snapshot("proj")
        assert watcher.get_snapshot("proj") is None
        assert watcher.get_tracked_project_count() == 0
