"""Task 8 (document review plan): worker profile rules carry the review workflow.

Every default worker profile whose ``aq_commands`` grants ``review_submit``
must also carry the "specs and plans go to review, not the repo" rule with
its `do not commit` wording.  The supervisor profile must carry the
decide/delegation rule.
"""

from __future__ import annotations

from pathlib import Path

from src.profiles.parser import parse_profile

DEFAULTS_DIR = Path("src") / "profiles" / "defaults"
SUPERVISOR = "supervisor"


def _parsed(profile_id: str):
    text = (DEFAULTS_DIR / profile_id / "profile.md").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.is_valid, f"{profile_id} parse errors: {parsed.errors}"
    return parsed


def test_all_default_profiles_exist():
    ids = {p.name for p in DEFAULTS_DIR.iterdir() if (p / "profile.md").is_file()}
    assert {"supervisor", "worker-claude", "worker-codex"}.issubset(ids)


def test_worker_profiles_carry_the_review_workflow_rule():
    checked = []
    for profile_dir in sorted(DEFAULTS_DIR.iterdir()):
        if profile_dir.name == SUPERVISOR:
            continue
        if not (profile_dir / "profile.md").is_file():
            continue
        parsed = _parsed(profile_dir.name)
        if not parsed.rules:
            continue
        aq_commands = (parsed.capabilities or {}).get("aq_commands", [])
        if "review_submit" not in aq_commands:
            continue
        checked.append(profile_dir.name)
        rules = parsed.rules
        assert "aq review submit" in rules, (
            f"{profile_dir.name}: rules missing `aq review submit`"
        )
        assert "do not commit" in rules, (
            f"{profile_dir.name}: rules missing `do not commit`"
        )
    assert checked, "no default worker profile grants review_submit — nothing tested"


def test_supervisor_profile_carry_the_decide_rule():
    rules = _parsed(SUPERVISOR).rules
    assert "aq review decide" in rules
    assert "delegated" in rules
