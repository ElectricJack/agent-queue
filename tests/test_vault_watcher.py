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
"""

from __future__ import annotations

import os
import time

import pytest

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
