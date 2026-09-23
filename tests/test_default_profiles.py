"""Tests for shipped default profiles seeding (supervisor-agent spec §4, §9).

Fresh installs must seed ``supervisor``, ``planner``, and ``reviewer``
profiles into ``vault/agent-types/<id>/profile.md`` via a write-if-absent
mechanism that mirrors ``ensure_default_harnesses`` — operator edits
survive reseed on next startup.
"""

from __future__ import annotations

from pathlib import Path

from src.profiles.capabilities import CapabilityPolicy
from src.profiles.parser import parse_profile
from src.vault import ensure_default_profiles, ensure_vault_layout

SHIPPED_PROFILE_IDS = ("supervisor", "planner", "reviewer", "final-reviewer")
#: One generic worker per harness.  The tier x level x provider ladder was
#: retired: a task's ``intelligence_class`` picks the model and reasoning level
#: for the run, so profiles that differ only in ``default_class`` bought nothing.
WORKER_PROFILE_IDS = ("worker-claude", "worker-codex")


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


def test_seeded_worker_profiles_require_material_progress_notes(tmp_path):
    """Shipped worker instructions must make in-progress enrichment explicit."""
    ensure_default_profiles(str(tmp_path))

    for profile_id in WORKER_PROFILE_IDS:
        text = _vault_profile_path(tmp_path, profile_id).read_text(encoding="utf-8")
        parsed = parse_profile(text)
        prompt = f"{parsed.role}\n{parsed.rules}".lower()
        assert "aq task comment" in prompt, profile_id
        assert "material findings" in prompt, profile_id
        assert "decisions" in prompt, profile_id
        assert "while working" in prompt, profile_id


def test_seeded_worker_profiles_carry_test_scope_and_baseline_policy(tmp_path):
    """Every runnable worker inherits bounded checks and baseline handling."""
    ensure_default_profiles(str(tmp_path))

    for profile_id in WORKER_PROFILE_IDS:
        text = _vault_profile_path(tmp_path, profile_id).read_text(encoding="utf-8")
        parsed = parse_profile(text)
        assert parsed.is_valid, (profile_id, parsed.errors)
        rules = " ".join(parsed.rules.lower().split())
        for required in (
            "aq test",
            "focused tests",
            "area suite",
            "full-suite runs belong to ci",
            "recorded known-failing baseline",
            "never capture your own baseline",
            "pre-existing failure does not fail your task",
            "weakening or skipping a test",
            "when authoring a task",
        ):
            assert required in rules, (profile_id, required)


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


def test_seeded_supervisor_profile_carries_operating_rules(tmp_path):
    """A fresh install gets the supervisor's patrol and concrete stall repairs."""
    ensure_default_profiles(str(tmp_path))
    profile = parse_profile(
        _vault_profile_path(tmp_path, "supervisor").read_text(encoding="utf-8")
    )
    assert profile.is_valid, profile.errors
    rules = " ".join(profile.rules.split())

    for instruction in (
        "First action on a cold start: establish the patrol",
        "harness's scheduled jobs",
        "about every 15 minutes, off the :00 and :30 marks",
        "stall sweep",
        "aq --json message inbox --inject",
        "profile:supervisor",
        "session:<your supervisor session id>",
        "fixes",
        "Never create a second patrol",
        "Re-establish it after every session restart",
        "scheduler job is not",
        "run the sweep at the start of every turn",
        "never hand the human a command to run",
        "Retry up to three times",
        "equivalent authorized route",
        "Confirm first only for",
        "aq integration abort",
        "aq integration cancel-preserving",
        "aq integration waive-history",
        "aq agent delete",
        "destroying work that cannot be recovered",
        "publishing outside the user's own repositories",
    ):
        assert instruction in rules, instruction

    for repair in (
        "Reroute to an eligible pool with live sessions",
        "Remove that satisfied dependency edge",
        "recover-child sweep",
        "deploy a newer fix",
        "Answer the prompt",
        "Reopen it with concrete feedback",
    ):
        assert repair in rules, repair

    assert "~/.agent-queue/operator-checks/" not in rules
    assert "supervisor-agent-queue" not in rules


