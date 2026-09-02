"""Tests for the shipped default pipeline playbook and triage profile.

Task 13: verifies that:
- default-pipeline.md ships via ensure_default_playbooks
- default-pipeline.md compiles cleanly with the real pipeline compiler
- triage/profile.md ships via ensure_default_profiles
- triage profile is parseable by the real profile parser
"""

from __future__ import annotations

from pathlib import Path

from src.playbooks.pipeline_compiler import compile_pipeline
from src.profiles.parser import parse_profile
from src.vault import ensure_default_playbooks, ensure_default_profiles


def test_default_pipeline_ships(tmp_path):
    """ensure_default_playbooks copies default-pipeline.md into the vault."""
    ensure_default_playbooks(str(tmp_path))
    pipeline = Path(tmp_path) / "vault" / "system" / "playbooks" / "default-pipeline.md"
    assert pipeline.is_file(), f"default-pipeline.md not seeded at {pipeline}"


def test_default_pipeline_compiles():
    """The shipped default-pipeline.md must compile with zero errors."""
    src = (
        Path(__file__).parent.parent
        / "src" / "prompts" / "default_playbooks" / "default-pipeline.md"
    )
    assert src.is_file(), f"Source file missing: {src}"
    md = src.read_text(encoding="utf-8")
    r = compile_pipeline(md)
    assert r.success, r.errors
    d = r.playbook.to_dict()
    assert d["kind"] == "pipeline"
    assert d["role"] == "default-pipeline"
    assert "task.created" not in d["triggers"]
    entries = {
        rule["entry"] for rules in d["pipeline_rules"].values() for rule in rules
    }
    assert not any("task-created-routing" in entry for entry in entries)
    assert not any("worker-filed-triage" in entry for entry in entries)
    expected = {
        "per-task-review",
        "per-branch-final-review",
        "spec-ingest-on-approve",
        "proposal-ready-gate",
        "commit-on-gate-resolve",
    }
    assert all(any(entry.startswith(f"{rule_id}-") for entry in entries) for rule_id in expected)


def test_triage_profile_ships(tmp_path):
    """ensure_default_profiles copies triage/profile.md into the vault."""
    ensure_default_profiles(str(tmp_path))
    prof = Path(tmp_path) / "vault" / "agent-types" / "triage" / "profile.md"
    assert prof.is_file(), f"triage profile.md not seeded at {prof}"
    body = prof.read_text(encoding="utf-8")
    assert "task_route" in body, "triage profile must list task_route in allowed tools"
    assert "needs_workspace" in body, "triage profile must mention needs_workspace in config"


def test_triage_profile_parseable():
    """The shipped triage profile.md must parse cleanly via parse_profile."""
    src = (
        Path(__file__).parent.parent
        / "src" / "profiles" / "defaults" / "triage" / "profile.md"
    )
    assert src.is_file(), f"Source file missing: {src}"
    md = src.read_text(encoding="utf-8")
    result = parse_profile(md)
    assert result.frontmatter.id == "triage"
    assert result.config.get("harness") == "claude"
    assert result.config.get("needs_workspace") is False
    # task_route must be in the profile's AQ command namespace.  Playbook V2
    # Package 0 (T-10) replaced the flat ``## Tools`` ``allowed`` list with
    # the three-namespace ``## Capabilities`` block on every shipped profile;
    # routing verbs are AQ commands, not harness tools.
    aq_commands = (result.capabilities or {}).get("aq_commands", [])
    assert "task_route" in aq_commands, f"task_route not in aq_commands: {aq_commands}"
