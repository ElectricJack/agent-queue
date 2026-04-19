"""Tests for src/readme_handler — project README.md vault watcher handler.

Covers Phase 6 implementation: actual orchestrator summary generation,
startup scanning, VaultWatcher dispatch integration, event bus
integration (Roadmap 6.4.2), and orchestrator memory integration
tests (Roadmap 6.4.3).
"""

from __future__ import annotations

import logging
import os
import time

import pytest

from src.event_bus import EventBus
from src.readme_handler import (
    README_PATTERN,
    ReadmeChangeInfo,
    derive_project_id,
    generate_summary_content,
    on_readme_changed,
    register_readme_handlers,
    scan_and_generate_readme_summaries,
    summary_path_for_project,
    _extract_title,
    _extract_section,
    _find_project_readmes,
    _write_summary,
    _remove_summary,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_project_id
# ---------------------------------------------------------------------------


class TestDeriveProjectId:
    """Tests for derive_project_id — extracting project_id from paths."""

    def test_simple_project(self):
        assert derive_project_id("projects/my-app/README.md") == "my-app"

    def test_project_with_dashes(self):
        assert derive_project_id("projects/mech-fighters/README.md") == "mech-fighters"

    def test_project_with_underscores(self):
        assert derive_project_id("projects/my_project/README.md") == "my_project"

    def test_single_word_project(self):
        assert derive_project_id("projects/webapp/README.md") == "webapp"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        assert derive_project_id("projects\\my-app\\README.md") == "my-app"

    def test_non_project_path_returns_none(self):
        assert derive_project_id("system/README.md") is None

    def test_nested_readme_returns_none(self):
        """READMEs deeper than projects/*/README.md should not match."""
        assert derive_project_id("projects/my-app/subdir/README.md") is None

    def test_wrong_filename_returns_none(self):
        assert derive_project_id("projects/my-app/readme.md") is None

    def test_empty_path_returns_none(self):
        assert derive_project_id("") is None

    def test_just_projects_returns_none(self):
        assert derive_project_id("projects/") is None

    def test_orchestrator_readme_returns_none(self):
        assert derive_project_id("orchestrator/README.md") is None


# ---------------------------------------------------------------------------
# ReadmeChangeInfo
# ---------------------------------------------------------------------------


class TestReadmeChangeInfo:
    """Tests for the ReadmeChangeInfo dataclass."""

    def test_creation(self):
        info = ReadmeChangeInfo(
            file_path="/vault/projects/app/README.md",
            change_type="modified",
            project_id="app",
        )
        assert info.file_path == "/vault/projects/app/README.md"
        assert info.change_type == "modified"
        assert info.project_id == "app"

    def test_frozen(self):
        info = ReadmeChangeInfo(
            file_path="/vault/projects/app/README.md",
            change_type="created",
            project_id="app",
        )
        with pytest.raises(AttributeError):
            info.project_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _extract_title / _extract_section
# ---------------------------------------------------------------------------


class TestExtractTitle:
    """Tests for _extract_title — pulling the first # heading."""

    def test_simple_heading(self):
        assert _extract_title("# My App\n\nSome text") == "My App"

    def test_heading_with_extra_spaces(self):
        assert _extract_title("#   Spaced Title  \ntext") == "Spaced Title"

    def test_no_heading(self):
        assert _extract_title("No heading here") == ""

    def test_empty_string(self):
        assert _extract_title("") == ""

    def test_only_h2_headings(self):
        assert _extract_title("## Subtitle\ntext") == ""

    def test_heading_not_first_line(self):
        assert _extract_title("Some preamble\n# The Real Title\nMore text") == "The Real Title"


class TestExtractSection:
    """Tests for _extract_section — pulling section bodies by heading."""

    def test_extracts_matching_section(self):
        text = "# Title\n## Getting Started\nStep 1\nStep 2\n## Other\nFoo"
        result = _extract_section(text, r"getting started")
        assert "Step 1" in result
        assert "Step 2" in result
        assert "Foo" not in result

    def test_case_insensitive_match(self):
        text = "## TECH STACK\nPython, SQLAlchemy\n## Next"
        result = _extract_section(text, r"tech.stack")
        assert "Python" in result

    def test_no_match_returns_empty(self):
        text = "## One\nBody\n## Two\nBody"
        assert _extract_section(text, r"nonexistent") == ""

    def test_last_section_includes_to_eof(self):
        text = "## First\nA\n## Target\nB\nC"
        result = _extract_section(text, r"target")
        assert "B" in result
        assert "C" in result


# ---------------------------------------------------------------------------
# generate_summary_content
# ---------------------------------------------------------------------------


class TestGenerateSummaryContent:
    """Tests for generate_summary_content — structured summary generation."""

    def test_contains_frontmatter(self):
        result = generate_summary_content(
            "my-app", "# My App\nDescription", timestamp="2026-04-09T00:00:00Z"
        )
        assert "---" in result
        assert 'project_id: "my-app"' in result
        assert "type: project-summary" in result
        assert "2026-04-09T00:00:00Z" in result

    def test_contains_title_from_readme(self):
        result = generate_summary_content("my-app", "# Cool Project\nText")
        assert "# Cool Project" in result

    def test_fallback_title_to_project_id(self):
        result = generate_summary_content("my-app", "No heading here, just text")
        # Title falls back to project_id
        assert "# my-app" in result

    def test_contains_readme_content(self):
        readme = "# App\n\n## Features\n- Fast\n- Reliable\n"
        result = generate_summary_content("app", readme)
        assert "## Features" in result
        assert "- Fast" in result
        assert "- Reliable" in result

    def test_source_path_in_frontmatter(self):
        result = generate_summary_content("my-app", "# X")
        assert 'source: "vault/projects/my-app/README.md"' in result

    def test_default_timestamp_is_utc(self):
        result = generate_summary_content("app", "# App")
        # Should contain a valid ISO timestamp
        assert "last_updated:" in result

    def test_trailing_newline(self):
        result = generate_summary_content("app", "# App")
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# summary_path_for_project
# ---------------------------------------------------------------------------


class TestSummaryPath:
    """Tests for summary_path_for_project."""

    def test_returns_expected_path(self):
        path = summary_path_for_project("/vault", "my-app")
        assert path == os.path.join("/vault", "agent-types", "supervisor", "memory", "project-my-app.md")

    def test_different_project_ids(self):
        for pid in ("webapp", "mech-fighters", "test_project"):
            path = summary_path_for_project("/v", pid)
            assert path.endswith(f"project-{pid}.md")


# ---------------------------------------------------------------------------
# _write_summary / _remove_summary
# ---------------------------------------------------------------------------


class TestWriteRemoveSummary:
    """Tests for _write_summary and _remove_summary."""

    def test_write_creates_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = _write_summary(str(vault), "my-app", "# My App\nDescription")

        assert os.path.isfile(path)
        content = open(path).read()
        assert 'project_id: "my-app"' in content
        assert "# My App" in content

    def test_write_creates_directories(self, tmp_path):
        vault = tmp_path / "vault"
        # Don't create vault dir — _write_summary should create it
        path = _write_summary(str(vault), "app", "# App")
        assert os.path.isfile(path)

    def test_write_overwrites_existing(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_summary(str(vault), "app", "# Version 1")
        path = _write_summary(str(vault), "app", "# Version 2")

        content = open(path).read()
        assert "# Version 2" in content
        assert "# Version 1" not in content

    def test_remove_deletes_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = _write_summary(str(vault), "app", "# App")
        assert os.path.isfile(path)

        removed = _remove_summary(str(vault), "app")
        assert removed is True
        assert not os.path.isfile(path)

    def test_remove_nonexistent_returns_false(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        removed = _remove_summary(str(vault), "no-such-project")
        assert removed is False


# ---------------------------------------------------------------------------
# on_readme_changed (Phase 6 handler)
# ---------------------------------------------------------------------------


class TestOnReadmeChanged:
    """Tests for the Phase 6 handler — actual summary generation."""

    @pytest.mark.asyncio
    async def test_created_generates_summary(self, tmp_path):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n\nA cool application.\n")

        change = VaultChange(
            path=str(readme),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success
        assert results[0].action == "created"
        assert results[0].project_id == "my-app"

        # Verify the summary file was written
        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()
        content = summary.read_text()
        assert "# My App" in content
        assert "A cool application" in content

    @pytest.mark.asyncio
    async def test_modified_updates_summary(self, tmp_path):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App v2\n\nUpdated description.\n")

        # Pre-create an old summary
        _write_summary(str(vault), "my-app", "# My App v1\nOld content.")

        change = VaultChange(
            path=str(readme),
            rel_path="projects/my-app/README.md",
            operation="modified",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success
        assert results[0].action == "updated"

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        content = summary.read_text()
        assert "Updated description" in content
        assert "Old content" not in content

    @pytest.mark.asyncio
    async def test_deleted_removes_summary(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Pre-create a summary
        _write_summary(str(vault), "my-app", "# My App\nContent.")

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="deleted",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success
        assert results[0].action == "removed"
        assert not summary.is_file()

    @pytest.mark.asyncio
    async def test_deleted_nonexistent_summary_skips(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="deleted",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success
        assert results[0].action == "skipped"

    @pytest.mark.asyncio
    async def test_multiple_changes_processed(self, tmp_path):
        vault = tmp_path / "vault"
        for pid in ("app-one", "app-two"):
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {pid}\n")

        changes = [
            VaultChange(
                path=str(vault / "projects" / "app-one" / "README.md"),
                rel_path="projects/app-one/README.md",
                operation="created",
            ),
            VaultChange(
                path=str(vault / "projects" / "app-two" / "README.md"),
                rel_path="projects/app-two/README.md",
                operation="created",
            ),
        ]
        results = await on_readme_changed(changes, vault_root=str(vault))

        assert len(results) == 2
        assert all(r.success for r in results)

        for pid in ("app-one", "app-two"):
            summary = vault / "agent-types" / "supervisor" / "memory" / f"project-{pid}.md"
            assert summary.is_file()

    @pytest.mark.asyncio
    async def test_unparseable_path_skipped(self, tmp_path):
        change = VaultChange(
            path="/vault/system/README.md",
            rel_path="system/README.md",
            operation="modified",
        )
        results = await on_readme_changed([change], vault_root=str(tmp_path))

        # Skipped entirely (no result entry)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_empty_changes_returns_empty(self):
        results = await on_readme_changed([])
        assert results == []

    @pytest.mark.asyncio
    async def test_unreadable_file_produces_error(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        # File doesn't exist — read will fail
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert not results[0].success
        assert results[0].action == "error"

    @pytest.mark.asyncio
    async def test_derives_vault_root_from_path(self, tmp_path):
        """When vault_root is not provided, derive it from the change path."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n")

        change = VaultChange(
            path=str(readme),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        # Don't pass vault_root — handler derives it
        results = await on_readme_changed([change])

        assert len(results) == 1
        assert results[0].success

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()

    @pytest.mark.asyncio
    async def test_logs_info_on_success(self, tmp_path, caplog):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text("# My App\n")

        change = VaultChange(
            path=str(proj_dir / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed([change], vault_root=str(vault))

        info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("my-app" in r.message and "created" in r.message for r in info_logs)


# ---------------------------------------------------------------------------
# register_readme_handlers
# ---------------------------------------------------------------------------


class TestRegisterReadmeHandlers:
    """Tests for register_readme_handlers — wiring pattern to VaultWatcher."""

    def test_registers_one_handler(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_id = register_readme_handlers(watcher)

        assert isinstance(handler_id, str)
        assert watcher.get_handler_count() == 1

    def test_handler_id_includes_pattern(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_id = register_readme_handlers(watcher)

        assert handler_id == "readme:projects/*/README.md"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice with explicit ID overwrites — no duplicates."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        id1 = register_readme_handlers(watcher)
        id2 = register_readme_handlers(watcher)

        assert id1 == id2
        assert watcher.get_handler_count() == 1

    def test_vault_root_passed_to_callback(self, tmp_path):
        """When vault_root is provided, the callback should use it."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        register_readme_handlers(watcher, vault_root=str(tmp_path / "vault"))
        # Handler registered — the callback is a closure wrapping vault_root
        assert watcher.get_handler_count() == 1


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that README_PATTERN matches the expected paths."""

    def test_matches_project_readme(self):
        assert VaultWatcher._matches_pattern("projects/my-app/README.md", README_PATTERN)

    def test_matches_project_with_dashes(self):
        assert VaultWatcher._matches_pattern("projects/mech-fighters/README.md", README_PATTERN)

    def test_matches_project_with_underscores(self):
        assert VaultWatcher._matches_pattern("projects/my_project/README.md", README_PATTERN)

    def test_nested_readme_matches_pattern_but_rejected_by_handler(self):
        """fnmatch's * matches path separators, so nested READMEs do match
        the glob pattern.  However, derive_project_id correctly rejects them
        (returns None), so the handler logs a warning instead of processing."""
        # The pattern matches (fnmatch quirk) ...
        assert VaultWatcher._matches_pattern("projects/my-app/subdir/README.md", README_PATTERN)
        # ... but derive_project_id rejects it
        assert derive_project_id("projects/my-app/subdir/README.md") is None

    def test_does_not_match_system_readme(self):
        assert not VaultWatcher._matches_pattern("system/README.md", README_PATTERN)

    def test_does_not_match_orchestrator_readme(self):
        assert not VaultWatcher._matches_pattern("orchestrator/README.md", README_PATTERN)

    def test_does_not_match_lowercase_readme(self):
        assert not VaultWatcher._matches_pattern("projects/my-app/readme.md", README_PATTERN)

    def test_does_not_match_non_md_readme(self):
        assert not VaultWatcher._matches_pattern("projects/my-app/README.txt", README_PATTERN)

    def test_does_not_match_memory_files(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/memory/knowledge/arch.md", README_PATTERN
        )

    def test_does_not_match_playbook_files(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md", README_PATTERN
        )


# ---------------------------------------------------------------------------
# _find_project_readmes
# ---------------------------------------------------------------------------


class TestFindProjectReadmes:
    """Tests for _find_project_readmes — discovery helper."""

    def test_finds_existing_readmes(self, tmp_path):
        vault = tmp_path / "vault"
        for pid in ("app-one", "app-two"):
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {pid}\n")

        results = _find_project_readmes(str(vault))
        assert len(results) == 2
        project_ids = {derive_project_id(rel) for _, rel in results}
        assert project_ids == {"app-one", "app-two"}

    def test_skips_projects_without_readme(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "projects" / "has-readme").mkdir(parents=True)
        (vault / "projects" / "has-readme" / "README.md").write_text("# Has\n")
        (vault / "projects" / "no-readme").mkdir(parents=True)

        results = _find_project_readmes(str(vault))
        assert len(results) == 1
        assert "has-readme" in results[0][1]

    def test_empty_vault_returns_empty(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        assert _find_project_readmes(str(vault)) == []

    def test_nonexistent_vault_returns_empty(self, tmp_path):
        assert _find_project_readmes(str(tmp_path / "no-such")) == []

    def test_sorted_by_project_id(self, tmp_path):
        vault = tmp_path / "vault"
        for pid in ("zebra", "alpha", "middle"):
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {pid}\n")

        results = _find_project_readmes(str(vault))
        project_ids = [derive_project_id(rel) for _, rel in results]
        assert project_ids == ["alpha", "middle", "zebra"]


# ---------------------------------------------------------------------------
# scan_and_generate_readme_summaries (startup scan)
# ---------------------------------------------------------------------------


class TestScanAndGenerateReadmeSummaries:
    """Tests for the startup scan function."""

    @pytest.mark.asyncio
    async def test_generates_summaries_for_all_readmes(self, tmp_path):
        vault = tmp_path / "vault"
        for pid in ("app-one", "app-two", "app-three"):
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {pid}\n\nDescription of {pid}.\n")

        results = await scan_and_generate_readme_summaries(str(vault))

        assert len(results) == 3
        assert all(r.success for r in results)

        for pid in ("app-one", "app-two", "app-three"):
            summary = vault / "agent-types" / "supervisor" / "memory" / f"project-{pid}.md"
            assert summary.is_file()
            content = summary.read_text()
            assert f"Description of {pid}" in content

    @pytest.mark.asyncio
    async def test_skips_uptodate_summaries(self, tmp_path):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text("# My App\n")

        # First scan generates
        results1 = await scan_and_generate_readme_summaries(str(vault))
        assert results1[0].action in ("created", "updated")

        # Second scan should skip (summary is up-to-date)
        results2 = await scan_and_generate_readme_summaries(str(vault))
        assert results2[0].action == "skipped"

    @pytest.mark.asyncio
    async def test_regenerates_stale_summary(self, tmp_path):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App v1\n")

        # Generate initial summary
        await scan_and_generate_readme_summaries(str(vault))

        # Update README with newer mtime
        time.sleep(0.05)
        readme.write_text("# My App v2\n")

        # Re-scan should regenerate
        results = await scan_and_generate_readme_summaries(str(vault))
        assert results[0].action == "updated"

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        content = summary.read_text()
        assert "v2" in content

    @pytest.mark.asyncio
    async def test_empty_vault_returns_empty(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        results = await scan_and_generate_readme_summaries(str(vault))
        assert results == []

    @pytest.mark.asyncio
    async def test_nonexistent_vault_returns_empty(self, tmp_path):
        results = await scan_and_generate_readme_summaries(str(tmp_path / "nope"))
        assert results == []

    @pytest.mark.asyncio
    async def test_mixed_readmes_and_empty_dirs(self, tmp_path):
        vault = tmp_path / "vault"
        # Project with README
        proj_dir = vault / "projects" / "has-readme"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text("# Has README\n")
        # Project without README
        (vault / "projects" / "no-readme").mkdir(parents=True)

        results = await scan_and_generate_readme_summaries(str(vault))
        assert len(results) == 1
        assert results[0].project_id == "has-readme"

    @pytest.mark.asyncio
    async def test_logs_scan_summary(self, tmp_path, caplog):
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text("# My App\n")

        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await scan_and_generate_readme_summaries(str(vault))

        info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("scan complete" in r.message.lower() for r in info_logs)


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects README change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change -> VaultWatcher -> handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_readme_create(self, tmp_path):
        """Create a project README and verify the handler is called."""
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

        watcher.register_handler(README_PATTERN, capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create project README
        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "README.md").write_text("# My App\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/README.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_readme_modification(self, tmp_path):
        """Modify an existing README and verify dispatch."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(README_PATTERN, capture_handler)

        # Initial snapshot includes existing file
        await watcher.check()

        # Modify the file (need different mtime)
        time.sleep(0.05)
        readme.write_text("# My App\n\nUpdated description.\n")

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/README.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_detects_readme_deletion(self, tmp_path):
        """Delete a README and verify dispatch."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(README_PATTERN, capture_handler)

        # Initial snapshot
        await watcher.check()

        # Delete the README
        readme.unlink()

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/README.md"
        assert dispatched[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_multiple_projects_dispatched(self, tmp_path):
        """READMEs from multiple projects should all be dispatched."""
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

        watcher.register_handler(README_PATTERN, capture_handler)

        # Initial snapshot
        await watcher.check()

        # Create READMEs for multiple projects
        for project_id in ("app-one", "app-two", "app-three"):
            proj_dir = vault / "projects" / project_id
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {project_id}\n")

        await watcher.check()

        assert len(dispatched) == 3
        project_ids = {derive_project_id(c.rel_path) for c in dispatched}
        assert project_ids == {"app-one", "app-two", "app-three"}

    @pytest.mark.asyncio
    async def test_non_readme_file_not_dispatched(self, tmp_path):
        """Non-README files in project directories should not trigger handler."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(README_PATTERN, capture_handler)
        await watcher.check()

        # Create a non-README file
        (proj_dir / "notes.md").write_text("# Notes\n")

        await watcher.check()

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_full_handler_generates_summary(self, tmp_path):
        """Register via register_readme_handlers and verify summary is generated."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot
        await watcher.check()

        # Create a project README
        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "README.md").write_text("# My App\n\nA great project.\n")

        await watcher.check()

        # Summary should have been generated
        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()
        content = summary.read_text()
        assert "A great project" in content
        assert 'project_id: "my-app"' in content

    @pytest.mark.asyncio
    async def test_full_handler_updates_summary_on_modify(self, tmp_path):
        """Register via register_readme_handlers and verify summary updates on modify."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n\nVersion 1.\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot (includes existing README)
        await watcher.check()

        # Modify the README
        time.sleep(0.05)
        readme.write_text("# My App\n\nVersion 2 — updated.\n")

        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()
        content = summary.read_text()
        assert "Version 2" in content
        assert "Version 1" not in content

    @pytest.mark.asyncio
    async def test_full_handler_removes_summary_on_delete(self, tmp_path):
        """Register via register_readme_handlers and verify summary removed on delete."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot — README exists
        await watcher.check()

        # Create summary by modifying the README
        time.sleep(0.05)
        readme.write_text("# My App\n\nUpdated.\n")
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert summary.is_file()

        # Delete the README
        readme.unlink()
        await watcher.check()

        assert not summary.is_file()

    @pytest.mark.asyncio
    async def test_full_lifecycle_create_modify_delete(self, tmp_path):
        """End-to-end lifecycle: create → modify → delete through VaultWatcher."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot (empty)
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"

        # Phase 1: Create
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n\nInitial version.\n")
        await watcher.check()

        assert summary.is_file()
        assert "Initial version" in summary.read_text()

        # Phase 2: Modify
        time.sleep(0.05)
        readme.write_text("# My App\n\nRevised version.\n")
        await watcher.check()

        assert summary.is_file()
        content = summary.read_text()
        assert "Revised version" in content
        assert "Initial version" not in content

        # Phase 3: Delete
        readme.unlink()
        await watcher.check()

        assert not summary.is_file()


# ---------------------------------------------------------------------------
# Event bus integration (Roadmap 6.4.2)
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    """Tests for event emission when README summaries are created/updated/removed."""

    @pytest.mark.asyncio
    async def test_emits_event_on_create(self, tmp_path):
        """Emits notify.readme_summary_updated with action='created'."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n")

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        change = VaultChange(
            path=str(readme),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        await on_readme_changed([change], vault_root=str(vault), event_bus=bus)

        assert len(events) == 1
        assert events[0]["action"] == "created"
        assert events[0]["project_id"] == "my-app"
        assert "source_path" in events[0]
        assert "summary_path" in events[0]

    @pytest.mark.asyncio
    async def test_emits_event_on_update(self, tmp_path):
        """Emits notify.readme_summary_updated with action='updated'."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n\nUpdated.\n")

        # Pre-create summary
        _write_summary(str(vault), "my-app", "# Old\n")

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        change = VaultChange(
            path=str(readme),
            rel_path="projects/my-app/README.md",
            operation="modified",
        )
        await on_readme_changed([change], vault_root=str(vault), event_bus=bus)

        assert len(events) == 1
        assert events[0]["action"] == "updated"
        assert events[0]["project_id"] == "my-app"

    @pytest.mark.asyncio
    async def test_emits_event_on_delete(self, tmp_path):
        """Emits notify.readme_summary_updated with action='removed'."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Pre-create summary
        _write_summary(str(vault), "my-app", "# My App\n")

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="deleted",
        )
        await on_readme_changed([change], vault_root=str(vault), event_bus=bus)

        assert len(events) == 1
        assert events[0]["action"] == "removed"
        assert events[0]["project_id"] == "my-app"

    @pytest.mark.asyncio
    async def test_no_event_on_delete_nonexistent_summary(self, tmp_path):
        """No event emitted when deleting a README that has no summary."""
        vault = tmp_path / "vault"
        vault.mkdir()

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="deleted",
        )
        await on_readme_changed([change], vault_root=str(vault), event_bus=bus)

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_emits_failed_event_on_read_error(self, tmp_path):
        """Emits notify.readme_summary_failed when README cannot be read."""
        vault = tmp_path / "vault"
        vault.mkdir()

        bus = EventBus(validate_events=False)
        failed_events: list[dict] = []
        bus.subscribe("notify.readme_summary_failed", lambda data: failed_events.append(data))

        change = VaultChange(
            path=str(vault / "projects" / "my-app" / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        # File doesn't exist — will fail to read
        await on_readme_changed([change], vault_root=str(vault), event_bus=bus)

        assert len(failed_events) == 1
        assert failed_events[0]["project_id"] == "my-app"
        assert len(failed_events[0]["errors"]) > 0

    @pytest.mark.asyncio
    async def test_no_events_without_event_bus(self, tmp_path):
        """When event_bus is None, no errors are raised."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text("# My App\n")

        change = VaultChange(
            path=str(proj_dir / "README.md"),
            rel_path="projects/my-app/README.md",
            operation="created",
        )
        # Should work fine without event_bus
        results = await on_readme_changed([change], vault_root=str(vault))
        assert len(results) == 1
        assert results[0].success

    @pytest.mark.asyncio
    async def test_event_bus_wired_through_register(self, tmp_path):
        """Event bus is propagated through register_readme_handlers."""
        vault = tmp_path / "vault"
        vault.mkdir()

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault), event_bus=bus)

        # Initial snapshot
        await watcher.check()

        # Create a project README
        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "README.md").write_text("# My App\n")

        await watcher.check()

        assert len(events) == 1
        assert events[0]["action"] == "created"
        assert events[0]["project_id"] == "my-app"

    @pytest.mark.asyncio
    async def test_multiple_events_in_batch(self, tmp_path):
        """Multiple README changes produce one event per project."""
        vault = tmp_path / "vault"
        for pid in ("app-one", "app-two"):
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(f"# {pid}\n")

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        changes = [
            VaultChange(
                path=str(vault / "projects" / pid / "README.md"),
                rel_path=f"projects/{pid}/README.md",
                operation="created",
            )
            for pid in ("app-one", "app-two")
        ]
        await on_readme_changed(changes, vault_root=str(vault), event_bus=bus)

        assert len(events) == 2
        project_ids = {e["project_id"] for e in events}
        assert project_ids == {"app-one", "app-two"}

    @pytest.mark.asyncio
    async def test_full_lifecycle_with_events(self, tmp_path):
        """Create → modify → delete lifecycle emits correct events."""
        vault = tmp_path / "vault"
        vault.mkdir()

        bus = EventBus(validate_events=False)
        events: list[dict] = []
        bus.subscribe("notify.readme_summary_updated", lambda data: events.append(data))

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher, vault_root=str(vault), event_bus=bus)

        # Initial snapshot
        await watcher.check()

        # Create
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text("# My App\n\nVersion 1.\n")
        await watcher.check()

        assert len(events) == 1
        assert events[0]["action"] == "created"

        # Modify
        time.sleep(0.05)
        readme.write_text("# My App\n\nVersion 2.\n")
        await watcher.check()

        assert len(events) == 2
        assert events[1]["action"] == "updated"

        # Delete
        readme.unlink()
        await watcher.check()

        assert len(events) == 3
        assert events[2]["action"] == "removed"

        # All events for same project
        assert all(e["project_id"] == "my-app" for e in events)


# ---------------------------------------------------------------------------
# Orchestrator memory from project READMEs (Roadmap 6.4.3)
# ---------------------------------------------------------------------------

# Realistic README content for integration tests.
_REALISTIC_README = """\
# My Web App

A full-stack web application for managing team tasks and collaboration.

## Purpose

MyWebApp helps distributed teams coordinate work by providing real-time
task boards, threaded discussions, and automated status reporting. It
replaces our previous spreadsheet-based workflow.

## Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy
- **Frontend:** React 18, TypeScript
- **Database:** PostgreSQL 16
- **Infrastructure:** Docker Compose, GitHub Actions CI

## Current Status

Active development. Core API and task-board UI are stable. Currently
working on the notification subsystem and Slack integration.

## Getting Started

```bash
docker compose up -d
pip install -e ".[dev]"
pytest tests/
```
"""

_REALISTIC_README_UPDATED = """\
# My Web App

A full-stack web application for managing team tasks and collaboration.

## Purpose

MyWebApp helps distributed teams coordinate work by providing real-time
task boards, threaded discussions, and automated status reporting. It
replaces our previous spreadsheet-based workflow. **Now with Slack integration!**

## Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy
- **Frontend:** React 18, TypeScript
- **Database:** PostgreSQL 16
- **Infrastructure:** Docker Compose, GitHub Actions CI
- **Integrations:** Slack Bot SDK

## Current Status

Entering beta. Notification subsystem and Slack integration are complete.
Focus shifting to performance tuning and load testing.

## Getting Started

```bash
docker compose up -d
pip install -e ".[dev]"
pytest tests/
```
"""


class TestOrchestratorMemoryFromProjectReadmes:
    """Roadmap 6.4.3 — integration tests for orchestrator memory via project READMEs.

    Each test method maps to a specific acceptance criterion:
        (a) README creation triggers summary generation
        (b) Summary captures key project details
        (c) README modification triggers summary update
        (d) Startup scan processes all READMEs
        (e) Project with no README handled gracefully
        (f) README deletion removes/flags summary
        (g) Summary conciseness
    """

    # (a) Creating vault/projects/myapp/README.md triggers generation of
    #     vault/orchestrator/memory/project-myapp.md
    @pytest.mark.asyncio
    async def test_readme_creation_triggers_summary_generation(self, tmp_path):
        """(a) A new project README creates a summary in orchestrator memory."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot (empty vault)
        await watcher.check()

        # Create the project README
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text(_REALISTIC_README)

        # Watcher detects the new file and dispatches the handler
        await watcher.check()

        # Verify the summary was generated at the expected path
        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"
        assert summary.is_file(), "Summary file should be created in orchestrator memory"

        content = summary.read_text()
        assert 'project_id: "myapp"' in content
        assert 'source: "vault/projects/myapp/README.md"' in content
        assert "type: project-summary" in content

    # (b) Summary captures key project details (tech stack, purpose, status)
    @pytest.mark.asyncio
    async def test_summary_captures_key_project_details(self, tmp_path):
        """(b) The summary preserves tech stack, purpose, and status from the README."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text(_REALISTIC_README)

        change = VaultChange(
            path=str(proj_dir / "README.md"),
            rel_path="projects/myapp/README.md",
            operation="created",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"
        content = summary.read_text()

        # Title captured
        assert "# My Web App" in content

        # Purpose section captured
        assert "distributed teams coordinate work" in content

        # Tech stack details captured
        assert "Python 3.12" in content
        assert "FastAPI" in content
        assert "React 18" in content
        assert "PostgreSQL 16" in content

        # Current status captured
        assert "Active development" in content
        assert "notification subsystem" in content

    # (c) Editing README triggers summary update — new content reflected
    @pytest.mark.asyncio
    async def test_readme_modification_triggers_summary_update(self, tmp_path):
        """(c) Modifying a README updates the summary with new content."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_readme_handlers(watcher, vault_root=str(vault))

        # Phase 1: Create initial README and summary
        readme.write_text(_REALISTIC_README)
        await watcher.check()  # initial snapshot
        # Force a create event (VaultWatcher needs pre-snapshot for modify)
        time.sleep(0.05)
        readme.write_text(_REALISTIC_README)  # re-trigger with same content to establish baseline
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"
        # May or may not exist yet depending on watcher behavior; use direct handler as fallback
        if not summary.is_file():
            change = VaultChange(
                path=str(readme),
                rel_path="projects/myapp/README.md",
                operation="created",
            )
            await on_readme_changed([change], vault_root=str(vault))

        assert summary.is_file()
        old_content = summary.read_text()
        assert "Active development" in old_content

        # Phase 2: Update README with new content
        time.sleep(0.05)
        readme.write_text(_REALISTIC_README_UPDATED)
        await watcher.check()

        new_content = summary.read_text()

        # New content reflected
        assert "Entering beta" in new_content
        assert "Slack Bot SDK" in new_content
        assert "Now with Slack integration" in new_content

        # Old content replaced
        assert "Active development" not in new_content

    # (d) Startup scan processes all existing READMEs
    @pytest.mark.asyncio
    async def test_startup_scan_processes_all_readmes(self, tmp_path):
        """(d) Startup scan creates summaries for every project with a README."""
        vault = tmp_path / "vault"

        # Pre-create READMEs for multiple projects
        project_data = {
            "frontend": "# Frontend\n\n## Tech Stack\n- React, TypeScript\n",
            "backend": "# Backend API\n\n## Tech Stack\n- Python, FastAPI\n",
            "infra": "# Infrastructure\n\n## Purpose\nCI/CD and deployment.\n",
        }
        for pid, content in project_data.items():
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(content)

        # Run startup scan
        results = await scan_and_generate_readme_summaries(str(vault))

        # All three READMEs processed successfully
        assert len(results) == 3
        assert all(r.success for r in results)
        project_ids = {r.project_id for r in results}
        assert project_ids == {"frontend", "backend", "infra"}

        # Each project has a summary with its specific content
        for pid, content in project_data.items():
            summary = vault / "agent-types" / "supervisor" / "memory" / f"project-{pid}.md"
            assert summary.is_file(), f"Summary for {pid} should exist"
            summary_text = summary.read_text()
            assert f'project_id: "{pid}"' in summary_text
            # Spot-check content propagation
            if pid == "frontend":
                assert "React" in summary_text
            elif pid == "backend":
                assert "FastAPI" in summary_text
            elif pid == "infra":
                assert "CI/CD" in summary_text

    # (e) Project with no README does not cause errors (skipped gracefully)
    @pytest.mark.asyncio
    async def test_missing_readme_handled_gracefully(self, tmp_path):
        """(e) Projects without a README are skipped — no errors raised."""
        vault = tmp_path / "vault"

        # Create some projects — only some have READMEs
        (vault / "projects" / "has-readme").mkdir(parents=True)
        (vault / "projects" / "has-readme" / "README.md").write_text(
            "# Has README\n\nA project with a README.\n"
        )
        (vault / "projects" / "no-readme").mkdir(parents=True)
        # no-readme has a directory but no README.md
        (vault / "projects" / "empty-dir").mkdir(parents=True)
        # empty-dir also has no README

        # Startup scan should process gracefully
        results = await scan_and_generate_readme_summaries(str(vault))

        # Only the project with a README is processed
        assert len(results) == 1
        assert results[0].project_id == "has-readme"
        assert results[0].success

        # No summary for projects without READMEs — and no crashes
        for pid in ("no-readme", "empty-dir"):
            summary = vault / "agent-types" / "supervisor" / "memory" / f"project-{pid}.md"
            assert not summary.is_file(), f"No summary should exist for {pid}"

    # (e) additional: watcher does not error on missing-README projects
    @pytest.mark.asyncio
    async def test_watcher_ignores_non_readme_project_files(self, tmp_path):
        """(e) Creating non-README files in project dirs causes no handler errors."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot
        await watcher.check()

        # Create project with non-README files only
        proj_dir = vault / "projects" / "my-app"
        proj_dir.mkdir(parents=True)
        (proj_dir / "config.yaml").write_text("key: value\n")
        (proj_dir / "notes.md").write_text("# Notes\n")

        # Should not trigger the handler or create a summary
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-my-app.md"
        assert not summary.is_file()

    # (f) Deleting README removes the orchestrator summary
    @pytest.mark.asyncio
    async def test_readme_deletion_removes_summary(self, tmp_path):
        """(f) Deleting a README removes its orchestrator summary."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text(_REALISTIC_README)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_readme_handlers(watcher, vault_root=str(vault))

        # Create + snapshot
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"

        # If watcher didn't generate summary on first check (pre-existing file),
        # force it via the handler
        if not summary.is_file():
            change = VaultChange(
                path=str(readme),
                rel_path="projects/myapp/README.md",
                operation="created",
            )
            await on_readme_changed([change], vault_root=str(vault))

        assert summary.is_file(), "Summary should exist before deletion"

        # Delete the README
        readme.unlink()
        await watcher.check()

        assert not summary.is_file(), "Summary should be removed after README deletion"

    # (f) additional: deletion via direct handler call
    @pytest.mark.asyncio
    async def test_readme_deletion_via_handler_removes_summary(self, tmp_path):
        """(f) Direct handler call for deleted README removes summary file."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Pre-create a summary
        _write_summary(str(vault), "myapp", _REALISTIC_README)
        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"
        assert summary.is_file()

        # Simulate deletion event
        change = VaultChange(
            path=str(vault / "projects" / "myapp" / "README.md"),
            rel_path="projects/myapp/README.md",
            operation="deleted",
        )
        results = await on_readme_changed([change], vault_root=str(vault))

        assert len(results) == 1
        assert results[0].success
        assert results[0].action == "removed"
        assert not summary.is_file()

    # (g) Summary is concise enough for orchestrator's context
    @pytest.mark.asyncio
    async def test_summary_is_concise(self, tmp_path):
        """(g) Summary overhead (frontmatter, structure) is minimal relative to README."""
        vault = tmp_path / "vault"
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        (proj_dir / "README.md").write_text(_REALISTIC_README)

        change = VaultChange(
            path=str(proj_dir / "README.md"),
            rel_path="projects/myapp/README.md",
            operation="created",
        )
        await on_readme_changed([change], vault_root=str(vault))

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"
        content = summary.read_text()

        # The summary overhead (frontmatter + title line) should be small.
        # Full README is ~600 chars; total summary should be under 2x that.
        readme_len = len(_REALISTIC_README)
        summary_len = len(content)
        overhead = summary_len - readme_len

        # Frontmatter + title wrapper should add < 300 bytes of overhead
        assert overhead < 300, (
            f"Summary overhead is {overhead} bytes — should be under 300. "
            f"README={readme_len}, summary={summary_len}"
        )

    @pytest.mark.asyncio
    async def test_multiple_summaries_fit_in_context(self, tmp_path):
        """(g) Multiple project summaries are small enough to coexist in context."""
        vault = tmp_path / "vault"

        # Create 10 projects with realistic READMEs
        for i in range(10):
            pid = f"project-{i:02d}"
            proj_dir = vault / "projects" / pid
            proj_dir.mkdir(parents=True)
            (proj_dir / "README.md").write_text(
                f"# Project {i}\n\n"
                f"## Purpose\nProject {i} handles module {i} of the system.\n\n"
                f"## Tech Stack\n- Python, FastAPI\n\n"
                f"## Status\nActive development.\n"
            )

        results = await scan_and_generate_readme_summaries(str(vault))

        assert len(results) == 10
        assert all(r.success for r in results)

        # Measure total size of all summaries
        total_size = 0
        for i in range(10):
            pid = f"project-{i:02d}"
            summary = vault / "agent-types" / "supervisor" / "memory" / f"project-{pid}.md"
            assert summary.is_file()
            total_size += len(summary.read_text())

        # 10 project summaries should fit comfortably in an LLM context.
        # A reasonable budget: < 50KB for 10 projects.
        assert total_size < 50_000, (
            f"Total size of 10 project summaries is {total_size} bytes — "
            f"should be under 50KB to fit in orchestrator context"
        )

    # Integration: full lifecycle with realistic content
    @pytest.mark.asyncio
    async def test_full_lifecycle_with_realistic_content(self, tmp_path):
        """Full create → modify → delete lifecycle with realistic README content."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_readme_handlers(watcher, vault_root=str(vault))

        # Initial snapshot
        await watcher.check()

        summary = vault / "agent-types" / "supervisor" / "memory" / "project-myapp.md"

        # Phase 1: Create README
        proj_dir = vault / "projects" / "myapp"
        proj_dir.mkdir(parents=True)
        readme = proj_dir / "README.md"
        readme.write_text(_REALISTIC_README)
        await watcher.check()

        assert summary.is_file()
        content = summary.read_text()
        assert "Active development" in content
        assert "Python 3.12" in content

        # Phase 2: Modify README (project evolves)
        time.sleep(0.05)
        readme.write_text(_REALISTIC_README_UPDATED)
        await watcher.check()

        content = summary.read_text()
        assert "Entering beta" in content
        assert "Slack Bot SDK" in content
        assert "Active development" not in content

        # Phase 3: Delete README (project removed)
        readme.unlink()
        await watcher.check()

        assert not summary.is_file()
