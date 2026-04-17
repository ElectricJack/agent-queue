"""Tests for scripts/migrate_task_records.py."""

from __future__ import annotations

import os

# Import the migration module from scripts/
import importlib.util

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


def _write(path: str, content: str = "hello") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# discover_source_projects
# ---------------------------------------------------------------------------


def test_discover_no_memory_dir(tmp_path):
    """Returns empty list when memory/ doesn't exist."""
    assert discover_source_projects(str(tmp_path)) == []


def test_discover_empty_memory(tmp_path):
    """Returns empty list when memory/ has no project dirs with tasks/."""
    os.makedirs(tmp_path / "memory" / "proj1")
    assert discover_source_projects(str(tmp_path)) == []


def test_discover_finds_projects(tmp_path):
    """Finds projects that have a tasks/ subdirectory."""
    os.makedirs(tmp_path / "memory" / "alpha" / "tasks")
    os.makedirs(tmp_path / "memory" / "beta" / "tasks")
    os.makedirs(tmp_path / "memory" / "gamma")  # no tasks/ dir
    result = discover_source_projects(str(tmp_path))
    assert result == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# migrate_project — basic move
# ---------------------------------------------------------------------------


def test_move_files(tmp_path):
    """Files are moved from memory/proj/tasks/ to tasks/proj/."""
    data = str(tmp_path)
    content = "# Task record\nSome content here"
    _write(os.path.join(data, "memory", "proj", "tasks", "task-a.md"), content)
    _write(os.path.join(data, "memory", "proj", "tasks", "task-b.md"), content)
    os.makedirs(os.path.join(data, "tasks", "proj"), exist_ok=True)

    moved, skipped, errors = migrate_project(data, "proj", execute=True)

    assert moved == 2
    assert skipped == 0
    assert errors == 0

    # Destination files exist with correct content
    for name in ("task-a.md", "task-b.md"):
        dst = os.path.join(data, "tasks", "proj", name)
        assert os.path.isfile(dst)
        with open(dst) as f:
            assert f.read() == content

    # Source files removed
    for name in ("task-a.md", "task-b.md"):
        src = os.path.join(data, "memory", "proj", "tasks", name)
        assert not os.path.exists(src)


def test_creates_destination_dir(tmp_path):
    """Destination directory is created if it doesn't exist."""
    data = str(tmp_path)
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"))

    moved, _, _ = migrate_project(data, "proj", execute=True)
    assert moved == 1
    assert os.path.isfile(os.path.join(data, "tasks", "proj", "task.md"))


# ---------------------------------------------------------------------------
# migrate_project — idempotency
# ---------------------------------------------------------------------------


def test_idempotent_already_moved(tmp_path):
    """Running twice: second run has nothing to move."""
    data = str(tmp_path)
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "content")
    os.makedirs(os.path.join(data, "tasks", "proj"), exist_ok=True)

    # First run
    moved1, _, _ = migrate_project(data, "proj", execute=True)
    assert moved1 == 1

    # Source is gone — project still has the tasks/ dir but no files
    moved2, skipped2, errors2 = migrate_project(data, "proj", execute=True)
    assert moved2 == 0
    assert skipped2 == 0
    assert errors2 == 0


def test_idempotent_source_still_present(tmp_path):
    """If source and destination have identical content, source is cleaned up."""
    data = str(tmp_path)
    content = "identical content"
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), content)
    _write(os.path.join(data, "tasks", "proj", "task.md"), content)

    moved, skipped, errors = migrate_project(data, "proj", execute=True)

    assert moved == 0
    assert skipped == 1
    assert errors == 0
    # Source cleaned up
    assert not os.path.exists(os.path.join(data, "memory", "proj", "tasks", "task.md"))
    # Destination untouched
    with open(os.path.join(data, "tasks", "proj", "task.md")) as f:
        assert f.read() == content


