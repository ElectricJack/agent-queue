"""Comprehensive tests for task record storage migration (vault spec §6).

Covers all required scenarios:
(a) New task record writes to ~/.agent-queue/tasks/{project_id}/, not memory/*/tasks/
(b) Memory search returns zero results from task files (no task pollution)
(c) Migration script moves existing task files and preserves content byte-for-byte
(d) Migration script is idempotent — running twice does not duplicate or corrupt
(e) Old task path is empty after migration
(f) Task read operations find records at new location
(g) Projects with no existing tasks do not cause migration errors
(h) Re-index after migration produces a clean collection with no task entries
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

from src.memory import MemoryConfig, MemoryManager

# ---------------------------------------------------------------------------
# Import migration script functions
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location(
    "migrate_task_records",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "migrate_task_records.py"),
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

discover_source_projects = _mod.discover_source_projects
migrate_project = _mod.migrate_project
run_migration = _mod.run_migration


# ---------------------------------------------------------------------------
# Lightweight fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-123"
    project_id: str = "my-project"
    title: str = "Add user auth"
    description: str = "Implement JWT authentication"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


@dataclass
class FakeOutput:
    result: MagicMock = field(default_factory=lambda: MagicMock(value="completed"))
    summary: str = "Added JWT auth with refresh tokens."
    files_changed: list = field(default_factory=lambda: ["src/auth.py", "tests/test_auth.py"])
    tokens_used: int = 12345


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(storage_root: str, **overrides) -> MemoryManager:
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=storage_root)


def _write(path: str, content: str = "hello") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _write_binary(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ===========================================================================
# (a) New task record writes to tasks/{project_id}/, NOT memory/*/tasks/
# ===========================================================================


class TestNewTaskRecordLocation:
    """Verify that remember() writes task files to the new canonical location."""

    def test_tasks_dir_returns_new_location(self, tmp_path):
        """_tasks_dir() returns {storage_root}/tasks/{project_id}/."""
        mgr = _make_manager(str(tmp_path))
        assert mgr._tasks_dir("my-project") == os.path.join(str(tmp_path), "tasks", "my-project")

    def test_tasks_dir_does_not_include_memory(self, tmp_path):
        """_tasks_dir() path must not contain 'memory' anywhere."""
        mgr = _make_manager(str(tmp_path))
        path = mgr._tasks_dir("my-project")
        assert "memory" not in path

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_writes_to_tasks_dir(self, MockMemSearch, tmp_path):
        """remember() creates the file under tasks/{project_id}/."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(id="task-abc", project_id="my-project")
        output = FakeOutput()

        path = await mgr.remember(task, output, str(tmp_path))

        assert path is not None
        expected_dir = os.path.join(str(tmp_path), "tasks", "my-project")
        assert path.startswith(expected_dir)
        assert path.endswith("task-abc.md")
        assert os.path.isfile(path)

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_does_not_write_to_memory_tasks(self, MockMemSearch, tmp_path):
        """remember() must NOT create files in memory/*/tasks/ (legacy location)."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(id="task-xyz", project_id="proj")
        output = FakeOutput()

        await mgr.remember(task, output, str(tmp_path))

        legacy_dir = os.path.join(str(tmp_path), "memory", "proj", "tasks")
        if os.path.isdir(legacy_dir):
            assert os.listdir(legacy_dir) == [], (
                f"Legacy memory/proj/tasks/ should be empty but contains: {os.listdir(legacy_dir)}"
            )

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_creates_tasks_directory(self, MockMemSearch, tmp_path):
        """remember() creates the tasks/{project_id}/ directory if needed."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        tasks_dir = os.path.join(str(tmp_path), "tasks", "new-project")
        assert not os.path.exists(tasks_dir)

        task = FakeTask(id="task-001", project_id="new-project")
        await mgr.remember(task, FakeOutput(), str(tmp_path))

        assert os.path.isdir(tasks_dir)

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_multiple_tasks_same_project(self, MockMemSearch, tmp_path):
        """Multiple task records for the same project coexist in tasks/{project}/."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))

        for i in range(5):
            task = FakeTask(id=f"task-{i:03d}", project_id="bulk-proj")
            await mgr.remember(task, FakeOutput(), str(tmp_path))

        tasks_dir = os.path.join(str(tmp_path), "tasks", "bulk-proj")
        files = sorted(os.listdir(tasks_dir))
        assert files == [f"task-{i:03d}.md" for i in range(5)]


# ===========================================================================
# (b) Memory search returns zero results from task files (no task pollution)
# ===========================================================================


class TestNoTaskPollution:
    """Verify that task files do NOT pollute the memory search index."""

    def test_memory_paths_excludes_tasks_dir(self, tmp_path):
        """_memory_paths() must not include the tasks/ directory."""
        mgr = _make_manager(str(tmp_path))
        paths = mgr._memory_paths("my-project", str(tmp_path))
        tasks_dir = mgr._tasks_dir("my-project")
        assert tasks_dir not in paths
        # Also ensure no path contains '/tasks/' as a component
        for p in paths:
            # The 'tasks' directory at storage root level should not be included
            # (memory/proj is fine, tasks/proj is not)
            assert not (
                str(tmp_path) in p and p.startswith(os.path.join(str(tmp_path), "tasks"))
            ), f"tasks/ directory included in memory paths: {p}"

    def test_memory_paths_includes_memory_dir_not_tasks(self, tmp_path):
        """_memory_paths() includes memory/{project}/ but not tasks/{project}/."""
        mgr = _make_manager(str(tmp_path))
        paths = mgr._memory_paths("test-proj", str(tmp_path))
        memory_dir = mgr._project_memory_dir("test-proj")
        tasks_dir = mgr._tasks_dir("test-proj")
        assert memory_dir in paths
        assert tasks_dir not in paths

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_memsearch_not_initialized_with_tasks_path(self, MockMemSearch, tmp_path):
        """MemSearch constructor is NOT given the tasks/ directory in paths."""
        MockMemSearch.return_value = MagicMock()

        mgr = _make_manager(str(tmp_path))
        await mgr.get_instance("proj", str(tmp_path))

        # Inspect the paths= argument given to MemSearch
        call_kwargs = MockMemSearch.call_args[1]
        indexed_paths = call_kwargs["paths"]
        tasks_dir = mgr._tasks_dir("proj")
        assert tasks_dir not in indexed_paths, (
            f"MemSearch was initialized with tasks dir in paths: {indexed_paths}"
        )

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_search_after_remember_does_not_include_task_in_paths(
        self, MockMemSearch, tmp_path
    ):
        """After remember(), the MemSearch instance's indexed paths still exclude tasks/."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        mock_instance.search = AsyncMock(return_value=[])
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(project_id="proj")
        await mgr.remember(task, FakeOutput(), str(tmp_path))

        # Verify MemSearch was created without tasks/ in paths
        call_kwargs = MockMemSearch.call_args[1]
        for p in call_kwargs["paths"]:
            assert not p.startswith(os.path.join(str(tmp_path), "tasks")), (
                f"MemSearch paths should not include tasks/: {p}"
            )

    def test_legacy_tasks_dir_inside_memory_tree(self, tmp_path):
        """The legacy location memory/{project}/tasks/ IS inside the memory tree.

        This confirms why migration is necessary — the old location would be
        recursively indexed by MemSearch as part of memory/{project}/.
        """
        mgr = _make_manager(str(tmp_path))
        memory_dir = mgr._project_memory_dir("proj")
        legacy_tasks = os.path.join(memory_dir, "tasks")

        # Legacy location is a subdirectory of what MemSearch indexes
        assert legacy_tasks.startswith(memory_dir)

        # New location is NOT under memory/
        new_tasks = mgr._tasks_dir("proj")
        assert not new_tasks.startswith(memory_dir)


