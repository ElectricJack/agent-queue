"""Tests for src/readme_handler — project README.md vault watcher handler.

Covers Phase 6 implementation: actual orchestrator summary generation,
startup scanning, VaultWatcher dispatch integration, and event bus
integration (Roadmap 6.4.2 — README change triggers summary update).
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
        assert path == os.path.join("/vault", "orchestrator", "memory", "project-my-app.md")

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
        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
        content = summary.read_text()
        assert "Updated description" in content
        assert "Old content" not in content

    @pytest.mark.asyncio
    async def test_deleted_removes_summary(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Pre-create a summary
        _write_summary(str(vault), "my-app", "# My App\nContent.")

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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
            summary = vault / "orchestrator" / "memory" / f"project-{pid}.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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
            summary = vault / "orchestrator" / "memory" / f"project-{pid}.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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
        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"
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

        summary = vault / "orchestrator" / "memory" / "project-my-app.md"

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
