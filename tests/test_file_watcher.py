"""Tests for the FileWatcher filesystem monitoring system."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from src.event_bus import EventBus
from src.file_watcher import FileWatcher, WatchRule, _FileState


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def watcher(bus):
    return FileWatcher(bus, debounce_seconds=0.1, poll_interval=0.0)


# --- File watch tests ---


class TestFileWatch:
    @pytest.mark.asyncio
    async def test_detects_file_modification(self, watcher, bus, tmp_path):
        """File modification should emit a file.changed event."""
        test_file = tmp_path / "config.yaml"
        test_file.write_text("key: value1")

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w1",
            project_id="proj-1",
            paths=[str(test_file)],
            watch_type="file",
        )
        watcher.add_watch(rule)

        # Modify the file
        time.sleep(0.05)  # Ensure mtime changes
        test_file.write_text("key: value2")

        await watcher.check()
        assert len(events) == 1
        assert events[0]["operation"] == "modified"
        assert events[0]["project_id"] == "proj-1"
        assert events[0]["path"] == str(test_file)

    @pytest.mark.asyncio
    async def test_detects_file_creation(self, watcher, bus, tmp_path):
        """A file that didn't exist and then appears should emit 'created'."""
        test_file = tmp_path / "newfile.txt"

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w2",
            project_id="proj-1",
            paths=[str(test_file)],
            watch_type="file",
        )
        watcher.add_watch(rule)

        # Create the file
        test_file.write_text("hello")

        await watcher.check()
        assert len(events) == 1
        assert events[0]["operation"] == "created"

    @pytest.mark.asyncio
    async def test_detects_file_deletion(self, watcher, bus, tmp_path):
        """A file that exists then is removed should emit 'deleted'."""
        test_file = tmp_path / "to_delete.txt"
        test_file.write_text("goodbye")

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w3",
            project_id="proj-1",
            paths=[str(test_file)],
            watch_type="file",
        )
        watcher.add_watch(rule)

        # Delete the file
        os.remove(str(test_file))

        await watcher.check()
        assert len(events) == 1
        assert events[0]["operation"] == "deleted"

    @pytest.mark.asyncio
    async def test_no_event_when_unchanged(self, watcher, bus, tmp_path):
        """No event should be emitted if the file hasn't changed."""
        test_file = tmp_path / "stable.txt"
        test_file.write_text("unchanged")

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w4",
            project_id="proj-1",
            paths=[str(test_file)],
            watch_type="file",
        )
        watcher.add_watch(rule)
        await watcher.check()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_multiple_file_watches(self, watcher, bus, tmp_path):
        """Multiple files in one rule should all be monitored."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w5",
            project_id="proj-1",
            paths=[str(f1), str(f2)],
            watch_type="file",
        )
        watcher.add_watch(rule)

        time.sleep(0.05)
        f1.write_text("a-modified")

        await watcher.check()
        assert len(events) == 1
        assert str(f1) in events[0]["path"]


# --- Folder watch tests ---


class TestFolderWatch:
    @pytest.mark.asyncio
    async def test_detects_new_file_in_folder(self, watcher, bus, tmp_path):
        """Adding a file to a watched folder should emit folder.changed."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "existing.py").write_text("pass")

        events = []
        bus.subscribe("folder.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w6",
            project_id="proj-1",
            paths=[str(src_dir)],
            watch_type="folder",
        )
        watcher.add_watch(rule)

        # Add a new file
        (src_dir / "new_module.py").write_text("def hello(): pass")

        await watcher.check()
        # Wait for debounce
        await asyncio.sleep(0.2)
        await watcher.check()

        assert len(events) == 1
        assert events[0]["count"] == 1
        changes = {c["path"]: c["operation"] for c in events[0]["changes"]}
        assert "new_module.py" in changes
        assert changes["new_module.py"] == "created"

    @pytest.mark.asyncio
    async def test_detects_deleted_file_in_folder(self, watcher, bus, tmp_path):
        """Removing a file from a watched folder should emit folder.changed."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        victim = src_dir / "victim.py"
        victim.write_text("pass")

        events = []
        bus.subscribe("folder.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w7",
            project_id="proj-1",
            paths=[str(src_dir)],
            watch_type="folder",
        )
        watcher.add_watch(rule)

        os.remove(str(victim))

        await watcher.check()
        await asyncio.sleep(0.2)
        await watcher.check()

        assert len(events) == 1
        changes = {c["path"]: c["operation"] for c in events[0]["changes"]}
        assert "victim.py" in changes
        assert changes["victim.py"] == "deleted"

    @pytest.mark.asyncio
    async def test_extension_filter(self, watcher, bus, tmp_path):
        """Folder watch with extensions filter should only track matching files."""
        src_dir = tmp_path / "docs"
        src_dir.mkdir()

        events = []
        bus.subscribe("folder.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w8",
            project_id="proj-1",
            paths=[str(src_dir)],
            watch_type="folder",
            extensions=[".md"],
        )
        watcher.add_watch(rule)

        # Add both .md and .txt files
        (src_dir / "readme.md").write_text("# Readme")
        (src_dir / "notes.txt").write_text("text")

        await watcher.check()
        await asyncio.sleep(0.2)
        await watcher.check()

        assert len(events) == 1
        paths = [c["path"] for c in events[0]["changes"]]
        assert "readme.md" in paths
        assert "notes.txt" not in paths

    @pytest.mark.asyncio
    async def test_recursive_folder_watch(self, watcher, bus, tmp_path):
        """Recursive folder watch should detect files in subdirectories."""
        src_dir = tmp_path / "project"
        src_dir.mkdir()
        sub_dir = src_dir / "sub"
        sub_dir.mkdir()

        events = []
        bus.subscribe("folder.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w9",
            project_id="proj-1",
            paths=[str(src_dir)],
            watch_type="folder",
            recursive=True,
        )
        watcher.add_watch(rule)

        # Add a file in subdirectory
        (sub_dir / "deep.py").write_text("pass")

        await watcher.check()
        await asyncio.sleep(0.2)
        await watcher.check()

        assert len(events) == 1
        paths = [c["path"] for c in events[0]["changes"]]
        assert any("deep.py" in p for p in paths)

    @pytest.mark.asyncio
    async def test_debouncing(self, bus, tmp_path):
        """Multiple rapid changes should be debounced into one event."""
        watcher = FileWatcher(bus, debounce_seconds=0.3, poll_interval=0.0)
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        events = []
        bus.subscribe("folder.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w10",
            project_id="proj-1",
            paths=[str(src_dir)],
            watch_type="folder",
        )
        watcher.add_watch(rule)

        # Rapid changes
        (src_dir / "a.py").write_text("a")
        await watcher.check()

        (src_dir / "b.py").write_text("b")
        await watcher.check()

        # Not enough time has passed for debounce
        assert len(events) == 0

        # Wait for debounce window
        await asyncio.sleep(0.4)
        await watcher.check()

        # Should get one aggregated event
        assert len(events) == 1
        assert events[0]["count"] == 2


# --- Watch management ---


class TestWatchManagement:
    def test_add_and_remove_watch(self, watcher):
        rule = WatchRule(
            watch_id="w11",
            project_id="proj-1",
            paths=["/tmp/test.txt"],
            watch_type="file",
        )
        watcher.add_watch(rule)
        assert watcher.get_watch_count() == 1

        watcher.remove_watch("w11")
        assert watcher.get_watch_count() == 0

    def test_remove_nonexistent_watch(self, watcher):
        """Removing a non-existent watch should not error."""
        watcher.remove_watch("nonexistent")
        assert watcher.get_watch_count() == 0

    @pytest.mark.asyncio
    async def test_poll_interval_respected(self, bus, tmp_path):
        """Watcher should skip checks when poll interval hasn't elapsed."""
        watcher = FileWatcher(bus, poll_interval=100.0)
        test_file = tmp_path / "f.txt"
        test_file.write_text("x")

        events = []
        bus.subscribe("file.changed", lambda d: events.append(d))

        rule = WatchRule(
            watch_id="w12",
            project_id="proj-1",
            paths=[str(test_file)],
            watch_type="file",
        )
        watcher.add_watch(rule)

        # First check sets _last_poll
        await watcher.check()

        # Modify file
        time.sleep(0.05)
        test_file.write_text("y")

        # Second check should be skipped due to poll interval
        await watcher.check()
        assert len(events) == 0


# --- FileState helpers ---


class TestFileState:
    def test_detect_modification(self):
        old = _FileState(mtime=1.0, size=100, exists=True)
        new = _FileState(mtime=2.0, size=100, exists=True)
        assert FileWatcher._detect_file_operation(old, new) == "modified"

    def test_detect_creation(self):
        old = _FileState(exists=False)
        new = _FileState(mtime=1.0, size=50, exists=True)
        assert FileWatcher._detect_file_operation(old, new) == "created"

    def test_detect_deletion(self):
        old = _FileState(mtime=1.0, size=50, exists=True)
        new = _FileState(exists=False)
        assert FileWatcher._detect_file_operation(old, new) == "deleted"

    def test_no_change(self):
        state = _FileState(mtime=1.0, size=100, exists=True)
        assert FileWatcher._detect_file_operation(state, state) is None

    def test_resolve_relative_path(self):
        result = FileWatcher._resolve_path("src/main.py", "/project")
        assert result == "/project/src/main.py"

    def test_resolve_absolute_path(self):
        result = FileWatcher._resolve_path("/abs/path.py", "/project")
        assert result == "/abs/path.py"
