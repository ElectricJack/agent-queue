"""Tests for VaultWatcher — unified vault file watcher with path-based dispatch.

Covers:
- File change detection (creation, modification, deletion)
- Handler registration and unregistration
- Glob pattern matching (single-level, recursive **)
- Debouncing (changes within window are batched)
- Deduplication (created+modified → created, created+deleted → removed)
- Poll interval throttling
- Multiple handlers with different patterns
- Start/stop lifecycle
- Path-based dispatch isolation (each handler category receives only its own files)
"""

from __future__ import annotations

import logging
import os
import time

import pytest

from src.facts_handler import FACTS_PATTERNS, register_facts_handlers
from src.memory_handler import MEMORY_PATTERNS, register_memory_handlers
from src.override_handler import OVERRIDE_PATTERN, register_override_handlers
from src.playbook_handler import PLAYBOOK_PATTERNS, register_playbook_handlers
from src.profile_sync import PROFILE_PATTERNS, register_profile_handlers
from src.readme_handler import README_PATTERN, register_readme_handlers
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_file(path: str, content: str = "test") -> None:
    """Create a file with the given content, ensuring parent dirs exist."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _touch(path: str) -> None:
    """Update a file's mtime to trigger modification detection."""
    now = time.time()
    os.utime(path, (now + 1, now + 1))


class ChangeCollector:
    """Test helper that collects VaultChange lists from handler calls."""

    def __init__(self):
        self.calls: list[list[VaultChange]] = []

    async def __call__(self, changes: list[VaultChange]) -> None:
        self.calls.append(changes)

    @property
    def all_changes(self) -> list[VaultChange]:
        """Flatten all calls into a single list."""
        return [c for batch in self.calls for c in batch]

    @property
    def call_count(self) -> int:
        return len(self.calls)


class SyncChangeCollector:
    """Like ChangeCollector but with a sync handler."""

    def __init__(self):
        self.calls: list[list[VaultChange]] = []

    def __call__(self, changes: list[VaultChange]) -> None:
        self.calls.append(changes)

    @property
    def all_changes(self) -> list[VaultChange]:
        return [c for batch in self.calls for c in batch]


# ---------------------------------------------------------------------------
# Pattern matching tests (unit tests for _matches_pattern)
# ---------------------------------------------------------------------------


class TestMatchesPattern:
    """Test the static _matches_pattern method directly."""

    def test_simple_wildcard(self):
        assert VaultWatcher._matches_pattern("system/playbooks/deploy.md", "*/playbooks/*.md")

    def test_simple_wildcard_no_match(self):
        assert not VaultWatcher._matches_pattern("system/memory/facts.md", "*/playbooks/*.md")

    def test_exact_filename_pattern(self):
        assert VaultWatcher._matches_pattern("agent-types/coding/profile.md", "*/profile.md")

    def test_double_star_recursive(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/knowledge/arch.md",
            "projects/*/memory/**/*.md",
        )

    def test_double_star_single_level(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/memory/facts.md",
            "projects/*/memory/**/*.md",
        )

    def test_double_star_at_end(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/notes/deep/nested/file.txt",
            "projects/my-app/notes/**",
        )

    def test_double_star_no_match(self):
        assert not VaultWatcher._matches_pattern(
            "system/playbooks/run.md",
            "projects/*/memory/**/*.md",
        )

    def test_extension_filter(self):
        assert VaultWatcher._matches_pattern("system/playbooks/deploy.md", "**/*.md")
        assert not VaultWatcher._matches_pattern("system/playbooks/data.json", "**/*.md")

    def test_all_md_files(self):
        """The **/*.md pattern should match .md files at any depth."""
        paths = [
            "system/playbooks/deploy.md",
            "orchestrator/memory/facts.md",
            "projects/app/notes/idea.md",
        ]
        for p in paths:
            assert VaultWatcher._matches_pattern(p, "**/*.md"), f"Should match: {p}"

    def test_specific_project(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/ci.md",
            "projects/my-app/playbooks/*.md",
        )
        assert not VaultWatcher._matches_pattern(
            "projects/other/playbooks/ci.md",
            "projects/my-app/playbooks/*.md",
        )

    def test_platform_separator_normalisation(self):
        """Paths with os.sep should still match patterns using /."""
        rel_path = os.path.join("system", "playbooks", "deploy.md")
        assert VaultWatcher._matches_pattern(rel_path, "*/playbooks/*.md")


# ---------------------------------------------------------------------------
# Change detection tests
# ---------------------------------------------------------------------------


