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


SHIPPED_PIPELINE = (
    Path(__file__).parent.parent
    / "src" / "prompts" / "default_playbooks" / "default-pipeline.md"
)

#: The pre-Package-6 source, kept so the V1 behaviour it encodes stays asserted.
FROZEN_V1_PIPELINE = (
    Path(__file__).parent / "fixtures" / "playbooks" / "v1" / "default-pipeline.md"
)

PIPELINE_RULE_IDS = {
    "per-task-review",
    "per-branch-final-review",
    "spec-ingest-on-approve",
    "proposal-ready-gate",
    "commit-on-gate-resolve",
}


def test_shipped_pipeline_has_no_embedded_action_block():
    """Package 6: the shipped source is prose, so the V1 compiler cannot use it.

    This is the intended end state, not a regression.  V2 compiles the prose to
    a reviewed artifact (`tests/fixtures/playbooks/v2/default-pipeline/`); the
    V1 compiler has nothing left to read, and says so rather than silently
    producing an empty pipeline.
    """
    assert SHIPPED_PIPELINE.is_file(), f"Source file missing: {SHIPPED_PIPELINE}"
    result = compile_pipeline(SHIPPED_PIPELINE.read_text(encoding="utf-8"))
    assert not result.success, (
        "the shipped pipeline still compiles under V1, so it still carries an "
        "embedded action graph — see tests/test_shipped_playbook_sources.py"
    )


def test_frozen_v1_pipeline_still_compiles():
    """The frozen V1 graph — the artifact's behavioural baseline — still compiles.

    Every assertion here was previously made against the shipped Markdown.  It
    moved rather than disappeared, because the V1 arm of the shadow-parity
    comparison and the reviewed V2 artifact are both derived from this file.
    """
    assert FROZEN_V1_PIPELINE.is_file(), f"Source file missing: {FROZEN_V1_PIPELINE}"
    r = compile_pipeline(FROZEN_V1_PIPELINE.read_text(encoding="utf-8"))
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
    assert all(
        any(entry.startswith(f"{rule_id}-") for entry in entries)
        for rule_id in PIPELINE_RULE_IDS
    )


def test_reviewed_artifact_keeps_the_pinned_rule_set():
    """The rule set survived the rewrite — asserted where it now lives."""
    import json

    artifact = json.loads(
        (
            Path(__file__).parent
            / "fixtures" / "playbooks" / "v2" / "default-pipeline" / "artifact.json"
        ).read_text(encoding="utf-8")
    )
    assert {rule["id"] for rule in artifact["rules"]} == PIPELINE_RULE_IDS


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
