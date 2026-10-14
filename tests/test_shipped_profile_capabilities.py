"""Package 0 T-10 — every shipped profile declares explicit capabilities.

The eleven profiles under ``src/profiles/defaults/`` are what a fresh install
runs, so they are the fleet Package 6 must not have to migrate later. Each
one now carries a ``## Capabilities`` block derived from its old ``## Tools``
list plus the AQ commands its ``## Role`` prose actually calls — the
invariant the ``<!-- tools-rationale -->`` comment in ``reviewer/profile.md``
already states ("Every command named in the Role section above appears in
this list"), now mechanically checkable.
"""

from __future__ import annotations

import pathlib
import runpy

import pytest

from src.api.scope import (
    AGENT_COMMAND_SET,
    _PLAYBOOK_COMPILER_COMMANDS,
    _TRIAGE_COMMANDS,
)
from src.commands.operator_authority import OPERATOR_READS, OPERATOR_WRITES
from src.profiles.capabilities import (
    HARNESS_TOOL_NAMES,
    NAMESPACES,
    WILDCARD_CHARS,
    CapabilityPolicy,
)
from src.profiles.parser import parse_profile
from src.tools.registry import _builtin_command_names

DEFAULTS_DIR = pathlib.Path(__file__).parent.parent / "src" / "profiles" / "defaults"
PROFILE_IDS = sorted(p.name for p in DEFAULTS_DIR.iterdir() if (p / "profile.md").is_file())

#: §1.5 — commands a shipped profile names that its own session token cannot
#: dispatch, because ``check_command_scope`` answers before the command is
#: ever reached. Elevated supervisors bypass that worker-scope command set,
#: so their explicit capability list is reachable and contributes no entries.
#: The remaining entries are a **pre-existing** gap that Package 0 surfaces
#: rather than repairs: closing it by adding names to ``AGENT_COMMAND_SET``
#: would widen a server-owned allowlist, which is precisely what this package
#: exists to prevent.
#:
#: Pinned as a literal so the set cannot grow silently — a profile that gains
#: a new unreachable name fails this test, forcing a deliberate decision.
#: Resolutions are filed for Package 1 in the child plan §14.
EXPECTED_UNREACHABLE: dict[str, set[str]] = {
    "final-reviewer": {"pr_merge", "reopen_with_feedback"},
    "operator-claude": set(),
    "operator-codex": set(),
    "planner": set(),
    "playbook-compiler": set(),
    "pr-merger": {"pr_merge"},
    "reviewer": {"reopen_with_feedback"},
    "spec-ingest": {"get_downstream_tasks", "task_batch_commit", "task_batch_propose"},
    "supervisor": set(),
    "triage": {"edit_task"},
    "worker-claude": {"pr_merge"},
    "worker-codex": {"pr_merge"},
    "worker-opencode": {"pr_merge"},
}

REACHABLE = AGENT_COMMAND_SET | _TRIAGE_COMMANDS | _PLAYBOOK_COMPILER_COMMANDS
#: An operator template's setup grants are dispatched through ``operator_execute``
#: under a request-local grant, never by its session token directly.
OPERATOR_REACHABLE = REACHABLE | OPERATOR_READS | OPERATOR_WRITES


def _parsed(profile_id: str):
    return parse_profile((DEFAULTS_DIR / profile_id / "profile.md").read_text(encoding="utf-8"))


def test_every_shipped_profile_is_covered():
    """The pin lists exactly the shipped profiles — no drift either way."""
    assert set(PROFILE_IDS) == set(EXPECTED_UNREACHABLE)
    assert len(PROFILE_IDS) == 13


@pytest.mark.parametrize("profile_id", [pid for pid in PROFILE_IDS if pid.startswith("worker-")])
def test_every_worker_template_can_publish_and_shares_the_worker_grants(profile_id):
    parsed = _parsed(profile_id)
    assert parsed.frontmatter.template is True
    policy = CapabilityPolicy.from_namespaces(**parsed.capabilities)
    assert policy.allows_plugin_tool("git_push")
    assert policy.allows_plugin_tool("git_create_pr")
    assert parsed.capabilities == _parsed("worker-codex").capabilities


