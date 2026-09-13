"""Retiring the classes this release stopped shipping, on an existing vault.

Seeding is write-if-absent, so an install that already ran keeps every retired
class file and every profile pointing at one.  These cover the two halves of
:mod:`src.profiles.class_retirement`: profiles are repointed at a surviving
class, and the retired files are moved aside rather than deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.profiles.class_retirement import (
    RETIRED_CLASS_REPLACEMENTS,
    repoint_vault_profile_classes,
    retire_orphaned_worker_rungs,
    retire_vault_intelligence_classes,
)
from src.profiles.parser import parse_profile
from src.vault import ensure_default_intelligence_classes


def _write_profile(data_dir: Path, profile_id: str, default_class: str, *, extra: str = "") -> Path:
    path = data_dir / "vault" / "agent-types" / profile_id / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {profile_id}\nname: {profile_id}\ntags: [profile, agent-type]\n---\n\n"
        f"# {profile_id}\n\n## Role\nA worker.\n\n## Config\n```json\n"
        '{\n  "harness": "claude",\n  "lifecycle": "task",\n'
        f'  "default_class": "{default_class}"{extra}\n}}\n```\n',
        encoding="utf-8",
    )
    return path


def _write_class(data_dir: Path, class_id: str, *, customized: bool = False) -> Path:
    path = data_dir / "vault" / "intelligence-classes" / f"{class_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    custom = "customized: true\n" if customized else ""
    path.write_text(
        f"---\nid: {class_id}\nname: {class_id}\n{custom}---\n\n"
        '```json\n{"anthropic": {"model": "claude-opus-5", "thinking": "medium"}}\n```\n',
        encoding="utf-8",
    )
    return path


def test_every_retired_class_maps_to_a_class_that_still_ships(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    shipped = {p.stem for p in (tmp_path / "vault" / "intelligence-classes").glob("*.md")}
    assert set(RETIRED_CLASS_REPLACEMENTS).isdisjoint(shipped)
    assert set(RETIRED_CLASS_REPLACEMENTS.values()) <= shipped


def test_profile_on_a_retired_class_is_repointed_and_still_parses(tmp_path):
    path = _write_profile(tmp_path, "worker-old", "standard-medium")
    changed = repoint_vault_profile_classes(tmp_path / "vault")

    assert changed == [(str(path), "standard-medium", "standard-high")]
    parsed = parse_profile(path.read_text(encoding="utf-8"))
    assert parsed.is_valid, parsed.errors
    assert parsed.config["default_class"] == "standard-high"
    # The rest of the operator's config block survives untouched.
    assert parsed.config["harness"] == "claude"
    assert parsed.config["lifecycle"] == "task"


def test_repointing_is_idempotent_and_leaves_a_surviving_class_alone(tmp_path):
    kept = _write_profile(tmp_path, "worker-new", "deep-high")
    before = kept.read_text(encoding="utf-8")
    _write_profile(tmp_path, "worker-old", "deep-medium")

    first = repoint_vault_profile_classes(tmp_path / "vault")
    assert [row[0] for row in first] == [
        str(tmp_path / "vault" / "agent-types" / "worker-old" / "profile.md")
    ]
    assert kept.read_text(encoding="utf-8") == before
    assert repoint_vault_profile_classes(tmp_path / "vault") == []


def test_retired_class_files_are_moved_aside_not_deleted(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    _write_class(tmp_path, "standard-medium")
    _write_class(tmp_path, "deep-off")
    root = tmp_path / "vault" / "intelligence-classes"
    original = (root / "standard-medium.md").read_text(encoding="utf-8")

    result = retire_vault_intelligence_classes(tmp_path)

    assert set(result.retired_files) == {"standard-medium", "deep-off"}
    assert not (root / "standard-medium.md").exists()
    # The operator's bytes are one rename away, never destroyed.
    assert (root / "standard-medium.md.retired").read_text(encoding="utf-8") == original
    assert (root / "deep-off.md.retired").is_file()
    assert (root / "standard-high.md").is_file()


def test_a_customized_retired_class_is_left_in_place(tmp_path):
    _write_class(tmp_path, "standard-medium", customized=True)
    root = tmp_path / "vault" / "intelligence-classes"

    result = retire_vault_intelligence_classes(tmp_path)

    assert result.retired_files == ()
    assert result.kept_customized == ("standard-medium",)
    assert (root / "standard-medium.md").is_file()


def test_a_class_re_authored_under_a_retired_id_is_not_retired_twice(tmp_path):
    _write_class(tmp_path, "fast-medium")
    root = tmp_path / "vault" / "intelligence-classes"
    assert retire_vault_intelligence_classes(tmp_path).retired_files == ("fast-medium",)

    _write_class(tmp_path, "fast-medium")
    second = retire_vault_intelligence_classes(tmp_path)

    assert second.retired_files == ()
    assert (root / "fast-medium.md").is_file()
    record = json.loads((root / ".retired-classes").read_text(encoding="utf-8"))
    assert record["retired"] == ["fast-medium"]


def test_profiles_are_repointed_before_their_class_file_moves(tmp_path):
    """A half-finished pass must never leave a profile naming a vanished class."""
    profile = _write_profile(tmp_path, "worker-old", "fast-off")
    _write_class(tmp_path, "fast-off")

    result = retire_vault_intelligence_classes(tmp_path)

    assert result.retired_files == ("fast-off",)
    assert [(row[1], row[2]) for row in result.repointed_profiles] == [("fast-off", "fast-low")]
    assert '"default_class": "fast-low"' in profile.read_text(encoding="utf-8")


def test_missing_vault_is_not_an_error(tmp_path):
    assert retire_vault_intelligence_classes(tmp_path / "nothing-here").retired_files == ()


# ---------------------------------------------------------------------------
# Derived worker rungs whose class has gone (src/profiles/catalog.py)
# ---------------------------------------------------------------------------


def _write_rung(data_dir: Path, profile_id: str, class_id: str, *, extends="worker-claude") -> Path:
    path = data_dir / "vault" / "agent-types" / profile_id / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"extends: {extends}\n" if extends else ""
    path.write_text(
        f"---\nid: {profile_id}\nname: {profile_id}\n{frontmatter}---\n\n"
        f"# {profile_id}\n\n## Config\n```json\n"
        f'{{"default_class": "{class_id}", "lifecycle": "task"}}\n```\n',
        encoding="utf-8",
    )
    return path


def test_a_rung_whose_class_is_gone_is_disabled_not_deleted(tmp_path):
    """It can own a live session and an in-flight task; deleting orphans both."""
    ensure_default_intelligence_classes(str(tmp_path))
    gone = _write_rung(tmp_path, "standard-medium-claude", "standard-medium")
    kept = _write_rung(tmp_path, "deep-high-claude", "deep-high")

    disabled = retire_orphaned_worker_rungs(tmp_path)

    assert [row[1] for row in disabled] == ["standard-medium"]
    assert gone.is_file()
    assert parse_profile(gone.read_text(encoding="utf-8")).config["enabled"] is False
    assert "enabled" not in parse_profile(kept.read_text(encoding="utf-8")).config


def test_disabling_an_orphaned_rung_is_idempotent_and_re_disables_a_re_enable(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    path = _write_rung(tmp_path, "standard-medium-claude", "standard-medium")
    assert retire_orphaned_worker_rungs(tmp_path)

    assert retire_orphaned_worker_rungs(tmp_path) == []

    # Turning it back on does not make its class exist: a rung that cannot
    # resolve a model would only quarantine on launch, so it goes back off.
    path.write_text(
        path.read_text(encoding="utf-8").replace('"enabled": false', '"enabled": true'),
        encoding="utf-8",
    )
    assert [row[1] for row in retire_orphaned_worker_rungs(tmp_path)] == ["standard-medium"]


def test_an_authored_profile_naming_a_vanished_class_is_left_alone(tmp_path):
    """Only derived rungs are ours to disable."""
    ensure_default_intelligence_classes(str(tmp_path))
    authored = _write_rung(tmp_path, "house-worker", "standard-medium", extends=None)

    assert retire_orphaned_worker_rungs(tmp_path) == []
    assert "enabled" not in parse_profile(authored.read_text(encoding="utf-8")).config


def test_an_empty_class_vault_disables_nothing(tmp_path):
    """No classes loaded is a broken vault, not proof every class was retired."""
    _write_rung(tmp_path, "standard-high-claude", "standard-high")

    assert retire_orphaned_worker_rungs(tmp_path) == []


def test_a_profile_whose_id_names_the_retired_class_is_disabled_not_repointed(tmp_path):
    """``standard-medium-claude`` must not quietly become a standard-high worker."""
    ensure_default_intelligence_classes(str(tmp_path))
    named = _write_rung(tmp_path, "standard-medium-claude", "standard-medium", extends=None)
    plain = _write_profile(tmp_path, "house-worker", "standard-medium")

    result = retire_vault_intelligence_classes(tmp_path)

    # The id says what it is for, and that no longer exists.
    named_config = parse_profile(named.read_text(encoding="utf-8")).config
    assert named_config["default_class"] == "standard-medium"
    assert named_config["enabled"] is False
    # A profile that merely uses the class is repointed as before.
    assert [(row[1], row[2]) for row in result.repointed_profiles] == [
        ("standard-medium", "standard-high")
    ]
    assert parse_profile(plain.read_text(encoding="utf-8")).config["default_class"] == (
        "standard-high"
    )