# ===========================================================================
# (c) Migration script moves files and preserves content byte-for-byte
# ===========================================================================


class TestMigrationPreservesContent:
    """Verify that both inline and script migration preserve file content exactly."""

    def test_inline_migration_preserves_content(self, tmp_path):
        """_migrate_legacy_tasks() preserves exact file content."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        content = "# Task: task-001 — Fix the bug\n\nSome detailed content.\n"
        (legacy_dir / "task-001.md").write_text(content)

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        dst = tmp_path / "tasks" / "proj" / "task-001.md"
        assert dst.read_text() == content

    def test_inline_migration_preserves_multiple_files(self, tmp_path):
        """All files moved by inline migration retain original content."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)

        files = {}
        for i in range(10):
            name = f"task-{i:03d}.md"
            content = f"# Task {i}\n\nContent for task {i} with special chars: é ñ ü\n"
            (legacy_dir / name).write_text(content)
            files[name] = content

        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is True
        for name, expected_content in files.items():
            dst = tmp_path / "tasks" / "proj" / name
            assert dst.read_text() == expected_content, f"Content mismatch for {name}"

    def test_script_migration_byte_for_byte(self, tmp_path):
        """Script migration preserves mixed line endings and special bytes."""
        data = str(tmp_path)
        content = "line1\r\nline2\n\ttabbed\x00null byte\n"
        src_path = os.path.join(data, "memory", "proj", "tasks", "task.md")
        os.makedirs(os.path.dirname(src_path), exist_ok=True)
        with open(src_path, "w", newline="") as f:
            f.write(content)

        migrate_project(data, "proj", execute=True)

        dst_path = os.path.join(data, "tasks", "proj", "task.md")
        with open(dst_path, "r", newline="") as f:
            assert f.read() == content

    def test_script_migration_binary_content(self, tmp_path):
        """Script migration preserves binary file content exactly."""
        data = str(tmp_path)
        # Content with various byte patterns
        content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
        src_path = os.path.join(data, "memory", "proj", "tasks", "task-bin.md")
        os.makedirs(os.path.dirname(src_path), exist_ok=True)
        with open(src_path, "wb") as f:
            f.write(content)

        migrate_project(data, "proj", execute=True)

        dst_path = os.path.join(data, "tasks", "proj", "task-bin.md")
        with open(dst_path, "rb") as f:
            assert f.read() == content

    def test_script_migration_unicode_content(self, tmp_path):
        """Script migration preserves unicode content correctly."""
        data = str(tmp_path)
        content = "# 任务报告\n\n这是一个测试。\nEmoji: 🚀🎉\nGreek: αβγδ\n"
        _write(os.path.join(data, "memory", "proj", "tasks", "task-unicode.md"), content)

        migrate_project(data, "proj", execute=True)

        dst_path = os.path.join(data, "tasks", "proj", "task-unicode.md")
        with open(dst_path) as f:
            assert f.read() == content

    def test_inline_migration_returns_true_on_migration(self, tmp_path):
        """_migrate_legacy_tasks() returns True when files are actually moved."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task.md").write_text("content")

        mgr = _make_manager(str(tmp_path))
        assert mgr._migrate_legacy_tasks("proj") is True


# ===========================================================================
# (d) Migration script is idempotent — running twice does not duplicate or corrupt
# ===========================================================================


class TestMigrationIdempotency:
    """Verify that running migration multiple times is safe."""

    def test_inline_migration_idempotent(self, tmp_path):
        """Running _migrate_legacy_tasks() twice is safe."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task-001.md").write_text("content A")
        (legacy_dir / "task-002.md").write_text("content B")

        mgr = _make_manager(str(tmp_path))

        # First run moves the files
        result1 = mgr._migrate_legacy_tasks("proj")
        assert result1 is True

        # Second run is a no-op (legacy dir is already gone)
        result2 = mgr._migrate_legacy_tasks("proj")
        assert result2 is False

        # Files still intact at destination
        new_dir = tmp_path / "tasks" / "proj"
        assert (new_dir / "task-001.md").read_text() == "content A"
        assert (new_dir / "task-002.md").read_text() == "content B"

    def test_inline_migration_no_duplicate_files(self, tmp_path):
        """Running migration twice does not create duplicate files."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task-001.md").write_text("content")

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")
        mgr._migrate_legacy_tasks("proj")

        new_dir = tmp_path / "tasks" / "proj"
        files = os.listdir(new_dir)
        assert files == ["task-001.md"], f"Expected exactly one file, got: {files}"

    def test_script_migration_idempotent_first_run(self, tmp_path):
        """Script migrate_project: first run moves files."""
        data = str(tmp_path)
        _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "content")

        moved1, skipped1, errors1 = migrate_project(data, "proj", execute=True)
        assert moved1 == 1
        assert errors1 == 0

    def test_script_migration_idempotent_second_run(self, tmp_path):
        """Script migrate_project: second run has nothing to move (source gone)."""
        data = str(tmp_path)
        _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "content")

        migrate_project(data, "proj", execute=True)

        # Second run — source dir no longer has the file
        moved2, skipped2, errors2 = migrate_project(data, "proj", execute=True)
        assert moved2 == 0
        assert errors2 == 0

    def test_script_migration_idempotent_with_identical_copy(self, tmp_path):
        """If source and destination are identical, source is cleaned up without error."""
        data = str(tmp_path)
        content = "same content"
        _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), content)
        _write(os.path.join(data, "tasks", "proj", "task.md"), content)

        moved, skipped, errors = migrate_project(data, "proj", execute=True)

        assert moved == 0
        assert skipped == 1
        assert errors == 0
        # Destination content unchanged
        with open(os.path.join(data, "tasks", "proj", "task.md")) as f:
            assert f.read() == content

    def test_script_full_migration_idempotent(self, tmp_path):
        """run_migration() is safe to call repeatedly."""
        data = str(tmp_path)
        for proj in ("alpha", "beta"):
            _write(os.path.join(data, "memory", proj, "tasks", "task.md"), f"content-{proj}")

        success1 = run_migration(data, execute=True)
        assert success1 is True

        success2 = run_migration(data, execute=True)
        assert success2 is True

        # Files still correct
        for proj in ("alpha", "beta"):
            with open(os.path.join(data, "tasks", proj, "task.md")) as f:
                assert f.read() == f"content-{proj}"

    def test_inline_migration_handles_preexisting_destination(self, tmp_path):
        """If destination already has the file, legacy copy is removed (not duplicated)."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task.md").write_text("stale copy")

        new_dir = tmp_path / "tasks" / "proj"
        new_dir.mkdir(parents=True)
        (new_dir / "task.md").write_text("already migrated")

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        # Destination preserved (existing file wins)
        assert (new_dir / "task.md").read_text() == "already migrated"
        # Legacy directory removed
        assert not legacy_dir.exists()


