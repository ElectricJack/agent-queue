"""Tests for shipped default profiles seeding (supervisor-agent spec §4, §9).

Fresh installs must seed ``supervisor``, ``planner``, and ``reviewer``
profiles into ``vault/agent-types/<id>/profile.md`` via a write-if-absent
mechanism that mirrors ``ensure_default_harnesses`` — operator edits
survive reseed on next startup.
"""

from __future__ import annotations

from pathlib import Path

from src.profiles.parser import parse_profile
from src.vault import ensure_default_profiles, ensure_vault_layout


SHIPPED_PROFILE_IDS = ("supervisor", "planner", "reviewer")


def _vault_profile_path(root: Path, profile_id: str) -> Path:
    return root / "vault" / "agent-types" / profile_id / "profile.md"


def test_ensure_default_profiles_seeds_all_three(tmp_path):
    """Fresh vault seeds supervisor, planner, and reviewer profile files."""
    result = ensure_default_profiles(str(tmp_path))

    created = set(result["created"])
    assert set(SHIPPED_PROFILE_IDS).issubset(created), (
        f"expected all three shipped profiles created, got {created}"
    )
    for pid in SHIPPED_PROFILE_IDS:
        path = _vault_profile_path(tmp_path, pid)
        assert path.is_file(), f"missing seeded profile: {path}"


def test_ensure_default_profiles_does_not_clobber_operator_edits(tmp_path):
    """A pre-existing profile.md is left untouched on reseed."""
    # Seed once, then edit the supervisor file to simulate an operator override.
    ensure_default_profiles(str(tmp_path))
    supervisor_path = _vault_profile_path(tmp_path, "supervisor")
    custom = "# Operator override\n\nCustom content.\n"
    supervisor_path.write_text(custom, encoding="utf-8")

    # Reseed — the edited file must remain byte-for-byte identical.
    result = ensure_default_profiles(str(tmp_path))
    assert supervisor_path.read_text(encoding="utf-8") == custom
    assert "supervisor" in result["skipped"], (
        f"expected supervisor in skipped, got {result}"
    )


def test_seeded_profiles_parse_without_errors(tmp_path):
    """Every seeded profile parses cleanly through the profile parser."""
    ensure_default_profiles(str(tmp_path))
    for pid in SHIPPED_PROFILE_IDS:
        text = _vault_profile_path(tmp_path, pid).read_text(encoding="utf-8")
        parsed = parse_profile(text)
        assert parsed.is_valid, f"{pid} parse errors: {parsed.errors}"
        assert parsed.frontmatter.id == pid


def test_seeded_supervisor_profile_has_named_session_config(tmp_path):
    """Supervisor profile carries the named-session config the lens needs."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "supervisor").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("harness") == "claude"
    assert parsed.config.get("lifecycle") == "named"
    assert parsed.config.get("mode") == "on_demand"
    assert parsed.config.get("wake_mode") == "resume"
    assert isinstance(parsed.config.get("idle_timeout"), int)


def test_seeded_planner_profile_is_task_lifecycle(tmp_path):
    """Planner ships as a task-lifecycle profile."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "planner").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("lifecycle") == "task"
    assert parsed.config.get("harness") == "claude"


def test_seeded_reviewer_profile_is_task_lifecycle(tmp_path):
    """Reviewer ships as a task-lifecycle profile."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "reviewer").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("lifecycle") == "task"
    assert parsed.config.get("harness") == "claude"


def test_ensure_vault_layout_seeds_default_profiles(tmp_path):
    """Startup vault layout seeds shipped profiles as a side effect."""
    ensure_vault_layout(str(tmp_path))
    for pid in SHIPPED_PROFILE_IDS:
        assert _vault_profile_path(tmp_path, pid).is_file(), (
            f"ensure_vault_layout missed seeding {pid}"
        )
