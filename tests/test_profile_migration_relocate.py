"""Destructive one-shot migration: ``relocate_stray_scoped_profiles``.

The migration moves colon-encoded profile directories out of
``vault/agent-types/`` into the canonical project layout.  It runs once, on
upgrade, unattended, and it deletes directories — so the arms that matter
most are the ones nobody watches: a conflicting canonical file, a failing
``os.replace``, and a directory name that does not parse.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.profiles.migration import relocate_stray_scoped_profiles

STRAY_PROFILE = """\
---
id: project:p1:reviewer
name: Stray Reviewer
---

## Config
```json
{"model": "stray-model"}
```
"""

CANONICAL_PROFILE = """\
---
id: project:p1:reviewer
name: Canonical Reviewer
---

## Config
```json
{"model": "canonical-model"}
```
"""


def _stray_dir(data_dir: Path) -> Path:
    d = data_dir / "vault" / "agent-types" / "project:p1:reviewer"
    d.mkdir(parents=True)
    (d / "profile.md").write_text(STRAY_PROFILE)
    return d


def _canonical_dir(data_dir: Path) -> Path:
    return data_dir / "vault" / "projects" / "p1" / "agent-types" / "reviewer"


def test_relocate_moves_stray_directory_when_canonical_is_absent(tmp_path):
    """The plain arm: no conflict, the whole directory is moved."""
    stray = _stray_dir(tmp_path)

    result = relocate_stray_scoped_profiles(str(tmp_path))

    assert result["success"] is True
    assert result["relocated"] == 1
    assert result["skipped"] == 0
    assert not stray.exists()
    assert (_canonical_dir(tmp_path) / "profile.md").read_text() == STRAY_PROFILE


def test_relocate_stray_scoped_profiles_failure_and_merge_arms(tmp_path, monkeypatch):
    """Merge, failure and unparseable-name arms of the destructive migration."""
    # ---- (a) merge arm: the canonical file exists with different content ----
    stray = _stray_dir(tmp_path)
    canonical = _canonical_dir(tmp_path)
    canonical.mkdir(parents=True)
    (canonical / "profile.md").write_text(CANONICAL_PROFILE)

    result = relocate_stray_scoped_profiles(str(tmp_path))

    assert result["relocated"] == 1
    assert result["skipped"] == 0
    # The canonical file wins; the stray copy is preserved beside it.
    assert (canonical / "profile.md").read_text() == CANONICAL_PROFILE
    assert (canonical / "profile.md.stray.bak").read_text() == STRAY_PROFILE
    assert not stray.exists()

    # Idempotence: a second run over the post-(a) tree relocates nothing.
    again = relocate_stray_scoped_profiles(str(tmp_path))
    assert again == {"success": True, "relocated": 0, "skipped": 0, "details": []}
    assert (canonical / "profile.md").read_text() == CANONICAL_PROFILE
    assert (canonical / "profile.md.stray.bak").read_text() == STRAY_PROFILE

    # ---- (b) failure arm: os.replace raises mid-migration ----
    fail_dir = tmp_path / "fail"
    stray2 = _stray_dir(fail_dir)
    before = {p.relative_to(fail_dir): p.read_bytes() for p in stray2.rglob("*") if p.is_file()}

    def boom(*_args, **_kwargs):
        raise OSError("device is full")

    monkeypatch.setattr(os, "replace", boom)
    failed = relocate_stray_scoped_profiles(str(fail_dir))
    monkeypatch.undo()

    # The call reports rather than raising, and counts the failure.
    assert failed["relocated"] == 0
    assert failed["skipped"] == 1
    assert any(d.strip().startswith("ERROR") and "device is full" in d for d in failed["details"])
    # Nothing was partially deleted: the stray tree is byte-for-byte intact...
    assert {
        p.relative_to(fail_dir): p.read_bytes() for p in stray2.rglob("*") if p.is_file()
    } == before
    # ...and the canonical tree was never created.
    assert not _canonical_dir(fail_dir).exists()

    # ---- (c) a colon-encoded name that does not parse is left in place ----
    weird_root = tmp_path / "weird"
    weird = weird_root / "vault" / "agent-types" / "weird:dir"
    weird.mkdir(parents=True)
    (weird / "profile.md").write_text(STRAY_PROFILE)

    skipped = relocate_stray_scoped_profiles(str(weird_root))

    assert skipped["relocated"] == 0
    assert skipped["skipped"] == 1
    assert (weird / "profile.md").read_text() == STRAY_PROFILE
    assert any("SKIP weird:dir" in d for d in skipped["details"])


def test_relocate_is_a_noop_without_an_agent_types_directory(tmp_path):
    """An install with no vault yet must not be an error."""
    assert relocate_stray_scoped_profiles(str(tmp_path)) == {
        "success": True,
        "relocated": 0,
        "skipped": 0,
        "details": [],
    }


@pytest.mark.parametrize("name", ["supervisor", "coding"])
def test_plain_system_profile_directories_are_untouched(tmp_path, name):
    """Only colon-encoded directories are candidates."""
    d = tmp_path / "vault" / "agent-types" / name
    d.mkdir(parents=True)
    (d / "profile.md").write_text(STRAY_PROFILE)

    result = relocate_stray_scoped_profiles(str(tmp_path))

    assert result["relocated"] == 0
    assert result["skipped"] == 0
    assert (d / "profile.md").exists()


def test_stray_file_identical_to_canonical_is_dropped_not_backed_up(tmp_path):
    """An identical stray copy is removed outright — no .bak clutter."""
    stray = _stray_dir(tmp_path)
    canonical = _canonical_dir(tmp_path)
    canonical.mkdir(parents=True)
    (canonical / "profile.md").write_text(STRAY_PROFILE)

    result = relocate_stray_scoped_profiles(str(tmp_path))

    assert result["relocated"] == 1
    assert not stray.exists()
    assert not (canonical / "profile.md.stray.bak").exists()
    assert sorted(p.name for p in canonical.iterdir()) == ["profile.md"]


def test_relocated_tree_is_readable_by_the_profile_scanner(tmp_path):
    """After relocation the file sits where the scanner actually looks."""
    from src.profiles.sync import _find_profile_files

    _stray_dir(tmp_path)
    vault = str(Path(tmp_path) / "vault")
    assert _find_profile_files(vault) == []  # stray layout is refused

    relocate_stray_scoped_profiles(str(tmp_path))

    assert [rel for _abs, rel in _find_profile_files(vault)] == [
        "projects/p1/agent-types/reviewer/profile.md"
    ]