# ===========================================================================
# (e) Old task path is empty after migration
# ===========================================================================


class TestOldPathEmptyAfterMigration:
    """Verify that the legacy tasks directory is cleaned up after migration."""

    def test_inline_migration_removes_legacy_dir(self, tmp_path):
        """_migrate_legacy_tasks() removes the legacy tasks/ directory entirely."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task-001.md").write_text("content")
        (legacy_dir / "task-002.md").write_text("content")

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        assert not legacy_dir.exists(), "Legacy tasks/ directory should be removed"

    def test_inline_migration_removes_empty_legacy_dir(self, tmp_path):
        """Even an empty legacy tasks/ directory is cleaned up."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        assert not legacy_dir.exists()

    def test_inline_migration_parent_memory_dir_preserved(self, tmp_path):
        """The parent memory/{project}/ directory is NOT removed — only tasks/ inside it."""
        memory_dir = tmp_path / "memory" / "proj"
        legacy_dir = memory_dir / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task.md").write_text("content")
        # Place a non-task file in the memory dir
        (memory_dir / "profile.md").write_text("# Profile")

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        assert not legacy_dir.exists(), "Legacy tasks/ dir should be gone"
        assert memory_dir.exists(), "Parent memory/proj/ should be preserved"
        assert (memory_dir / "profile.md").read_text() == "# Profile"

    def test_script_migration_source_files_removed(self, tmp_path):
        """Script migration removes source files after successful copy."""
        data = str(tmp_path)
        files = ["task-a.md", "task-b.md", "task-c.md"]
        for f in files:
            _write(os.path.join(data, "memory", "proj", "tasks", f), f"content-{f}")

        migrate_project(data, "proj", execute=True)

        for f in files:
            src = os.path.join(data, "memory", "proj", "tasks", f)
            assert not os.path.exists(src), f"Source file {f} should be removed"

    def test_no_legacy_files_remain_after_full_migration(self, tmp_path):
        """After run_migration, no project has files in memory/*/tasks/."""
        data = str(tmp_path)
        projects = ["proj-a", "proj-b", "proj-c"]
        for proj in projects:
            for i in range(3):
                _write(
                    os.path.join(data, "memory", proj, "tasks", f"task-{i}.md"),
                    f"content-{proj}-{i}",
                )

        run_migration(data, execute=True)

        for proj in projects:
            tasks_dir = os.path.join(data, "memory", proj, "tasks")
            if os.path.isdir(tasks_dir):
                remaining = os.listdir(tasks_dir)
                assert remaining == [], f"Legacy tasks/ for {proj} still has files: {remaining}"