class TestChangeDetection:
    """Test that the watcher correctly detects file changes."""

    @pytest.fixture
    def vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "notes").mkdir(parents=True)
        return str(vault)

    def test_detects_new_file(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Create a new file
        _create_file(os.path.join(vault_dir, "system", "playbooks", "new.md"))

        changes = watcher._detect_changes()
        assert len(changes) == 1
        assert changes[0].operation == "created"
        assert changes[0].rel_path == os.path.join("system", "playbooks", "new.md")

    def test_detects_modified_file(self, vault_dir):
        filepath = os.path.join(vault_dir, "system", "playbooks", "existing.md")
        _create_file(filepath, "original")

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Modify the file
        _touch(filepath)

        changes = watcher._detect_changes()
        assert len(changes) == 1
        assert changes[0].operation == "modified"

    def test_detects_deleted_file(self, vault_dir):
        filepath = os.path.join(vault_dir, "system", "playbooks", "to-delete.md")
        _create_file(filepath)

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Delete the file
        os.remove(filepath)

        changes = watcher._detect_changes()
        assert len(changes) == 1
        assert changes[0].operation == "deleted"

    def test_no_changes_detected(self, vault_dir):
        _create_file(os.path.join(vault_dir, "system", "playbooks", "stable.md"))

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        changes = watcher._detect_changes()
        assert changes == []

    def test_multiple_changes(self, vault_dir):
        existing = os.path.join(vault_dir, "system", "playbooks", "existing.md")
        to_delete = os.path.join(vault_dir, "projects", "app", "notes", "old.md")
        _create_file(existing, "content")
        _create_file(to_delete, "will be deleted")

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Create new, modify existing, delete another
        _create_file(os.path.join(vault_dir, "projects", "app", "notes", "new.md"))
        _touch(existing)
        os.remove(to_delete)

        changes = watcher._detect_changes()
        ops = {c.operation for c in changes}
        assert ops == {"created", "modified", "deleted"}
        assert len(changes) == 3

    def test_skips_hidden_files(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Create a hidden file
        _create_file(os.path.join(vault_dir, "system", ".hidden"))

        changes = watcher._detect_changes()
        assert changes == []

    def test_skips_hidden_directories(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True

        # Create file inside .obsidian (hidden dir)
        _create_file(os.path.join(vault_dir, ".obsidian", "workspace.json"))

        changes = watcher._detect_changes()
        assert changes == []


# ---------------------------------------------------------------------------
# Handler dispatch tests
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    """Test that changes are dispatched to the correct handlers."""

    @pytest.fixture
    def vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "projects" / "app" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "memory" / "knowledge").mkdir(parents=True)
        return str(vault)

    @pytest.mark.asyncio
    async def test_handler_receives_matching_changes(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        collector = ChangeCollector()
        watcher.register_handler("*/playbooks/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        assert collector.call_count == 1
        assert len(collector.all_changes) == 1
        assert collector.all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_handler_ignores_non_matching_changes(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        playbook_collector = ChangeCollector()
        watcher.register_handler("*/playbooks/*.md", playbook_collector)

        watcher._snapshot()
        watcher._initialized = True

        # Create a file in memory, not playbooks
        _create_file(os.path.join(vault_dir, "system", "memory", "facts.md"))

        await watcher.check()

        assert playbook_collector.call_count == 0

    @pytest.mark.asyncio
    async def test_multiple_handlers(self, vault_dir):
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        playbook_collector = ChangeCollector()
        memory_collector = ChangeCollector()

        watcher.register_handler("*/playbooks/*.md", playbook_collector)
        watcher.register_handler("**/memory/**/*.md", memory_collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))
        _create_file(os.path.join(vault_dir, "projects", "app", "memory", "knowledge", "arch.md"))

        await watcher.check()

        assert playbook_collector.call_count == 1
        assert len(playbook_collector.all_changes) == 1
        assert memory_collector.call_count == 1
        assert len(memory_collector.all_changes) == 1

    @pytest.mark.asyncio
    async def test_same_change_dispatched_to_multiple_matching_handlers(self, vault_dir):
        """A change can match multiple patterns and be dispatched to each."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        broad_collector = ChangeCollector()
        specific_collector = ChangeCollector()

        watcher.register_handler("**/*.md", broad_collector)
        watcher.register_handler("*/playbooks/*.md", specific_collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        assert broad_collector.call_count == 1
        assert specific_collector.call_count == 1

    @pytest.mark.asyncio
    async def test_sync_handler(self, vault_dir):
        """Sync handlers are supported alongside async handlers."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        collector = SyncChangeCollector()
        watcher.register_handler("*/playbooks/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        assert len(collector.all_changes) == 1

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_block_others(self, vault_dir):
        """A handler that raises an exception should not block other handlers."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)

        async def failing_handler(changes):
            raise RuntimeError("boom")

        good_collector = ChangeCollector()

        watcher.register_handler("**/*.md", failing_handler)
        watcher.register_handler("*/playbooks/*.md", good_collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        # Good handler should still receive its changes
        assert good_collector.call_count == 1


# ---------------------------------------------------------------------------
# Debounce tests
# ---------------------------------------------------------------------------


class TestDebouncing:
    """Test that changes are debounced correctly."""

    @pytest.fixture
    def vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        return str(vault)

    @pytest.mark.asyncio
    async def test_changes_held_during_debounce_window(self, vault_dir):
        """Changes within the debounce window are not dispatched immediately."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=10.0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        # Should NOT be dispatched yet — debounce window is 10s
        assert collector.call_count == 0
        assert watcher.get_pending_change_count() > 0

    @pytest.mark.asyncio
    async def test_changes_dispatched_after_debounce_window(self, vault_dir):
        """Changes are dispatched once the debounce window elapses."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))

        await watcher.check()

        assert collector.call_count == 1

    @pytest.mark.asyncio
    async def test_force_flush_on_stop(self, vault_dir):
        """Pending changes are flushed on stop regardless of debounce window."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))
        await watcher.check()
        assert collector.call_count == 0  # Not flushed yet

        await watcher.stop()

        assert collector.call_count == 1


# ---------------------------------------------------------------------------
# Debounce behaviour tests (roadmap 1.3.10)
# ---------------------------------------------------------------------------


class TestDebounceBehaviour:
    """Detailed debounce behaviour tests per spec Section 17.

    These tests verify the precise debounce semantics: batching rapid edits,
    per-file dispatch, window expiry, final-state preservation, error
    resilience, and configurability.
    """

    @pytest.fixture
    def vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "playbooks").mkdir(parents=True)
        return str(vault)

    # (a) editing same file 10 times in 100ms triggers handler only once
    @pytest.mark.asyncio
    async def test_rapid_edits_same_file_triggers_handler_once(self, vault_dir):
        """Editing the same file 10 times in quick succession triggers handler only once.

        All edits land within the debounce window and are batched into a single
        dispatch with one VaultChange for that file.
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        filepath = os.path.join(vault_dir, "system", "playbooks", "deploy.md")

        # Create the file, then modify it 9 more times (10 total edits)
        _create_file(filepath, "v1")
        await watcher.check()

        for i in range(2, 11):
            with open(filepath, "w") as f:
                f.write(f"v{i}")
            _touch(filepath)
            await watcher.check()

        # All 10 edits are still pending — debounce window (999s) hasn't elapsed
        assert collector.call_count == 0
        assert watcher.get_pending_change_count() > 0

        # Now flush — handler should be called exactly once
        await watcher._flush_pending(force=True)

        assert collector.call_count == 1
        # Deduplication: created + 9 modified → single "created"
        assert len(collector.all_changes) == 1
        assert collector.all_changes[0].operation == "created"

    # (b) editing two different files in same category triggers handler once per file
    @pytest.mark.asyncio
    async def test_two_files_same_category_triggers_handler_once_per_file(self, vault_dir):
        """Editing two different files in the same glob category triggers the handler
        once, with both files in the change list (one dispatch, two VaultChange items).
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("*/playbooks/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        # Create two separate playbook files
        file_a = os.path.join(vault_dir, "system", "playbooks", "deploy.md")
        file_b = os.path.join(vault_dir, "system", "playbooks", "rollback.md")
        _create_file(file_a, "deploy content")
        _create_file(file_b, "rollback content")
        await watcher.check()

        await watcher._flush_pending(force=True)

        # Handler called once with both changes batched together
        assert collector.call_count == 1
        rel_paths = {c.rel_path for c in collector.all_changes}
        assert len(rel_paths) == 2
        assert os.path.join("system", "playbooks", "deploy.md") in rel_paths
        assert os.path.join("system", "playbooks", "rollback.md") in rel_paths

    # (c) editing a file, waiting past debounce window, editing again triggers twice
    @pytest.mark.asyncio
    async def test_edit_wait_edit_triggers_handler_twice(self, vault_dir):
        """Editing a file, waiting past the debounce window, then editing again
        results in two separate handler invocations.
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=1.0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        filepath = os.path.join(vault_dir, "system", "playbooks", "deploy.md")

        # First edit
        _create_file(filepath, "v1")
        await watcher.check()
        assert collector.call_count == 0  # Within debounce window

        # Backdate pending timestamps so debounce window has "elapsed"
        watcher._pending = [(c, t - 2.0) for c, t in watcher._pending]
        await watcher._flush_pending()

        assert collector.call_count == 1
        assert collector.all_changes[0].operation == "created"

        # Second edit (modify the same file)
        with open(filepath, "w") as f:
            f.write("v2")
        _touch(filepath)
        await watcher.check()

        # Backdate again to expire debounce
        watcher._pending = [(c, t - 2.0) for c, t in watcher._pending]
        await watcher._flush_pending()

        assert collector.call_count == 2
        assert collector.calls[1][0].operation == "modified"

    # (d) debounce does not drop the final state — handler receives latest content
    @pytest.mark.asyncio
    async def test_debounce_preserves_final_state(self, vault_dir):
        """After rapid edits within the debounce window, the handler receives the
        final state of the file — deduplication keeps the correct operation and
        the file on disk has the latest content when the handler runs.
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)

        # Capture what the handler sees when it runs
        handler_snapshots: list[dict[str, str]] = []

        async def snapshot_handler(changes: list[VaultChange]) -> None:
            for change in changes:
                if change.operation != "deleted" and os.path.exists(change.path):
                    with open(change.path) as f:
                        handler_snapshots.append({"rel_path": change.rel_path, "content": f.read()})

        watcher.register_handler("**/*.md", snapshot_handler)

        watcher._snapshot()
        watcher._initialized = True

        filepath = os.path.join(vault_dir, "system", "playbooks", "deploy.md")

        # Rapid edits: v1 → v2 → v3 → v4 → v5 (final)
        for version in range(1, 6):
            if version == 1:
                _create_file(filepath, f"version-{version}")
            else:
                with open(filepath, "w") as f:
                    f.write(f"version-{version}")
                _touch(filepath)
            await watcher.check()

        # Flush — handler should see file with "version-5" on disk
        await watcher._flush_pending(force=True)

        assert len(handler_snapshots) == 1
        assert handler_snapshots[0]["content"] == "version-5"

    # (e) handler errors during debounced call do not prevent future triggers
    @pytest.mark.asyncio
    async def test_handler_error_does_not_prevent_future_triggers(self, vault_dir):
        """If a handler raises during a debounced dispatch, subsequent debounce
        windows still dispatch to that handler.
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)

        call_count = 0
        error_on_first = True

        async def flaky_handler(changes: list[VaultChange]) -> None:
            nonlocal call_count, error_on_first
            call_count += 1
            if error_on_first and call_count == 1:
                raise RuntimeError("transient failure")

        watcher.register_handler("**/*.md", flaky_handler)

        watcher._snapshot()
        watcher._initialized = True

        # First edit — handler will raise
        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))
        await watcher.check()
        assert call_count == 1  # Was called (and raised)

        # Second edit — handler should still be called
        _create_file(os.path.join(vault_dir, "system", "playbooks", "rollback.md"))
        await watcher.check()
        assert call_count == 2  # Still invoked despite previous error

    @pytest.mark.asyncio
    async def test_handler_error_does_not_prevent_future_triggers_with_debounce(self, vault_dir):
        """Same as above but with a real debounce window — errors in one flush
        don't poison the handler registration for the next flush.
        """
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=1.0)

        invocations: list[list[VaultChange]] = []

        async def error_then_ok(changes: list[VaultChange]) -> None:
            invocations.append(changes)
            if len(invocations) == 1:
                raise RuntimeError("boom")

        watcher.register_handler("**/*.md", error_then_ok)

        watcher._snapshot()
        watcher._initialized = True

        # First change batch
        _create_file(os.path.join(vault_dir, "system", "playbooks", "a.md"))
        await watcher.check()
        # Expire debounce
        watcher._pending = [(c, t - 2.0) for c, t in watcher._pending]
        await watcher._flush_pending()

        assert len(invocations) == 1  # Called and raised

        # Second change batch — handler must still be active
        _create_file(os.path.join(vault_dir, "system", "playbooks", "b.md"))
        await watcher.check()
        watcher._pending = [(c, t - 2.0) for c, t in watcher._pending]
        await watcher._flush_pending()

        assert len(invocations) == 2  # Called again, succeeded

    # (f) debounce window is configurable and defaults to a reasonable value
    def test_debounce_default_value(self, vault_dir):
        """Default debounce_seconds is 2.0 — a reasonable value for editor saves."""
        watcher = VaultWatcher(vault_dir)
        assert watcher.debounce_seconds == 2.0

    def test_debounce_configurable(self, vault_dir):
        """debounce_seconds can be set to a custom value."""
        watcher = VaultWatcher(vault_dir, debounce_seconds=5.0)
        assert watcher.debounce_seconds == 5.0

    @pytest.mark.asyncio
    async def test_debounce_zero_dispatches_immediately(self, vault_dir):
        """Setting debounce_seconds=0 dispatches changes on the same check() cycle."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))
        await watcher.check()

        # With debounce=0, dispatched immediately
        assert collector.call_count == 1

    @pytest.mark.asyncio
    async def test_short_debounce_holds_then_releases(self, vault_dir):
        """A short debounce window holds changes until it elapses."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0.5)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "deploy.md"))
        await watcher.check()

        # Still within 0.5s debounce — not dispatched
        assert collector.call_count == 0

        # Backdate to simulate 0.5s passing
        watcher._pending = [(c, t - 1.0) for c, t in watcher._pending]
        await watcher._flush_pending()

        assert collector.call_count == 1


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Test that changes are deduplicated correctly across debounce windows."""

    @pytest.fixture
    def vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        return str(vault)

    @pytest.mark.asyncio
    async def test_created_then_modified_becomes_created(self, vault_dir):
        """If a file is created then modified within the debounce window, report created."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        filepath = os.path.join(vault_dir, "system", "playbooks", "new.md")
        _create_file(filepath, "v1")
        await watcher.check()

        _touch(filepath)
        await watcher.check()

        # Force flush
        await watcher._flush_pending(force=True)

        assert collector.call_count == 1
        assert len(collector.all_changes) == 1
        assert collector.all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_created_then_deleted_cancels_out(self, vault_dir):
        """If a file is created then deleted within the debounce window, no event."""
        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        filepath = os.path.join(vault_dir, "system", "playbooks", "ephemeral.md")
        _create_file(filepath)
        await watcher.check()

        os.remove(filepath)
        await watcher.check()

        await watcher._flush_pending(force=True)

        assert collector.call_count == 0

    @pytest.mark.asyncio
    async def test_modified_then_deleted_becomes_deleted(self, vault_dir):
        """If a file is modified then deleted, report deleted."""
        filepath = os.path.join(vault_dir, "system", "playbooks", "existing.md")
        _create_file(filepath, "content")

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _touch(filepath)
        await watcher.check()

        os.remove(filepath)
        await watcher.check()

        await watcher._flush_pending(force=True)

        assert collector.call_count == 1
        assert len(collector.all_changes) == 1
        assert collector.all_changes[0].operation == "deleted"


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_returns_id(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path))
        hid = watcher.register_handler("**/*.md", lambda c: None)
        assert hid
        assert watcher.get_handler_count() == 1

    def test_register_custom_id(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path))
        hid = watcher.register_handler("**/*.md", lambda c: None, handler_id="my-handler")
        assert hid == "my-handler"

    def test_unregister(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path))
        hid = watcher.register_handler("**/*.md", lambda c: None)
        assert watcher.get_handler_count() == 1
        assert watcher.unregister_handler(hid) is True
        assert watcher.get_handler_count() == 0

    def test_unregister_nonexistent(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path))
        assert watcher.unregister_handler("no-such-handler") is False


# ---------------------------------------------------------------------------
# Poll interval tests
# ---------------------------------------------------------------------------


class TestPollInterval:
    @pytest.mark.asyncio
    async def test_poll_interval_throttles(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        vault_dir = str(vault)

        watcher = VaultWatcher(vault_dir, poll_interval=999, debounce_seconds=0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True
        watcher._last_poll = time.time()  # Just polled

        _create_file(os.path.join(vault_dir, "system", "playbooks", "new.md"))

        # Should be throttled — poll interval hasn't elapsed
        changes = await watcher.check()
        assert changes == []
        assert collector.call_count == 0

    @pytest.mark.asyncio
    async def test_poll_interval_zero_always_checks(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        vault_dir = str(vault)

        watcher = VaultWatcher(vault_dir, poll_interval=0, debounce_seconds=0)
        collector = ChangeCollector()
        watcher.register_handler("**/*.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        _create_file(os.path.join(vault_dir, "system", "playbooks", "new.md"))
        changes = await watcher.check()
        assert len(changes) == 1


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_takes_initial_snapshot(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)
        _create_file(str(vault / "system" / "playbooks" / "existing.md"))

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        await watcher.start()
        try:
            assert watcher._initialized
            assert watcher.get_tracked_file_count() == 1
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=1)
        await watcher.start()
        assert watcher._task is not None
        assert not watcher._task.done()

        await watcher.stop()
        assert watcher._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=1)
        await watcher.start()
        await watcher.start()  # Should not raise
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_nonexistent_vault_root(self, tmp_path):
        """Watcher handles non-existent vault directory gracefully."""
        watcher = VaultWatcher(str(tmp_path / "no-such-dir"), poll_interval=0, debounce_seconds=0)
        watcher._snapshot()
        watcher._initialized = True
        changes = watcher._detect_changes()
        assert changes == []


# ---------------------------------------------------------------------------
# Introspection tests
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_handler_count(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path))
        assert watcher.get_handler_count() == 0
        watcher.register_handler("**/*.md", lambda c: None)
        assert watcher.get_handler_count() == 1
        watcher.register_handler("*/playbooks/*.md", lambda c: None)
        assert watcher.get_handler_count() == 2

    def test_tracked_file_count(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system").mkdir(parents=True)
        _create_file(str(vault / "system" / "a.md"))
        _create_file(str(vault / "system" / "b.md"))

        watcher = VaultWatcher(str(vault))
        watcher._snapshot()
        assert watcher.get_tracked_file_count() == 2

    @pytest.mark.asyncio
    async def test_pending_change_count(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "system").mkdir(parents=True)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=999)
        watcher._snapshot()
        watcher._initialized = True

        assert watcher.get_pending_change_count() == 0

        _create_file(str(vault / "system" / "new.md"))
        await watcher.check()

        assert watcher.get_pending_change_count() > 0


# ---------------------------------------------------------------------------
# VaultChange dataclass tests
# ---------------------------------------------------------------------------


class TestVaultChange:
    def test_immutable(self):
        change = VaultChange(path="/a/b/c.md", rel_path="b/c.md", operation="created")
        with pytest.raises(AttributeError):
            change.operation = "modified"  # type: ignore[misc]

    def test_equality(self):
        a = VaultChange(path="/a/b.md", rel_path="b.md", operation="created")
        b = VaultChange(path="/a/b.md", rel_path="b.md", operation="created")
        assert a == b

    def test_hashable(self):
        change = VaultChange(path="/a/b.md", rel_path="b.md", operation="created")
        s = {change}
        assert change in s


# ---------------------------------------------------------------------------
# Path-based dispatch isolation tests (roadmap 1.3.9)
# ---------------------------------------------------------------------------
#
# These tests register ALL handler categories simultaneously and verify that
# each file change is dispatched to the correct handler(s) and ONLY those
# handlers.  This is the cross-category dispatch guarantee described in
# the playbooks spec Section 17.


@pytest.fixture
def dispatch_env(tmp_path):
    """Set up a vault with ALL handler categories registered.

    Returns (vault_path, watcher, collectors) where *collectors* is a dict
    mapping category names to ChangeCollector instances.  Each category's
    patterns are registered with its own collector so we can assert which
    categories received dispatch.
    """
    vault = tmp_path / "vault"
    vault.mkdir()

    watcher = VaultWatcher(
        vault_root=str(vault),
        poll_interval=0,
        debounce_seconds=0,
    )

    collectors: dict[str, ChangeCollector] = {
        "playbook": ChangeCollector(),
        "profile": ChangeCollector(),
        "memory": ChangeCollector(),
        "facts": ChangeCollector(),
        "override": ChangeCollector(),
        "readme": ChangeCollector(),
    }

    # Register all patterns from every handler category
    for pattern in PLAYBOOK_PATTERNS:
        watcher.register_handler(pattern, collectors["playbook"], handler_id=f"t-pb:{pattern}")
    for pattern in PROFILE_PATTERNS:
        watcher.register_handler(pattern, collectors["profile"], handler_id=f"t-pf:{pattern}")
    for pattern in MEMORY_PATTERNS:
        watcher.register_handler(pattern, collectors["memory"], handler_id=f"t-mem:{pattern}")
    for pattern in FACTS_PATTERNS:
        watcher.register_handler(pattern, collectors["facts"], handler_id=f"t-facts:{pattern}")
    watcher.register_handler(OVERRIDE_PATTERN, collectors["override"], handler_id="t-override")
    watcher.register_handler(README_PATTERN, collectors["readme"], handler_id="t-readme")

    return vault, watcher, collectors


def _assert_only_collector_fired(
    collectors: dict[str, ChangeCollector],
    expected_category: str,
    *,
    min_changes: int = 1,
) -> None:
    """Assert that exactly *expected_category* received changes and all others are empty."""
    for category, collector in collectors.items():
        if category == expected_category:
            assert len(collector.all_changes) >= min_changes, (
                f"Expected {expected_category} collector to have >= {min_changes} "
                f"change(s), got {len(collector.all_changes)}"
            )
        else:
            assert len(collector.all_changes) == 0, (
                f"Expected {category} collector to be empty, but it received "
                f"{len(collector.all_changes)} change(s): "
                f"{[c.rel_path for c in collector.all_changes]}"
            )


def _assert_no_collector_fired(collectors: dict[str, ChangeCollector]) -> None:
    """Assert that no collector received any changes."""
    for category, collector in collectors.items():
        assert len(collector.all_changes) == 0, (
            f"Expected {category} collector to be empty, but it received "
            f"{len(collector.all_changes)} change(s): "
            f"{[c.rel_path for c in collector.all_changes]}"
        )


class TestPlaybookDispatchIsolation:
    """(a) Creating/editing */playbooks/*.md triggers playbook handler only."""

    @pytest.mark.asyncio
    async def test_system_playbook_triggers_playbook_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()  # initial snapshot

        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Deploy\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")
        assert collectors["playbook"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_orchestrator_playbook_triggers_playbook_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "orchestrator" / "playbooks").mkdir(parents=True)
        (vault / "orchestrator" / "playbooks" / "routing.md").write_text("# Routing\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")

    @pytest.mark.asyncio
    async def test_project_playbook_triggers_playbook_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "my-app" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "my-app" / "playbooks" / "review.md").write_text("# Review\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")

    @pytest.mark.asyncio
    async def test_agent_type_playbook_triggers_playbook_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "agent-types" / "coding" / "playbooks").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "playbooks" / "quality.md").write_text("# QA\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")

    @pytest.mark.asyncio
    async def test_modified_playbook_triggers_playbook_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text("# Deploy v1\n")
        await watcher.check()  # snapshot includes file

        time.sleep(0.05)
        playbook_file.write_text("# Deploy v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")
        assert collectors["playbook"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_multiple_playbooks_across_scopes(self, dispatch_env):
        """Creating playbooks in all 4 scopes triggers only the playbook handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        for scope_path in [
            "system/playbooks",
            "orchestrator/playbooks",
            "projects/app/playbooks",
            "agent-types/coder/playbooks",
        ]:
            d = vault / scope_path
            d.mkdir(parents=True, exist_ok=True)
            (d / "test.md").write_text("# Test\n")

        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook", min_changes=4)


class TestProfileDispatchIsolation:
    """(b) Creating/editing */profile.md triggers profile sync handler only."""

    @pytest.mark.asyncio
    async def test_agent_type_profile_triggers_profile_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "agent-types" / "coding").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "profile.md").write_text("# Coding Profile\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "profile")
        assert collectors["profile"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_orchestrator_profile_triggers_profile_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "orchestrator").mkdir(parents=True)
        (vault / "orchestrator" / "profile.md").write_text("# Orchestrator Profile\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "profile")

    @pytest.mark.asyncio
    async def test_modified_profile_triggers_profile_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        profile_dir = vault / "agent-types" / "review"
        profile_dir.mkdir(parents=True)
        profile_file = profile_dir / "profile.md"
        profile_file.write_text("# Review v1\n")
        await watcher.check()

        time.sleep(0.05)
        profile_file.write_text("# Review v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "profile")
        assert collectors["profile"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_profile_in_both_scopes(self, dispatch_env):
        """Profiles in agent-types and orchestrator both go to profile handler only."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "agent-types" / "coding").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "profile.md").write_text("# Coding\n")
        (vault / "orchestrator").mkdir(parents=True)
        (vault / "orchestrator" / "profile.md").write_text("# Orchestrator\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "profile", min_changes=2)


class TestMemoryDispatchIsolation:
    """(c) Creating/editing */memory/**/*.md triggers memory re-index handler only."""

    @pytest.mark.asyncio
    async def test_system_memory_triggers_memory_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "conventions.md").write_text("# Conventions\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_orchestrator_memory_triggers_memory_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "orchestrator" / "memory").mkdir(parents=True)
        (vault / "orchestrator" / "memory" / "notes.md").write_text("# Notes\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_agent_type_memory_triggers_memory_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "agent-types" / "coding" / "memory").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "memory" / "patterns.md").write_text("# Patterns\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_project_memory_flat_triggers_memory_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "app" / "memory").mkdir(parents=True)
        (vault / "projects" / "app" / "memory" / "arch.md").write_text("# Arch\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_project_memory_nested_triggers_memory_only(self, dispatch_env):
        """Project memory uses ** pattern and supports nested subdirectories."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "app" / "memory" / "knowledge").mkdir(parents=True)
        (vault / "projects" / "app" / "memory" / "knowledge" / "api.md").write_text("# API\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_project_memory_deeply_nested_triggers_memory_only(self, dispatch_env):
        """Deeply nested project memory files still dispatch correctly."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        deep = vault / "projects" / "app" / "memory" / "knowledge" / "subsystem" / "details"
        deep.mkdir(parents=True)
        (deep / "internals.md").write_text("# Internals\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")

    @pytest.mark.asyncio
    async def test_modified_memory_triggers_memory_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        mem_dir = vault / "system" / "memory"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "conventions.md"
        mem_file.write_text("# v1\n")
        await watcher.check()

        time.sleep(0.05)
        mem_file.write_text("# v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")
        assert collectors["memory"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_memory_across_all_scopes(self, dispatch_env):
        """Memory files in all 4 scopes go to memory handler only."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "global.md").write_text("# Global\n")
        (vault / "orchestrator" / "memory").mkdir(parents=True)
        (vault / "orchestrator" / "memory" / "ops.md").write_text("# Ops\n")
        (vault / "agent-types" / "coder" / "memory").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "memory" / "tips.md").write_text("# Tips\n")
        (vault / "projects" / "app" / "memory" / "knowledge").mkdir(parents=True)
        (vault / "projects" / "app" / "memory" / "knowledge" / "arch.md").write_text("# Arch\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory", min_changes=4)


class TestReadmeDispatchIsolation:
    """(d) Creating/editing projects/*/README.md triggers readme handler only."""

    @pytest.mark.asyncio
    async def test_project_readme_triggers_readme_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "README.md").write_text("# My App\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "readme")
        assert collectors["readme"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_modified_readme_triggers_readme_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        readme_dir = vault / "projects" / "my-app"
        readme_dir.mkdir(parents=True)
        readme_file = readme_dir / "README.md"
        readme_file.write_text("# My App v1\n")
        await watcher.check()

        time.sleep(0.05)
        readme_file.write_text("# My App v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "readme")
        assert collectors["readme"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_multiple_project_readmes(self, dispatch_env):
        """READMEs in different projects go to readme handler only."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        for proj in ["app-one", "app-two", "app-three"]:
            (vault / "projects" / proj).mkdir(parents=True)
            (vault / "projects" / proj / "README.md").write_text(f"# {proj}\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "readme", min_changes=3)


class TestOverrideDispatchIsolation:
    """(e) Creating/editing projects/*/overrides/*.md triggers override handler only."""

    @pytest.mark.asyncio
    async def test_override_triggers_override_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "my-app" / "overrides").mkdir(parents=True)
        (vault / "projects" / "my-app" / "overrides" / "coding.md").write_text("# Override\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "override")
        assert collectors["override"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_modified_override_triggers_override_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        override_dir = vault / "projects" / "app" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "review.md"
        override_file.write_text("# v1\n")
        await watcher.check()

        time.sleep(0.05)
        override_file.write_text("# v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "override")
        assert collectors["override"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_multiple_overrides_different_projects(self, dispatch_env):
        """Override files in different projects go to override handler only."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        for proj in ["app-a", "app-b"]:
            (vault / "projects" / proj / "overrides").mkdir(parents=True)
            (vault / "projects" / proj / "overrides" / "coding.md").write_text("# Override\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "override", min_changes=2)


class TestFactsDispatchIsolation:
    """(f) Creating/editing */facts.md triggers KV sync handler only."""

    @pytest.mark.asyncio
    async def test_system_facts_triggers_facts_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system").mkdir(parents=True)
        (vault / "system" / "facts.md").write_text("# Facts\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")

    @pytest.mark.asyncio
    async def test_orchestrator_facts_triggers_facts_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "orchestrator").mkdir(parents=True)
        (vault / "orchestrator" / "facts.md").write_text("# Facts\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")

    @pytest.mark.asyncio
    async def test_project_facts_triggers_facts_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "my-app").mkdir(parents=True)
        (vault / "projects" / "my-app" / "facts.md").write_text("# Facts\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")

    @pytest.mark.asyncio
    async def test_agent_type_facts_triggers_facts_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "agent-types" / "coding").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "facts.md").write_text("# Facts\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")

    @pytest.mark.asyncio
    async def test_modified_facts_triggers_facts_only(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        facts_dir = vault / "projects" / "app"
        facts_dir.mkdir(parents=True)
        facts_file = facts_dir / "facts.md"
        facts_file.write_text("# v1\n")
        await watcher.check()

        time.sleep(0.05)
        facts_file.write_text("# v2\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")
        assert collectors["facts"].all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_facts_across_all_scopes(self, dispatch_env):
        """Facts files in all 4 scopes go to facts handler only."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system").mkdir(parents=True)
        (vault / "system" / "facts.md").write_text("# System\n")
        (vault / "orchestrator").mkdir(parents=True)
        (vault / "orchestrator" / "facts.md").write_text("# Orchestrator\n")
        (vault / "agent-types" / "coder").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "facts.md").write_text("# Agent\n")
        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "facts.md").write_text("# Project\n")
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts", min_changes=4)


class TestUnwatchedPathDispatch:
    """(g) Editing a file outside any watched path triggers NO handler."""

    @pytest.mark.asyncio
    async def test_random_md_in_project_root(self, dispatch_env):
        """A .md file directly under projects/<name>/ matches no handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "notes.md").write_text("# Notes\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_non_md_file_in_playbooks(self, dispatch_env):
        """A non-.md file in playbooks/ matches no handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "data.yaml").write_text("key: value\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_config_file_at_root(self, dispatch_env):
        """A config file at the vault root matches no handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "config.yaml").write_text("setting: true\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_random_file_in_system(self, dispatch_env):
        """A random file under system/ (not facts/memory/playbooks) matches nothing."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system").mkdir(parents=True)
        (vault / "system" / "config.md").write_text("# Config\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_readme_in_non_project_scope(self, dispatch_env):
        """README.md under system/ should not trigger the readme handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system").mkdir(parents=True)
        (vault / "system" / "README.md").write_text("# System README\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_override_in_non_project_scope(self, dispatch_env):
        """overrides/ under system/ should not trigger the override handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system" / "overrides").mkdir(parents=True)
        (vault / "system" / "overrides" / "coding.md").write_text("# Override\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_deeply_nested_unknown_file(self, dispatch_env):
        """A file deep in an unrelated path matches no handler."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "custom" / "data" / "reports").mkdir(parents=True)
        (vault / "custom" / "data" / "reports" / "q4.md").write_text("# Q4 Report\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_profile_md_outside_registered_scopes(self, dispatch_env):
        """profile.md under projects/ is not a registered profile pattern."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        # Profile patterns only cover agent-types/*/profile.md and orchestrator/profile.md
        # Not projects/*/profile.md or system/profile.md
        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "profile.md").write_text("# Profile\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)

    @pytest.mark.asyncio
    async def test_txt_file_in_memory_directory(self, dispatch_env):
        """A .txt file in a memory directory matches no handler (only .md does)."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "notes.txt").write_text("plain text\n")
        await watcher.check()

        _assert_no_collector_fired(collectors)


class TestDeletionDispatch:
    """(h) Deleting a watched file triggers the appropriate handler."""

    @pytest.mark.asyncio
    async def test_deleted_playbook_dispatches_to_playbook(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        pb_dir = vault / "system" / "playbooks"
        pb_dir.mkdir(parents=True)
        pb_file = pb_dir / "deploy.md"
        pb_file.write_text("# Deploy\n")
        await watcher.check()  # snapshot includes the file

        pb_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "playbook")
        assert collectors["playbook"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_deleted_profile_dispatches_to_profile(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        prof_dir = vault / "agent-types" / "coding"
        prof_dir.mkdir(parents=True)
        prof_file = prof_dir / "profile.md"
        prof_file.write_text("# Profile\n")
        await watcher.check()

        prof_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "profile")
        assert collectors["profile"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_deleted_memory_dispatches_to_memory(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        mem_dir = vault / "projects" / "app" / "memory" / "knowledge"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "arch.md"
        mem_file.write_text("# Architecture\n")
        await watcher.check()

        mem_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "memory")
        assert collectors["memory"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_deleted_facts_dispatches_to_facts(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        (vault / "system").mkdir(parents=True)
        facts_file = vault / "system" / "facts.md"
        facts_file.write_text("# Facts\n")
        await watcher.check()

        facts_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "facts")
        assert collectors["facts"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_deleted_override_dispatches_to_override(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        ovr_dir = vault / "projects" / "app" / "overrides"
        ovr_dir.mkdir(parents=True)
        ovr_file = ovr_dir / "coding.md"
        ovr_file.write_text("# Override\n")
        await watcher.check()

        ovr_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "override")
        assert collectors["override"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_deleted_readme_dispatches_to_readme(self, dispatch_env):
        vault, watcher, collectors = dispatch_env
        proj_dir = vault / "projects" / "app"
        proj_dir.mkdir(parents=True)
        readme_file = proj_dir / "README.md"
        readme_file.write_text("# App\n")
        await watcher.check()

        readme_file.unlink()
        await watcher.check()

        _assert_only_collector_fired(collectors, "readme")
        assert collectors["readme"].all_changes[0].operation == "deleted"


class TestCrossCategoryRename:
    """(i) Renaming across categories triggers both old-path and new-path handlers.

    VaultWatcher doesn't track renames natively — it sees a deletion at the old
    path and a creation at the new path.  Both operations should dispatch to
    their respective handlers.
    """

    @pytest.mark.asyncio
    async def test_rename_playbook_to_memory(self, dispatch_env):
        """Moving a file from playbooks/ to memory/ triggers both handlers."""
        vault, watcher, collectors = dispatch_env

        # Start with a playbook file
        pb_dir = vault / "system" / "playbooks"
        pb_dir.mkdir(parents=True)
        pb_file = pb_dir / "deploy.md"
        pb_file.write_text("# Deploy\n")

        mem_dir = vault / "system" / "memory"
        mem_dir.mkdir(parents=True)

        await watcher.check()  # snapshot

        # "Rename" = delete old + create new
        pb_file.unlink()
        (mem_dir / "deploy.md").write_text("# Deploy\n")
        await watcher.check()

        # Playbook handler should see a deletion
        assert len(collectors["playbook"].all_changes) == 1
        assert collectors["playbook"].all_changes[0].operation == "deleted"
        assert "playbooks" in collectors["playbook"].all_changes[0].rel_path

        # Memory handler should see a creation
        assert len(collectors["memory"].all_changes) == 1
        assert collectors["memory"].all_changes[0].operation == "created"
        assert "memory" in collectors["memory"].all_changes[0].rel_path

        # No other handlers should fire
        assert len(collectors["profile"].all_changes) == 0
        assert len(collectors["facts"].all_changes) == 0
        assert len(collectors["override"].all_changes) == 0
        assert len(collectors["readme"].all_changes) == 0

    @pytest.mark.asyncio
    async def test_rename_facts_to_override(self, dispatch_env):
        """Moving a file from facts to overrides triggers both handlers."""
        vault, watcher, collectors = dispatch_env

        (vault / "projects" / "app").mkdir(parents=True)
        facts_file = vault / "projects" / "app" / "facts.md"
        facts_file.write_text("# Facts\n")

        (vault / "projects" / "app" / "overrides").mkdir(parents=True)

        await watcher.check()

        facts_file.unlink()
        (vault / "projects" / "app" / "overrides" / "coding.md").write_text("# Override\n")
        await watcher.check()

        assert len(collectors["facts"].all_changes) == 1
        assert collectors["facts"].all_changes[0].operation == "deleted"

        assert len(collectors["override"].all_changes) == 1
        assert collectors["override"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_rename_readme_to_memory(self, dispatch_env):
        """Moving README.md to memory/ triggers both handlers."""
        vault, watcher, collectors = dispatch_env

        (vault / "projects" / "app").mkdir(parents=True)
        readme = vault / "projects" / "app" / "README.md"
        readme.write_text("# App\n")

        (vault / "projects" / "app" / "memory").mkdir(parents=True)

        await watcher.check()

        readme.unlink()
        (vault / "projects" / "app" / "memory" / "readme-backup.md").write_text("# App\n")
        await watcher.check()

        assert len(collectors["readme"].all_changes) == 1
        assert collectors["readme"].all_changes[0].operation == "deleted"

        assert len(collectors["memory"].all_changes) == 1
        assert collectors["memory"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_rename_memory_to_playbook(self, dispatch_env):
        """Moving a memory file to playbooks/ triggers both handlers."""
        vault, watcher, collectors = dispatch_env

        (vault / "orchestrator" / "memory").mkdir(parents=True)
        mem_file = vault / "orchestrator" / "memory" / "notes.md"
        mem_file.write_text("# Notes\n")

        (vault / "orchestrator" / "playbooks").mkdir(parents=True)

        await watcher.check()

        mem_file.unlink()
        (vault / "orchestrator" / "playbooks" / "notes.md").write_text("# Notes\n")
        await watcher.check()

        assert len(collectors["memory"].all_changes) == 1
        assert collectors["memory"].all_changes[0].operation == "deleted"

        assert len(collectors["playbook"].all_changes) == 1
        assert collectors["playbook"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_rename_override_to_playbook_across_projects(self, dispatch_env):
        """Moving an override from one project to a playbook in another."""
        vault, watcher, collectors = dispatch_env

        (vault / "projects" / "app-a" / "overrides").mkdir(parents=True)
        ovr_file = vault / "projects" / "app-a" / "overrides" / "coding.md"
        ovr_file.write_text("# Override\n")

        (vault / "projects" / "app-b" / "playbooks").mkdir(parents=True)

        await watcher.check()

        ovr_file.unlink()
        (vault / "projects" / "app-b" / "playbooks" / "coding.md").write_text("# Playbook\n")
        await watcher.check()

        assert len(collectors["override"].all_changes) == 1
        assert collectors["override"].all_changes[0].operation == "deleted"

        assert len(collectors["playbook"].all_changes) == 1
        assert collectors["playbook"].all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_rename_from_watched_to_unwatched(self, dispatch_env):
        """Moving from a watched path to an unwatched one triggers only old handler."""
        vault, watcher, collectors = dispatch_env

        (vault / "system" / "playbooks").mkdir(parents=True)
        pb_file = vault / "system" / "playbooks" / "deploy.md"
        pb_file.write_text("# Deploy\n")

        (vault / "system" / "archive").mkdir(parents=True)

        await watcher.check()

        pb_file.unlink()
        (vault / "system" / "archive" / "deploy.md").write_text("# Deploy\n")
        await watcher.check()

        # Only playbook handler fires (deletion), no handler for the archive path
        _assert_only_collector_fired(collectors, "playbook")
        assert collectors["playbook"].all_changes[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_rename_from_unwatched_to_watched(self, dispatch_env):
        """Moving from an unwatched path to a watched one triggers only new handler."""
        vault, watcher, collectors = dispatch_env

        (vault / "system" / "drafts").mkdir(parents=True)
        draft = vault / "system" / "drafts" / "deploy.md"
        draft.write_text("# Deploy Draft\n")

        (vault / "system" / "playbooks").mkdir(parents=True)

        await watcher.check()

        draft.unlink()
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Deploy\n")
        await watcher.check()

        # Only playbook handler fires (creation), no handler for the drafts path
        _assert_only_collector_fired(collectors, "playbook")
        assert collectors["playbook"].all_changes[0].operation == "created"


class TestMultiCategorySimultaneous:
    """Test that changes to multiple categories in a single check() cycle
    are correctly dispatched to their respective handlers."""

    @pytest.mark.asyncio
    async def test_all_categories_in_single_cycle(self, dispatch_env):
        """Create one file per category in a single check cycle."""
        vault, watcher, collectors = dispatch_env
        await watcher.check()

        # Create exactly one file per handler category
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Playbook\n")

        (vault / "agent-types" / "coding").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "profile.md").write_text("# Profile\n")

        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "conventions.md").write_text("# Memory\n")

        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "README.md").write_text("# README\n")

        (vault / "projects" / "app" / "overrides").mkdir(parents=True)
        (vault / "projects" / "app" / "overrides" / "coding.md").write_text("# Override\n")

        (vault / "system" / "facts.md").write_text("# Facts\n")

        await watcher.check()

        # Each collector should have exactly 1 change
        for category, collector in collectors.items():
            assert len(collector.all_changes) == 1, (
                f"Expected {category} to have 1 change, "
                f"got {len(collector.all_changes)}: "
                f"{[c.rel_path for c in collector.all_changes]}"
            )
            assert collector.all_changes[0].operation == "created"

    @pytest.mark.asyncio
    async def test_mixed_operations_in_single_cycle(self, dispatch_env):
        """Create, modify, and delete files across different categories in one cycle."""
        vault, watcher, collectors = dispatch_env

        # Pre-create files for modification and deletion
        (vault / "system" / "playbooks").mkdir(parents=True)
        pb_file = vault / "system" / "playbooks" / "existing.md"
        pb_file.write_text("# Existing Playbook\n")

        (vault / "orchestrator" / "memory").mkdir(parents=True)
        mem_file = vault / "orchestrator" / "memory" / "old.md"
        mem_file.write_text("# Old Memory\n")

        await watcher.check()  # snapshot

        # Modify the playbook
        time.sleep(0.05)
        pb_file.write_text("# Updated Playbook\n")

        # Delete the memory file
        mem_file.unlink()

        # Create a new facts file
        (vault / "orchestrator" / "facts.md").write_text("# Facts\n")

        await watcher.check()

        assert len(collectors["playbook"].all_changes) == 1
        assert collectors["playbook"].all_changes[0].operation == "modified"

        assert len(collectors["memory"].all_changes) == 1
        assert collectors["memory"].all_changes[0].operation == "deleted"

        assert len(collectors["facts"].all_changes) == 1
        assert collectors["facts"].all_changes[0].operation == "created"

        # Profile, override, readme should be untouched
        assert len(collectors["profile"].all_changes) == 0
        assert len(collectors["override"].all_changes) == 0
        assert len(collectors["readme"].all_changes) == 0


class TestDispatchWithRealHandlers:
    """Integration tests using the actual register_*_handlers functions."""

    @pytest.mark.asyncio
    async def test_all_real_handlers_registered_and_dispatching(self, tmp_path, caplog):
        """Register all real handlers and verify they log appropriately."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)

        register_playbook_handlers(watcher)
        register_profile_handlers(watcher)
        register_memory_handlers(watcher)
        register_facts_handlers(watcher)
        register_override_handlers(watcher)
        register_readme_handlers(watcher)

        await watcher.check()  # initial snapshot

        # Create one file per category
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Playbook\n")

        (vault / "agent-types" / "coding").mkdir(parents=True)
        (vault / "agent-types" / "coding" / "profile.md").write_text("# Profile\n")

        (vault / "system" / "memory").mkdir(parents=True)
        (vault / "system" / "memory" / "conventions.md").write_text("# Memory\n")

        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "README.md").write_text("# App\n")

        (vault / "projects" / "app" / "overrides").mkdir(parents=True)
        (vault / "projects" / "app" / "overrides" / "coding.md").write_text("# Override\n")

        (vault / "system" / "facts.md").write_text("# Facts\n")

        with caplog.at_level(logging.INFO):
            await watcher.check()

        # Verify each handler type logged
        messages = [r.message for r in caplog.records]

        # Playbook handler should log
        assert any("Playbook" in m and "created" in m for m in messages), (
            f"Expected playbook log, got: {messages}"
        )
        # Profile handler should log
        assert any("Profile" in m and "created" in m for m in messages), (
            f"Expected profile log, got: {messages}"
        )
        # Memory handler should log
        assert any("memory.md" in m and "created" in m for m in messages), (
            f"Expected memory log, got: {messages}"
        )
        # README handler should log
        assert any("README" in m and "created" in m for m in messages), (
            f"Expected readme log, got: {messages}"
        )
        # Override handler should log
        assert any("override.md" in m and "created" in m for m in messages), (
            f"Expected override log, got: {messages}"
        )
        # Facts handler should log
        assert any("facts.md" in m and "created" in m for m in messages), (
            f"Expected facts log, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_total_handler_count_with_all_registered(self, tmp_path):
        """Verify the expected total handler count when all categories are registered."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)

        register_playbook_handlers(watcher)
        register_profile_handlers(watcher)
        register_memory_handlers(watcher)
        register_facts_handlers(watcher)
        register_override_handlers(watcher)
        register_readme_handlers(watcher)

        expected = (
            len(PLAYBOOK_PATTERNS)
            + len(PROFILE_PATTERNS)
            + len(MEMORY_PATTERNS)
            + len(FACTS_PATTERNS)
            + 1  # override (single pattern)
            + 1  # readme (single pattern)
        )
        assert watcher.get_handler_count() == expected