def test_worker_grant_generator_repairs_drift_without_changing_harness_or_prose(tmp_path):
    generate = runpy.run_path(
        str(DEFAULTS_DIR.parents[2] / "scripts" / "generate-worker-capabilities.py")
    )["generate"]
    source = tmp_path / "worker-codex" / "profile.md"
    source.parent.mkdir()
    source.write_text((DEFAULTS_DIR / "worker-codex" / "profile.md").read_text())
    target = tmp_path / "worker-opencode" / "profile.md"
    target.parent.mkdir()
    original = (DEFAULTS_DIR / "worker-opencode" / "profile.md").read_text()
    stale = original.replace('    "git_push",\n', '').replace('    "job_submit",\n', '')
    target.write_text(stale)

    assert generate(tmp_path, check=True) == [target]
    assert target.read_text() == stale
    assert generate(tmp_path) == [target]
    assert target.read_text() == original
    assert generate(tmp_path, check=True) == []


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
class TestShippedProfile:
    def test_parses_without_errors(self, profile_id):
        parsed = _parsed(profile_id)
        assert parsed.errors == []

    def test_declares_explicit_capabilities(self, profile_id):
        parsed = _parsed(profile_id)
        assert parsed.capabilities is not None, "profile still on the legacy ## Tools shape"
        assert set(parsed.capabilities) == set(NAMESPACES)

    def test_legacy_tools_block_is_gone(self, profile_id):
        text = (DEFAULTS_DIR / profile_id / "profile.md").read_text(encoding="utf-8")
        assert "\n## Tools" not in text

    def test_no_wildcards(self, profile_id):
        parsed = _parsed(profile_id)
        for ns in NAMESPACES:
            for name in parsed.capabilities[ns]:
                assert not any(ch in name for ch in WILDCARD_CHARS), name

    def test_harness_tools_are_non_empty_and_include_bash(self, profile_id):
        parsed = _parsed(profile_id)
        harness = parsed.capabilities["harness_tools"]
        assert harness, "a session with no harness tools cannot reach the aq CLI"
        assert "Bash" in harness
        assert set(harness) <= HARNESS_TOOL_NAMES

    def test_every_aq_command_is_a_real_command(self, profile_id):
        """Every grant names a handler or an explicitly defined read scope."""
        parsed = _parsed(profile_id)
        from src.profiles.capabilities import AQ_FEATURE_CAPABILITIES

        unknown = sorted(set(parsed.capabilities["aq_commands"]) -
                         (_builtin_command_names() | AQ_FEATURE_CAPABILITIES))
        assert unknown == []

    def test_policy_is_constructible_and_not_legacy(self, profile_id):
        parsed = _parsed(profile_id)
        policy = CapabilityPolicy.from_namespaces(**parsed.capabilities)
        assert policy.derived_from_legacy is False
        assert policy.fingerprint().startswith("sha256:")

    def test_read_only_profiles_declare_no_write_tools(self, profile_id):
        parsed = _parsed(profile_id)
        if not parsed.config.get("read_only"):
            pytest.skip("not a read_only profile")
        forbidden = {"Write", "Edit", "NotebookEdit"}
        assert forbidden.isdisjoint(parsed.capabilities["harness_tools"])

    def test_unreachable_command_report_matches_the_pin(self, profile_id):
        """§1.5 report — surfaced, not failed, but pinned so it cannot grow."""
        parsed = _parsed(profile_id)
        granted = set(parsed.capabilities["aq_commands"])
        reachable = OPERATOR_REACHABLE if "operator_execute" in granted else REACHABLE
        unreachable = set() if profile_id == "supervisor" else granted - reachable
        assert unreachable == EXPECTED_UNREACHABLE[profile_id]






def test_supervisor_inbox_grants_exclude_internal_intake():
    granted = set(_parsed("supervisor").capabilities["aq_commands"])
    assert {
        "supervisor_inbox_history",
        "supervisor_inbox_reply",
        "supervisor_inbox_status",
    } <= granted
    assert "supervisor_inbox_post" not in granted
    from src.profiles.capabilities import AQ_FEATURE_CAPABILITIES

    assert granted <= _builtin_command_names() | AQ_FEATURE_CAPABILITIES


# ---------------------------------------------------------------------------
# Emergent-work prime section vs. the profile-owned capability gate
# ---------------------------------------------------------------------------

#: Shipped profiles that deliberately cannot create tasks directly. Prime must
#: therefore *not* render its Emergent work section for them — see
#: ``src/prime/sections.py:profile_allows_create_task``.
NO_CREATE_TASK: set[str] = {"spec-ingest", "pr-merger"}


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_emergent_work_prime_matches_the_create_task_capability(profile_id):
    """Either the section is absent from the profile's prime, or it can file.

    The emergent-work section (``src/prime/templates/emergent_work.md``) tells
    the session to run ``aq task create``. ``create_task`` is on the scope
    allowlist, but ``aq_commands`` is a second, profile-owned gate, so a
    profile that omits it would be instructed to file work its own policy
    denies.
    """
    from src.prime.sections import build_completion_protocol_section

    parsed = _parsed(profile_id)
    allows = "create_task" in set(parsed.capabilities["aq_commands"])
    assert allows is (profile_id not in NO_CREATE_TASK)

    body = build_completion_protocol_section("t-1", allow_emergent_work=allows).body
    assert ("## Emergent work" in body) is allows


def test_promotion_controls_keep_operator_approval_and_publish_out_of_agent_grants():
    supervisor = set(_parsed("supervisor").capabilities["aq_commands"])
    assert {"promote_request", "promote_cancel", "promote_status", "promote_list"} <= supervisor
    for profile_id in PROFILE_IDS:
        grants = set(_parsed(profile_id).capabilities["aq_commands"])
        assert "integration_promotion_publish" not in grants
        assert "promote_approve" not in grants