def test_conflict_different_content(tmp_path):
    """If destination exists with different content, it's flagged as an error."""
    data = str(tmp_path)
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "version A")
    _write(os.path.join(data, "tasks", "proj", "task.md"), "version B")

    moved, skipped, errors = migrate_project(data, "proj", execute=True)

    assert moved == 0
    assert skipped == 0
    assert errors == 1
    # Neither file is removed
    assert os.path.isfile(os.path.join(data, "memory", "proj", "tasks", "task.md"))
    assert os.path.isfile(os.path.join(data, "tasks", "proj", "task.md"))


# ---------------------------------------------------------------------------
# migrate_project — empty / missing directories
# ---------------------------------------------------------------------------


def test_empty_source_dir(tmp_path):
    """Empty tasks directory is handled gracefully."""
    data = str(tmp_path)
    os.makedirs(os.path.join(data, "memory", "proj", "tasks"))

    moved, skipped, errors = migrate_project(data, "proj", execute=True)

    assert moved == 0
    assert skipped == 0
    assert errors == 0


def test_missing_source_dir(tmp_path):
    """Non-existent source directory is handled gracefully."""
    data = str(tmp_path)

    moved, skipped, errors = migrate_project(data, "nonexistent", execute=True)

    assert moved == 0
    assert skipped == 0
    assert errors == 0


# ---------------------------------------------------------------------------
# migrate_project — dry run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_modify(tmp_path):
    """Dry run reports what would happen but doesn't change files."""
    data = str(tmp_path)
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "content")

    moved, skipped, errors = migrate_project(data, "proj", execute=False)

    assert moved == 1
    assert skipped == 0
    assert errors == 0
    # Source still exists
    assert os.path.isfile(os.path.join(data, "memory", "proj", "tasks", "task.md"))
    # Destination was NOT created
    assert not os.path.exists(os.path.join(data, "tasks", "proj", "task.md"))


# ---------------------------------------------------------------------------
# run_migration — end-to-end
# ---------------------------------------------------------------------------


def test_full_migration(tmp_path):
    """End-to-end migration across multiple projects."""
    data = str(tmp_path)

    # Set up two projects with task files
    for proj in ("alpha", "beta"):
        for i in range(3):
            _write(
                os.path.join(data, "memory", proj, "tasks", f"task-{i}.md"),
                f"content-{proj}-{i}",
            )

    # Set up an empty project
    os.makedirs(os.path.join(data, "memory", "empty", "tasks"))

    success = run_migration(data, execute=True)
    assert success is True

    # Verify all files migrated
    for proj in ("alpha", "beta"):
        for i in range(3):
            dst = os.path.join(data, "tasks", proj, f"task-{i}.md")
            assert os.path.isfile(dst)
            with open(dst) as f:
                assert f.read() == f"content-{proj}-{i}"
            # Source removed
            src = os.path.join(data, "memory", proj, "tasks", f"task-{i}.md")
            assert not os.path.exists(src)


def test_full_migration_returns_false_on_errors(tmp_path):
    """run_migration returns False when there are conflicts."""
    data = str(tmp_path)
    _write(os.path.join(data, "memory", "proj", "tasks", "task.md"), "version A")
    _write(os.path.join(data, "tasks", "proj", "task.md"), "version B")

    success = run_migration(data, execute=True)
    assert success is False


def test_byte_for_byte_preservation(tmp_path):
    """Binary content is preserved exactly (not just text)."""
    data = str(tmp_path)
    # Write content with mixed line endings, trailing whitespace, etc.
    content = "line1\r\nline2\n\ttabbed\x00null byte\n"
    src_path = os.path.join(data, "memory", "proj", "tasks", "task.md")
    os.makedirs(os.path.dirname(src_path), exist_ok=True)
    with open(src_path, "w", newline="") as f:
        f.write(content)

    migrate_project(data, "proj", execute=True)

    dst_path = os.path.join(data, "tasks", "proj", "task.md")
    with open(dst_path, "r", newline="") as f:
        assert f.read() == content
