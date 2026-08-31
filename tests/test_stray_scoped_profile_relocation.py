"""Tests for the legacy colon-encoded scoped-profile vault layout.

An older ``_vault_profile_path`` used the raw profile id as a directory
name, so project-scoped profiles landed in the *system* folder as
``vault/agent-types/project:<pid>:<type>/profile.md``.  The profile
scanner refuses that layout (``is_invalid_scoped_flat_path``), which left
the DB row live while its markdown source went inert — and warned on
every startup scan.

Covers:
- ``_vault_profile_path`` routes scoped ids to the project layout
- ``relocate_stray_scoped_profiles`` moves stray dirs to the canonical path
- Conflict handling keeps the canonical file and preserves a differing stray
- Idempotency and no-op behaviour
- The relocated files are accepted by the profile scanner
"""

from __future__ import annotations

import os
from pathlib import Path

from src.profiles.migration import (
    _vault_profile_path,
    relocate_stray_scoped_profiles,
    split_scoped_profile_id,
)
from src.profiles.sync import derive_profile_id, is_invalid_scoped_flat_path

PID = "moss-and-spade-business-logic"
SCOPED_ID = f"project:{PID}:email-triager"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stray_dir(data_dir: Path, profile_id: str) -> Path:
    return data_dir / "vault" / "agent-types" / profile_id


def _canonical_dir(data_dir: Path, pid: str, agent_type: str) -> Path:
    return data_dir / "vault" / "projects" / pid / "agent-types" / agent_type


class TestSplitScopedProfileId:
    def test_splits_scoped_id(self):
        assert split_scoped_profile_id(SCOPED_ID) == (PID, "email-triager")

    def test_plain_id_returns_none(self):
        assert split_scoped_profile_id("supervisor") is None

    def test_malformed_scoped_id_returns_none(self):
        assert split_scoped_profile_id("project::email-triager") is None
        assert split_scoped_profile_id("project:only-two") is None


class TestVaultProfilePath:
    def test_system_profile_uses_agent_types_folder(self, tmp_path):
        path = _vault_profile_path(str(tmp_path), "supervisor")
        assert path == os.path.join(
            str(tmp_path), "vault", "agent-types", "supervisor", "profile.md"
        )

    def test_scoped_profile_uses_project_folder(self, tmp_path):
        path = _vault_profile_path(str(tmp_path), SCOPED_ID)
        assert path == os.path.join(
            str(tmp_path),
            "vault",
            "projects",
            PID,
            "agent-types",
            "email-triager",
            "profile.md",
        )

    def test_scoped_path_is_accepted_by_the_scanner(self, tmp_path):
        """The regression guard: the generated path must not be a 'stray'."""
        path = _vault_profile_path(str(tmp_path), SCOPED_ID)
        rel = os.path.relpath(path, os.path.join(str(tmp_path), "vault")).replace(os.sep, "/")
        assert not is_invalid_scoped_flat_path(rel)
        assert derive_profile_id(rel) == SCOPED_ID


class TestRelocateStrayScopedProfiles:
    def test_moves_whole_directory_when_target_absent(self, tmp_path):
        stray = _stray_dir(tmp_path, SCOPED_ID)
        _write(stray / "profile.md", "---\nid: " + SCOPED_ID + "\n---\n\n## Role\nHi\n")
        _write(stray / "memory" / "memory.md", "notes\n")

        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["success"] is True
        assert report["relocated"] == 1
        assert not stray.exists()

        target = _canonical_dir(tmp_path, PID, "email-triager")
        assert (target / "profile.md").read_text().startswith("---")
        assert (target / "memory" / "memory.md").read_text() == "notes\n"

    def test_identical_duplicate_is_dropped(self, tmp_path):
        body = "---\nid: " + SCOPED_ID + "\n---\n\n## Role\nSame\n"
        stray = _stray_dir(tmp_path, SCOPED_ID)
        target = _canonical_dir(tmp_path, PID, "email-triager")
        _write(stray / "profile.md", body)
        _write(target / "profile.md", body)

        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["relocated"] == 1
        assert not stray.exists()
        assert (target / "profile.md").read_text() == body
        assert not (target / "profile.md.stray.bak").exists()

    def test_canonical_file_wins_and_stray_is_preserved(self, tmp_path):
        stray = _stray_dir(tmp_path, SCOPED_ID)
        target = _canonical_dir(tmp_path, PID, "email-triager")
        _write(stray / "profile.md", "STRAY")
        _write(target / "profile.md", "CANONICAL")

        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["relocated"] == 1
        assert not stray.exists()
        # Canonical content is untouched; the differing stray is kept alongside.
        assert (target / "profile.md").read_text() == "CANONICAL"
        assert (target / "profile.md.stray.bak").read_text() == "STRAY"

    def test_non_conflicting_files_are_merged_into_the_canonical_tree(self, tmp_path):
        stray = _stray_dir(tmp_path, SCOPED_ID)
        target = _canonical_dir(tmp_path, PID, "email-triager")
        _write(stray / "profile.md", "SAME")
        _write(stray / "memory" / "insights" / "one.md", "insight\n")
        _write(target / "profile.md", "SAME")

        relocate_stray_scoped_profiles(str(tmp_path))

        assert not stray.exists()
        assert (target / "memory" / "insights" / "one.md").read_text() == "insight\n"

    def test_is_idempotent(self, tmp_path):
        stray = _stray_dir(tmp_path, SCOPED_ID)
        _write(stray / "profile.md", "x")

        first = relocate_stray_scoped_profiles(str(tmp_path))
        second = relocate_stray_scoped_profiles(str(tmp_path))

        assert first["relocated"] == 1
        assert second["relocated"] == 0
        assert second["success"] is True

    def test_leaves_system_profiles_alone(self, tmp_path):
        plain = _stray_dir(tmp_path, "supervisor")
        _write(plain / "profile.md", "---\nid: supervisor\n---\n")

        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["relocated"] == 0
        assert (plain / "profile.md").exists()

    def test_unparseable_colon_dir_is_skipped_not_moved(self, tmp_path):
        weird = _stray_dir(tmp_path, "not:a:scoped:id:really")
        _write(weird / "profile.md", "x")

        # ``not:a:...`` does not start with ``project:`` so it cannot be routed.
        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["relocated"] == 0
        assert report["skipped"] == 1
        assert report["success"] is False
        assert weird.exists()

    def test_partial_relocation_is_not_reported_as_success(self, tmp_path):
        """Every candidate must move before the migration reports success."""
        stray = _stray_dir(tmp_path, SCOPED_ID)
        _write(stray / "profile.md", "movable")
        weird = _stray_dir(tmp_path, "not:a:scoped:id:really")
        _write(weird / "profile.md", "manual review required")

        report = relocate_stray_scoped_profiles(str(tmp_path))

        assert report["relocated"] == 1
        assert report["skipped"] == 1
        assert report["success"] is False
        assert not stray.exists()
        assert weird.exists()

    def test_missing_vault_is_a_noop(self, tmp_path):
        report = relocate_stray_scoped_profiles(str(tmp_path))
        assert report == {"success": True, "relocated": 0, "skipped": 0, "details": []}
