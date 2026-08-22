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


SHIPPED_PROFILE_IDS = ("supervisor", "planner", "reviewer", "final-reviewer")


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
    """Reviewer ships with claude_sdk runtime and read-only workspace."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "reviewer").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("runtime") == "claude_sdk"
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is True


def test_ensure_vault_layout_seeds_default_profiles(tmp_path):
    """Startup vault layout seeds shipped profiles as a side effect."""
    ensure_vault_layout(str(tmp_path))
    for pid in SHIPPED_PROFILE_IDS:
        assert _vault_profile_path(tmp_path, pid).is_file(), (
            f"ensure_vault_layout missed seeding {pid}"
        )


# ---------------------------------------------------------------------------
# T3: reviewer and final-reviewer dv2-phase2 profiles
# ---------------------------------------------------------------------------


def test_reviewer_profile_parses_and_lacks_merge_authority():
    """Reviewer profile parses cleanly and must not have pr_merge in allowed tools."""
    from pathlib import Path

    src = Path("src/profiles/defaults/reviewer/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "reviewer"
    tools = parsed.tools.get("allowed", [])
    assert "pr_merge" not in tools, "reviewer must not have merge authority"
    assert "reopen_with_feedback" in tools
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is True


def test_final_reviewer_profile_has_merge_authority():
    """Final-reviewer profile parses cleanly and must have pr_merge in allowed tools."""
    from pathlib import Path

    src = Path("src/profiles/defaults/final-reviewer/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "final-reviewer"
    tools = parsed.tools.get("allowed", [])
    assert "pr_merge" in tools, "final-reviewer must have merge authority"
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is False


def test_seeded_final_reviewer_profile_parses_without_errors(tmp_path):
    """Final-reviewer profile seeds and parses cleanly through the vault seeder."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "final-reviewer").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.is_valid, f"final-reviewer parse errors: {parsed.errors}"
    assert parsed.frontmatter.id == "final-reviewer"


def test_reviewer_profile_lacks_merge_authority_after_seeding(tmp_path):
    """Seeded reviewer profile must not have pr_merge (worker profiles must not merge)."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "reviewer").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    tools = parsed.tools.get("allowed", [])
    assert "pr_merge" not in tools, "reviewer must not have merge authority after seeding"


# ---------------------------------------------------------------------------
# Phase 6 Group C: spec-ingest and playbook-compiler profiles
# ---------------------------------------------------------------------------

PHASE6_PROFILE_IDS = ("spec-ingest", "playbook-compiler")


def test_phase6_profiles_are_auto_discovered_and_seeded(tmp_path):
    """ensure_default_profiles discovers every src/profiles/defaults/<id>/
    directory — spec-ingest and playbook-compiler included — with no code
    changes required."""
    result = ensure_default_profiles(str(tmp_path))
    created = set(result["created"])
    assert set(PHASE6_PROFILE_IDS).issubset(created), (
        f"expected spec-ingest + playbook-compiler seeded, got {created}"
    )
    for pid in PHASE6_PROFILE_IDS:
        path = _vault_profile_path(tmp_path, pid)
        assert path.is_file(), f"missing seeded profile: {path}"
        parsed = parse_profile(path.read_text(encoding="utf-8"))
        assert parsed.is_valid, f"{pid} parse errors: {parsed.errors}"
        assert parsed.warnings == [], f"{pid} parse warnings: {parsed.warnings}"
        assert parsed.frontmatter.id == pid


def test_spec_ingest_profile_shape():
    """spec-ingest is a workspace-free planning profile whitelisting the
    task_batch_propose flow, never create_task directly."""
    from pathlib import Path

    src = Path("src/profiles/defaults/spec-ingest/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "spec-ingest"
    assert parsed.config.get("runtime") == "claude_sdk"
    assert parsed.config.get("needs_workspace") is False
    tools = parsed.tools.get("allowed", [])
    assert "task_batch_propose" in tools
    assert "list_tasks" in tools
    assert "get_downstream_tasks" in tools
    assert "create_task" not in tools, "spec-ingest must not create tasks directly"


def test_playbook_compiler_profile_shape():
    """playbook-compiler is a workspace-free mechanical profile whitelisting
    the playbook_validate/playbook_install loop."""
    from pathlib import Path

    src = Path("src/profiles/defaults/playbook-compiler/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "playbook-compiler"
    assert parsed.config.get("runtime") == "claude_sdk"
    assert parsed.config.get("needs_workspace") is False
    tools = parsed.tools.get("allowed", [])
    assert "playbook_validate" in tools
    assert "playbook_install" in tools