# ===========================================================================
# (f) Task read operations find records at new location
# ===========================================================================


class TestTaskReadAtNewLocation:
    """Verify that task records are findable at the new location."""

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_returns_new_path(self, MockMemSearch, tmp_path):
        """remember() returns a file path under tasks/{project_id}/."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(id="task-read-01", project_id="proj")
        path = await mgr.remember(task, FakeOutput(), str(tmp_path))

        assert path is not None
        expected = os.path.join(str(tmp_path), "tasks", "proj", "task-read-01.md")
        assert path == expected
        assert os.path.isfile(path)

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_file_readable_with_expected_content(self, MockMemSearch, tmp_path):
        """The file written by remember() can be read back with correct content."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(id="task-read-02", title="Fix login bug", project_id="proj")
        output = FakeOutput(summary="Fixed the redirect loop", files_changed=["src/login.py"])
        path = await mgr.remember(task, output, str(tmp_path))

        content = open(path).read()
        assert "# Task: task-read-02 — Fix login bug" in content
        assert "Fixed the redirect loop" in content
        assert "src/login.py" in content

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_remember_file_indexed_at_new_path(self, MockMemSearch, tmp_path):
        """index_file is called with the new tasks/ path, not a memory/ path."""
        mock_instance = MagicMock()
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(id="task-idx", project_id="proj")
        path = await mgr.remember(task, FakeOutput(), str(tmp_path))

        mock_instance.index_file.assert_called_once_with(path)
        assert "tasks/proj/task-idx.md" in path.replace("\\", "/")
        assert "memory" not in path

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_recall_searches_via_memsearch(self, MockMemSearch, tmp_path):
        """recall() delegates to MemSearch.search(), which can find indexed tasks."""
        mock_instance = MagicMock()
        mock_instance.search = AsyncMock(
            return_value=[{"content": "# Task: task-001", "source": "tasks/proj/task-001.md"}]
        )
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        task = FakeTask(title="auth module", description="JWT implementation")
        results = await mgr.recall(task, str(tmp_path))

        assert len(results) == 1
        assert "task-001" in results[0]["content"]

    def test_migrated_files_readable_at_new_location(self, tmp_path):
        """After inline migration, files are readable at the new canonical path."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        content = "# Task: migrated-001 — Important fix\n\nDetailed content here.\n"
        (legacy_dir / "migrated-001.md").write_text(content)

        mgr = _make_manager(str(tmp_path))
        mgr._migrate_legacy_tasks("proj")

        new_path = tmp_path / "tasks" / "proj" / "migrated-001.md"
        assert new_path.exists()
        assert new_path.read_text() == content

    def test_script_migrated_files_readable(self, tmp_path):
        """After script migration, files are readable at the destination."""
        data = str(tmp_path)
        content = "# Task record with\nmultiline content\nand special chars: àéîõü\n"
        _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), content)

        migrate_project(data, "proj", execute=True)

        dst = os.path.join(data, "tasks", "proj", "task.md")
        with open(dst) as f:
            assert f.read() == content


# ===========================================================================
# (g) Projects with no existing tasks do not cause migration errors
# ===========================================================================


class TestEmptyProjectMigration:
    """Verify graceful handling of projects with no task files."""

    def test_inline_migration_no_legacy_dir(self, tmp_path):
        """No legacy tasks/ directory — _migrate_legacy_tasks returns False, no error."""
        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("nonexistent-proj")
        assert result is False

    def test_inline_migration_empty_legacy_dir(self, tmp_path):
        """Empty legacy tasks/ directory — cleaned up without error."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)

        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is False
        assert not legacy_dir.exists()

    def test_inline_migration_memory_dir_without_tasks(self, tmp_path):
        """Project has memory/ directory but no tasks/ subdirectory — no error."""
        memory_dir = tmp_path / "memory" / "proj"
        memory_dir.mkdir(parents=True)
        (memory_dir / "profile.md").write_text("# Profile")

        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is False
        # Other files in memory/ are unaffected
        assert (memory_dir / "profile.md").read_text() == "# Profile"

    def test_script_migration_no_source_dir(self, tmp_path):
        """Script handles non-existent source directory gracefully."""
        data = str(tmp_path)
        moved, skipped, errors = migrate_project(data, "nonexistent", execute=True)
        assert moved == 0
        assert skipped == 0
        assert errors == 0

    def test_script_migration_empty_source_dir(self, tmp_path):
        """Script handles empty source directory gracefully."""
        data = str(tmp_path)
        os.makedirs(os.path.join(data, "memory", "proj", "tasks"))
        moved, skipped, errors = migrate_project(data, "proj", execute=True)
        assert moved == 0
        assert skipped == 0
        assert errors == 0

    def test_script_discover_skips_projects_without_tasks(self, tmp_path):
        """discover_source_projects only finds projects that have a tasks/ dir."""
        data = str(tmp_path)
        os.makedirs(os.path.join(data, "memory", "with-tasks", "tasks"))
        os.makedirs(os.path.join(data, "memory", "no-tasks"))
        os.makedirs(os.path.join(data, "memory", "also-no-tasks", "knowledge"))

        result = discover_source_projects(data)
        assert result == ["with-tasks"]

    def test_script_no_memory_dir_at_all(self, tmp_path):
        """No memory/ directory — discover returns empty list, no crash."""
        data = str(tmp_path)
        result = discover_source_projects(data)
        assert result == []

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_get_instance_with_no_legacy_tasks(self, MockMemSearch, tmp_path):
        """get_instance() works fine for projects that never had legacy tasks."""
        MockMemSearch.return_value = MagicMock()

        mgr = _make_manager(str(tmp_path))
        instance = await mgr.get_instance("fresh-project", str(tmp_path))

        assert instance is not None
        # tasks/ directory created for future use
        assert os.path.isdir(os.path.join(str(tmp_path), "tasks", "fresh-project"))

    def test_full_migration_no_projects(self, tmp_path):
        """run_migration with no projects succeeds cleanly."""
        data = str(tmp_path)
        success = run_migration(data, execute=True)
        assert success is True

    def test_full_migration_mix_of_empty_and_populated(self, tmp_path):
        """run_migration handles a mix of projects with and without tasks."""
        data = str(tmp_path)
        _write(os.path.join(data, "memory", "has-tasks", "tasks", "t.md"), "content")
        os.makedirs(os.path.join(data, "memory", "empty-tasks", "tasks"))
        os.makedirs(os.path.join(data, "memory", "no-tasks-dir"))

        success = run_migration(data, execute=True)
        assert success is True

        # has-tasks was migrated
        assert os.path.isfile(os.path.join(data, "tasks", "has-tasks", "t.md"))


