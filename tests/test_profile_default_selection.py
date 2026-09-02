"""Unit tests for src/profiles/default_selection.py.

Guards the fallback that keeps READY tasks from stalling with
"no resolvable profile_id".
"""
from __future__ import annotations

from src.profiles.default_selection import select_default_profile_id


def test_prefers_claude_opus():
    assert select_default_profile_id(
        ["claude-sonnet", "reviewer", "claude-opus", "acp-codex"]
    ) == "claude-opus"


def test_falls_back_to_claude_sonnet():
    assert select_default_profile_id(
        ["claude-sonnet", "reviewer", "acp-codex"]
    ) == "claude-sonnet"


def test_prefers_the_standard_worker_over_the_deep_one():
    """Alphabetical order made ``worker-deep`` the default for every
    unpinned task, which is the most expensive lane, not the sensible one."""
    assert select_default_profile_id(
        ["worker-deep", "worker-fast", "worker-standard", "reviewer"]
    ) == "worker-standard"


def test_falls_back_to_general_purpose_alphabetically():
    """No preferred profile → the alphabetically-first general-purpose
    one, so the choice is stable across ticks."""
    assert select_default_profile_id(
        ["worker-deep", "acp-gemini", "reviewer", "supervisor"]
    ) == "acp-gemini"


def test_special_purpose_only_is_last_resort():
    """Stage-specific profiles are a poor default but beat stalling."""
    assert select_default_profile_id(["triage", "reviewer"]) == "reviewer"


def test_never_picks_supervisor():
    """The supervisor is a daemon-wide singleton, not an agent slot."""
    assert select_default_profile_id(["supervisor"]) is None


def test_never_picks_project_scoped_override():
    """`project:{pid}:{profile_id}` rows are reached via the bare id."""
    assert select_default_profile_id(
        ["project:foo:claude-code", "supervisor"]
    ) is None


def test_project_scoped_override_does_not_mask_a_real_candidate():
    assert select_default_profile_id(
        ["project:foo:claude-code", "worker-fast"]
    ) == "worker-fast"


def test_empty_input_returns_none():
    assert select_default_profile_id([]) is None


def test_ignores_falsy_ids():
    assert select_default_profile_id(["", None, "claude-opus"]) == "claude-opus"


def test_is_deterministic_regardless_of_input_order():
    ids = ["worker-deep", "acp-gemini", "reviewer", "acp-codex"]
    first = select_default_profile_id(ids)
    assert first == select_default_profile_id(list(reversed(ids)))
