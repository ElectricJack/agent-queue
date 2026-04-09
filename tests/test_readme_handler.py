"""Tests for src/readme_handler — project README.md vault watcher handler."""

from __future__ import annotations

import logging
import time

import pytest

from src.readme_handler import (
    README_PATTERN,
    ReadmeChangeInfo,
    derive_project_id,
    on_readme_changed,
    register_readme_handlers,
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
# on_readme_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnReadmeChanged:
    """Tests for the stub handler on_readme_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/projects/my-app/README.md",
            rel_path="projects/my-app/README.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "README" in record.message
        assert "modified" in record.message
        assert "my-app" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/projects/app-one/README.md",
                rel_path="projects/app-one/README.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/projects/app-two/README.md",
                rel_path="projects/app-two/README.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app-three/README.md",
                rel_path="projects/app-three/README.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed(changes)

        assert len(caplog.records) == 3
        messages = [r.message for r in caplog.records]
        assert any("app-one" in m and "created" in m for m in messages)
        assert any("app-two" in m and "modified" in m for m in messages)
        assert any("app-three" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_project_id(self, caplog):
        """Verify project_id derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/README.md",
            rel_path="projects/mech-fighters/README.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed([change])

        assert "mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_warns_on_unparseable_path(self, caplog):
        """If project_id cannot be derived, a warning is logged."""
        change = VaultChange(
            path="/vault/system/README.md",
            rel_path="system/README.md",
            operation="modified",
        )
        with caplog.at_level(logging.WARNING, logger="src.readme_handler"):
            await on_readme_changed([change])

        warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_logs) == 1
        assert "could not derive project_id" in warning_logs[0].message

    @pytest.mark.asyncio
    async def test_log_mentions_phase_6(self, caplog):
        """Log message should reference Phase 6 for future implementation."""
        change = VaultChange(
            path="/vault/projects/app/README.md",
            rel_path="projects/app/README.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await on_readme_changed([change])

        assert "Phase 6" in caplog.records[0].message


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


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that README_PATTERN matches the expected paths."""

    def test_matches_project_readme(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/README.md", README_PATTERN
        )

    def test_matches_project_with_dashes(self):
        assert VaultWatcher._matches_pattern(
            "projects/mech-fighters/README.md", README_PATTERN
        )

    def test_matches_project_with_underscores(self):
        assert VaultWatcher._matches_pattern(
            "projects/my_project/README.md", README_PATTERN
        )

    def test_nested_readme_matches_pattern_but_rejected_by_handler(self):
        """fnmatch's * matches path separators, so nested READMEs do match
        the glob pattern.  However, derive_project_id correctly rejects them
        (returns None), so the handler logs a warning instead of processing."""
        # The pattern matches (fnmatch quirk) ...
        assert VaultWatcher._matches_pattern(
            "projects/my-app/subdir/README.md", README_PATTERN
        )
        # ... but derive_project_id rejects it
        assert derive_project_id("projects/my-app/subdir/README.md") is None

    def test_does_not_match_system_readme(self):
        assert not VaultWatcher._matches_pattern(
            "system/README.md", README_PATTERN
        )

    def test_does_not_match_orchestrator_readme(self):
        assert not VaultWatcher._matches_pattern(
            "orchestrator/README.md", README_PATTERN
        )

    def test_does_not_match_lowercase_readme(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/readme.md", README_PATTERN
        )

    def test_does_not_match_non_md_readme(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/README.txt", README_PATTERN
        )

    def test_does_not_match_memory_files(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/memory/knowledge/arch.md", README_PATTERN
        )

    def test_does_not_match_playbook_files(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md", README_PATTERN
        )


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
    async def test_full_handler_via_register(self, tmp_path, caplog):
        """Register via register_readme_handlers and verify end-to-end."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_readme_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create a project README
        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "README.md").write_text("# My App\n")

        with caplog.at_level(logging.INFO, logger="src.readme_handler"):
            await watcher.check()

        handler_logs = [
            r
            for r in caplog.records
            if "README" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 1
        assert "my-app" in handler_logs[0].message