# ===========================================================================
# (h) Re-index after migration produces clean collection with no task entries
# ===========================================================================


class TestReindexAfterMigration:
    """Verify that re-indexing after migration cleans up task entries from the collection."""

    def test_migration_writes_reindex_marker(self, tmp_path):
        """_migrate_legacy_tasks() creates .needs_reindex when files are moved."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task.md").write_text("content")

        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is True
        marker = tmp_path / "memory" / "proj" / ".needs_reindex"
        assert marker.exists()
        assert marker.read_text() == "reindex-after-task-migration\n"

    def test_no_reindex_marker_without_migration(self, tmp_path):
        """No marker when there are no files to migrate."""
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        # Empty dir — nothing to migrate

        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is False
        marker = tmp_path / "memory" / "proj" / ".needs_reindex"
        assert not marker.exists()

    def test_no_reindex_marker_without_legacy_dir(self, tmp_path):
        """No marker when no legacy directory exists."""
        mgr = _make_manager(str(tmp_path))
        result = mgr._migrate_legacy_tasks("proj")

        assert result is False

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_get_instance_triggers_reindex_on_marker(self, MockMemSearch, tmp_path):
        """get_instance() calls index() when .needs_reindex marker exists."""
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(return_value=42)
        MockMemSearch.return_value = mock_instance

        # Create marker
        mem_dir = tmp_path / "memory" / "proj"
        mem_dir.mkdir(parents=True)
        (mem_dir / ".needs_reindex").write_text("reindex-after-task-migration")

        mgr = _make_manager(str(tmp_path))
        instance = await mgr.get_instance("proj", str(tmp_path))

        assert instance is mock_instance
        mock_instance.index.assert_called_once()

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_removes_marker(self, MockMemSearch, tmp_path):
        """After successful reindex, the .needs_reindex marker is removed."""
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(return_value=10)
        MockMemSearch.return_value = mock_instance

        mem_dir = tmp_path / "memory" / "proj"
        mem_dir.mkdir(parents=True)
        marker = mem_dir / ".needs_reindex"
        marker.write_text("reindex-after-task-migration")

        mgr = _make_manager(str(tmp_path))
        await mgr.get_instance("proj", str(tmp_path))

        assert not marker.exists(), "Marker should be removed after reindex"

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_marker_removed_even_on_failure(self, MockMemSearch, tmp_path):
        """Marker is cleaned up even if reindex fails (avoid infinite retry loops)."""
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(side_effect=RuntimeError("index boom"))
        MockMemSearch.return_value = mock_instance

        mem_dir = tmp_path / "memory" / "proj"
        mem_dir.mkdir(parents=True)
        marker = mem_dir / ".needs_reindex"
        marker.write_text("reindex-after-task-migration")

        mgr = _make_manager(str(tmp_path))
        instance = await mgr.get_instance("proj", str(tmp_path))

        # Instance is still returned despite reindex failure
        assert instance is mock_instance
        # Marker cleaned up in finally block
        assert not marker.exists()

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_no_reindex_without_marker(self, MockMemSearch, tmp_path):
        """get_instance() does NOT call index() when there's no marker."""
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock()
        MockMemSearch.return_value = mock_instance

        mgr = _make_manager(str(tmp_path))
        await mgr.get_instance("proj", str(tmp_path))

        mock_instance.index.assert_not_called()

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_after_inline_migration_end_to_end(self, MockMemSearch, tmp_path):
        """Full end-to-end: legacy files → inline migration → get_instance → reindex.

        Simulates the real daemon startup flow:
        1. Legacy task files exist in memory/proj/tasks/
        2. get_instance() is called
        3. _migrate_legacy_tasks() moves the files and writes a marker
        4. MemSearch is created with paths that exclude tasks/
        5. index() is called to purge stale vector entries
        6. Marker is removed
        """
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(return_value=5)
        mock_instance.index_file = AsyncMock()
        MockMemSearch.return_value = mock_instance

        # Set up legacy task files
        legacy_dir = tmp_path / "memory" / "proj" / "tasks"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "task-001.md").write_text("# Task 001")
        (legacy_dir / "task-002.md").write_text("# Task 002")

        mgr = _make_manager(str(tmp_path))
        instance = await mgr.get_instance("proj", str(tmp_path))

        # Verify the full flow
        assert instance is mock_instance

        # 1. Legacy files migrated to new location
        assert not legacy_dir.exists()
        new_dir = tmp_path / "tasks" / "proj"
        assert (new_dir / "task-001.md").read_text() == "# Task 001"
        assert (new_dir / "task-002.md").read_text() == "# Task 002"

        # 2. MemSearch was NOT given the tasks/ directory
        call_kwargs = MockMemSearch.call_args[1]
        for p in call_kwargs["paths"]:
            assert not p.startswith(os.path.join(str(tmp_path), "tasks"))

        # 3. Reindex was triggered (to purge stale entries)
        mock_instance.index.assert_called_once()

        # 4. Marker was cleaned up
        marker = tmp_path / "memory" / "proj" / ".needs_reindex"
        assert not marker.exists()

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_reindex_marker_nonmd_extension(self, MockMemSearch, tmp_path):
        """The .needs_reindex marker has a non-.md extension to avoid being indexed."""
        mgr = _make_manager(str(tmp_path))
        marker_path = mgr._reindex_marker_path("proj")

        assert not marker_path.endswith(".md")
        assert marker_path.endswith(".needs_reindex")

    @patch("src.memory.MEMSEARCH_AVAILABLE", True)
    @patch("src.memory.MemSearch")
    async def test_second_get_instance_skips_reindex(self, MockMemSearch, tmp_path):
        """Second get_instance() call does not re-trigger reindex (instance cached)."""
        mock_instance = MagicMock()
        mock_instance.index = AsyncMock(return_value=0)
        MockMemSearch.return_value = mock_instance

        mem_dir = tmp_path / "memory" / "proj"
        mem_dir.mkdir(parents=True)
        (mem_dir / ".needs_reindex").write_text("reindex-after-task-migration")

        mgr = _make_manager(str(tmp_path))

        # First call triggers reindex
        await mgr.get_instance("proj", str(tmp_path))
        assert mock_instance.index.call_count == 1

        # Second call uses cached instance — no reindex
        await mgr.get_instance("proj", str(tmp_path))
        assert mock_instance.index.call_count == 1  # still 1