def test_seeded_supervisor_can_use_advertised_worker_message_surface(tmp_path):
    """Supervisor capability policy grants the worker messaging commands it documents."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "supervisor").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.capabilities is not None
    policy = CapabilityPolicy.from_namespaces(**parsed.capabilities)

    assert policy.allows_aq_command("agent_message")
    assert policy.allows_aq_command("message_status")
    for command in (
        "explain_task", "integration_status", "pool_status", "session_list",
        "session_logs", "session_peek",
    ):
        assert policy.allows_aq_command(command)


def test_seeded_workers_have_publication_grants_without_supervisor_grants(tmp_path):
    """Supervisors route workers; they do not need the workers' Git permissions."""
    ensure_default_profiles(str(tmp_path))

    def policy_for(profile_id):
        parsed = parse_profile(_vault_profile_path(tmp_path, profile_id).read_text(encoding="utf-8"))
        assert parsed.capabilities is not None
        return CapabilityPolicy.from_namespaces(**parsed.capabilities)

    supervisor = policy_for("supervisor")
    for command in ("git_create_pr", "git_push"):
        assert not supervisor.allows_plugin_tool(command)
    for profile_id in WORKER_PROFILE_IDS:
        worker = policy_for(profile_id)
        for command in ("git_create_pr", "git_push"):
            assert worker.allows_plugin_tool(command), (profile_id, command)


def test_seeded_planner_profile_is_task_lifecycle(tmp_path):
    """Planner ships as a task-lifecycle profile."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "planner").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("lifecycle") == "task"
    assert parsed.config.get("harness") == "claude"


def test_seeded_reviewer_profile_is_task_lifecycle(tmp_path):
    """Reviewer ships with the claude harness and a read-only workspace."""
    ensure_default_profiles(str(tmp_path))
    text = _vault_profile_path(tmp_path, "reviewer").read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config.get("harness") == "claude"
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
    tools = (parsed.capabilities or {}).get("aq_commands", [])
    assert "pr_merge" not in tools, "reviewer must not have merge authority"
    assert {
        "get_task",
        "task_show",
        "task_comments",
        "task_heartbeat",
        "task_close",
        "reopen_with_feedback",
    } <= set(tools)
    assert "task_comment" not in tools
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is True


def test_final_reviewer_profile_has_merge_authority():
    """Final-reviewer profile parses cleanly and must have pr_merge in allowed tools."""
    from pathlib import Path

    src = Path("src/profiles/defaults/final-reviewer/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "final-reviewer"
    tools = (parsed.capabilities or {}).get("aq_commands", [])
    assert "pr_merge" in tools, "final-reviewer must have merge authority"
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is True


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
    tools = (parsed.capabilities or {}).get("aq_commands", [])
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
    assert parsed.config.get("harness") == "claude"
    assert parsed.config.get("needs_workspace") is False
    tools = (parsed.capabilities or {}).get("aq_commands", [])
    assert "task_batch_propose" in tools
    assert "list_tasks" in tools
    assert "get_downstream_tasks" in tools
    assert "create_task" not in tools, "spec-ingest must not create tasks directly"


def test_playbook_compiler_profile_shape():
    """playbook-compiler is a workspace-free mechanical profile whitelisting
    the V2 propose/validate loop."""
    from pathlib import Path

    src = Path("src/profiles/defaults/playbook-compiler/profile.md").read_text()
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "playbook-compiler"
    assert parsed.config.get("harness") == "claude"
    assert parsed.config.get("needs_workspace") is False
    tools = (parsed.capabilities or {}).get("aq_commands", [])
    assert "playbook_v2_validate" in tools
    assert "playbook_v2_propose" in tools
    assert "playbook_activate" not in tools


# ---------------------------------------------------------------------------
# Reviewer protocol: a task that produced no code can never have a PR
# ---------------------------------------------------------------------------


def test_reviewer_profile_does_not_reject_solely_for_a_missing_pr():
    """The reviewer must not demand a PR from a task that left no commits.

    Reviews, plans, ``no-op`` closes and empty branches have nothing to
    push, so "reject: open a PR first" reopens them into a dead end and
    the reopen → re-review loop is what grew the ``Review: Review: ...``
    chains (task solid-bridge-31).
    """
    src = Path("src/profiles/defaults/reviewer/profile.md").read_text(encoding="utf-8")
    parsed = parse_profile(src)
    assert parsed.is_valid, parsed.errors
    prompt = f"{parsed.role}\n{parsed.rules}".lower()

    assert "open a pr first" not in prompt
    assert "a missing pr is not" in prompt
    assert "no commits ahead of its base" in prompt
    # The reviewer is told how to tell the two cases apart with tools it has.
    assert "task_show" in prompt
    assert "task_comments" in prompt
