"""Behavioural coverage for plan-file discovery."""

from __future__ import annotations

import os

from src.plan_parser import (
    DEFAULT_PLAN_FILE_PATTERNS,
    find_all_plan_files,
    find_plan_file,
    read_plan_file,
)


def _write(path, text: str = "# plan\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_find_plan_file_returns_only_canonical_path(tmp_path):
    assert DEFAULT_PLAN_FILE_PATTERNS == [".claude/plan.md"]
    _write(tmp_path / "plan.md")
    _write(tmp_path / "docs" / "plans" / "new.md")
    canonical = _write(tmp_path / ".claude" / "plan.md")
    assert find_plan_file(str(tmp_path)) == canonical
    os.remove(canonical)
    assert find_plan_file(str(tmp_path)) is None


def test_find_plan_file_honours_explicit_ordered_patterns(tmp_path):
    _write(tmp_path / "a.md")
    _write(tmp_path / "b.md")
    assert find_plan_file(str(tmp_path), ["a.md", "b.md"]) == str(tmp_path / "a.md")
    assert find_plan_file(str(tmp_path), ["b.md", "a.md"]) == str(tmp_path / "b.md")
    assert find_plan_file(str(tmp_path), ["missing.md", "b.md"]) == str(tmp_path / "b.md")
    assert find_plan_file(str(tmp_path), ["missing.md"]) is None


def test_find_all_plan_files_includes_current_candidates_newest_first(tmp_path):
    base = 1_700_000_000.0
    paths = [
        (tmp_path / "plan.md", base + 10),
        (tmp_path / ".claude" / "plans" / "alpha.md", base + 20),
        (tmp_path / ".claude" / "plan.md", base + 30),
        (tmp_path / ".claude" / "plans" / "beta.md", base + 40),
    ]
    for path, mtime in paths:
        _write(path)
        os.utime(path, (mtime, mtime))
    found = find_all_plan_files(str(tmp_path))
    assert [entry["path"] for entry in found] == [str(path) for path, _ in paths[::-1]]
    assert all(set(entry) == {"path", "ctime"} and os.path.isabs(entry["path"]) for entry in found)
    assert [entry["ctime"] for entry in found] == sorted(
        (entry["ctime"] for entry in found), reverse=True
    )


def test_find_all_plan_files_excludes_archived_and_ignores_stat_race(tmp_path, monkeypatch):
    active = _write(tmp_path / ".claude" / "plans" / "current.md")
    _write(tmp_path / ".claude" / "plans" / "task-1-plan.md")
    _write(tmp_path / ".claude" / "plans" / "stale-task-2-plan.md")
    raced = _write(tmp_path / "plan.md")
    real_stat = os.stat
    calls = 0

    def racing_stat(path, *args, **kwargs):
        nonlocal calls
        if os.fspath(path) == raced:
            calls += 1
            if calls > 1:
                raise OSError(2, "gone", raced)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", racing_stat)
    assert [entry["path"] for entry in find_all_plan_files(str(tmp_path))] == [active]
    assert calls >= 2


def test_read_plan_file_preserves_utf8_content(tmp_path):
    content = "# Plan — étape 1\n\n- [ ] café ☕\n"
    assert read_plan_file(_write(tmp_path / ".claude" / "plan.md", content)) == content
