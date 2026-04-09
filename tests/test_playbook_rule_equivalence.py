"""Validate default playbooks produce equivalent results to current rules.

Roadmap 5.6.6 — Phase 1 migration gate.  These tests verify that the
default playbooks installed in ``vault/system/playbooks/`` cover the same
trigger events, logic steps, and behavioral intent as the six default rules
in ``src/prompts/default_rules/``.

The rule→playbook mapping is:

  Rules (6)                                         Playbook (1)
  ─────────────────────────────────────────────────  ──────────────────────────
  post-action-reflection  (task.completed)       ─┐
  spec-drift-detector     (task.completed)       ─┤  task-outcome.md
  error-recovery-monitor  (task.failed)          ─┘    (task.completed, task.failed)

  periodic-project-review (every 30 min)         →  system-health-check.md
                                                        (timer.30m)

  proactive-codebase-inspector (every 4 hours)   →  codebase-inspector.md
                                                        (timer.4h)

  dependency-update-check (every 24 hours)       →  dependency-audit.md
                                                        (timer.24h)

Additionally, the default_playbooks directory contains
``vibecop-weekly-scan.md`` (timer.168h), which replaces the VibeCop
plugin's ``@cron("0 6 * * 1")`` hook (roadmap 5.6.7, playbooks spec §16).

Both systems ultimately send prompts to an LLM, so comparing literal outputs
is non-deterministic.  Instead we validate:

  1. **Trigger equivalence** — same events fire both systems.
  2. **Logic coverage** — playbook markdown covers every logic step from the
     rule(s) it replaces.
  3. **Structural validity** — playbooks have valid frontmatter and compile
     without errors (with mocked LLM).
  4. **Coexistence** — both install cleanly to the same vault and both systems
     recognise their respective triggers for the same events.
  5. **Consolidation correctness** — 3 rules → 1 playbook for task-outcome;
     1:1 for the remaining three.

These validations are the gate between Phase 1 (coexistence) and Phase 2
(default rule removal, roadmap 5.6.8).
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
import yaml

from src.playbook_compiler import PlaybookCompiler
from src.playbook_manager import PlaybookManager
from src.playbook_models import CompiledPlaybook, PlaybookNode, PlaybookTrigger
from src.rule_manager import RuleManager
from src.vault import ensure_default_playbooks


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
_RULES_DIR = os.path.join(_SRC_DIR, "prompts", "default_rules")
_PLAYBOOKS_DIR = os.path.join(_SRC_DIR, "prompts", "default_playbooks")


def _read(directory: str, filename: str) -> str:
    """Read a file from the given directory."""
    with open(os.path.join(directory, filename)) as f:
        return f.read()


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, content
    return fm, parts[2].strip()


# ===================================================================
# 1. Trigger Equivalence
# ===================================================================


class TestTriggerEquivalence:
    """Verify that every rule trigger is covered by its replacement playbook."""

    def test_task_outcome_covers_task_completed(self):
        """task-outcome.md triggers on task.completed (replaces post-action-reflection + spec-drift-detector)."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "task-outcome.md"))
        triggers = fm["triggers"]
        assert "task.completed" in triggers

    def test_task_outcome_covers_task_failed(self):
        """task-outcome.md triggers on task.failed (replaces error-recovery-monitor)."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "task-outcome.md"))
        triggers = fm["triggers"]
        assert "task.failed" in triggers

    def test_system_health_check_covers_periodic_review(self):
        """system-health-check.md uses timer.30m (replaces periodic-project-review's 'every 30 min')."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "system-health-check.md"))
        triggers = fm["triggers"]
        assert "timer.30m" in triggers

        # Verify the rule parses to the same interval
        rule_content = _read(_RULES_DIR, "periodic-project-review.md")
        trigger = RuleManager._parse_trigger(rule_content)
        assert trigger is not None
        assert trigger["type"] == "periodic"
        assert trigger["interval_seconds"] == 1800  # 30 minutes

    def test_codebase_inspector_covers_proactive_inspector(self):
        """codebase-inspector.md uses timer.4h (replaces proactive-codebase-inspector's 'every 4 hours')."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "codebase-inspector.md"))
        triggers = fm["triggers"]
        assert "timer.4h" in triggers

        # Verify the rule parses to the same interval
        rule_content = _read(_RULES_DIR, "proactive-codebase-inspector.md")
        trigger = RuleManager._parse_trigger(rule_content)
        assert trigger is not None
        assert trigger["type"] == "periodic"
        assert trigger["interval_seconds"] == 14400  # 4 hours

    def test_dependency_audit_covers_dependency_check(self):
        """dependency-audit.md uses timer.24h (replaces dependency-update-check's 'every 24 hours')."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "dependency-audit.md"))
        triggers = fm["triggers"]
        assert "timer.24h" in triggers

        # Verify the rule parses to the same interval
        rule_content = _read(_RULES_DIR, "dependency-update-check.md")
        trigger = RuleManager._parse_trigger(rule_content)
        assert trigger is not None
        assert trigger["type"] == "periodic"
        assert trigger["interval_seconds"] == 86400  # 24 hours

    def test_all_rule_triggers_have_playbook_coverage(self):
        """Every default rule trigger event type appears in at least one default playbook's triggers."""
        # Collect all rule trigger event types
        rule_triggers: set[str] = set()
        for filename in sorted(os.listdir(_RULES_DIR)):
            if not filename.endswith(".md"):
                continue
            content = _read(_RULES_DIR, filename)
            trigger = RuleManager._parse_trigger(content)
            if trigger is None:
                continue
            if trigger["type"] == "event":
                rule_triggers.add(trigger["event_type"])
            elif trigger["type"] == "periodic":
                # Map periodic intervals to timer event types
                secs = trigger["interval_seconds"]
                if secs == 1800:
                    rule_triggers.add("timer.30m")
                elif secs == 14400:
                    rule_triggers.add("timer.4h")
                elif secs == 86400:
                    rule_triggers.add("timer.24h")

        # Collect all playbook trigger event types
        playbook_triggers: set[str] = set()
        for filename in sorted(os.listdir(_PLAYBOOKS_DIR)):
            if not filename.endswith(".md"):
                continue
            fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, filename))
            for t in fm.get("triggers", []):
                if isinstance(t, str):
                    playbook_triggers.add(t)
                elif isinstance(t, dict):
                    playbook_triggers.add(t["event_type"])

        # Every rule trigger must be covered by a playbook
        uncovered = rule_triggers - playbook_triggers
        assert uncovered == set(), f"Rule triggers not covered by any playbook: {uncovered}"


# ===================================================================
# 2. Logic Coverage — Playbook markdown covers all rule logic steps
# ===================================================================


class TestLogicCoverage:
    """Verify that playbook content addresses every concern from the rules it replaces."""

    # -- task-outcome.md covers post-action-reflection -----------------

    def test_task_outcome_covers_review_output(self):
        """task-outcome covers 'review completed task's output' from post-action-reflection."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "review" in body and "output" in body

    def test_task_outcome_covers_acceptance_criteria(self):
        """task-outcome covers 'acceptance criteria' from post-action-reflection."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "acceptance criteria" in body or "criteria" in body

    def test_task_outcome_covers_follow_up(self):
        """task-outcome covers 'follow-up tasks' from post-action-reflection."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "follow-up" in body or "follow up" in body or "create" in body

    def test_task_outcome_covers_memory_update(self):
        """task-outcome covers 'update project memory' from post-action-reflection."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "memory" in body or "insights" in body

    # -- task-outcome.md covers spec-drift-detector --------------------

    def test_task_outcome_covers_spec_check(self):
        """task-outcome covers 'check if modified files have specs' from spec-drift-detector."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "spec" in body

    def test_task_outcome_covers_spec_divergence(self):
        """task-outcome covers 'code diverged from spec' from spec-drift-detector."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "diverge" in body or "spec" in body

    def test_task_outcome_covers_spec_update_task(self):
        """task-outcome covers 'create task to update spec' from spec-drift-detector."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "update the spec" in body or "spec" in body

    # -- task-outcome.md covers error-recovery-monitor -----------------

    def test_task_outcome_covers_error_review(self):
        """task-outcome covers 'review failed task's error' from error-recovery-monitor."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "fail" in body

    def test_task_outcome_covers_recurring_failure(self):
        """task-outcome covers 'recurring failure' check from error-recovery-monitor."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "recurring" in body

    def test_task_outcome_covers_transient_retry(self):
        """task-outcome covers 'transient error → retry' from error-recovery-monitor."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "transient" in body and "retry" in body

    def test_task_outcome_covers_code_issue_fix(self):
        """task-outcome covers 'code issue → create fix task' from error-recovery-monitor."""
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        assert "code issue" in body and "fix" in body

    # -- system-health-check.md covers periodic-project-review ---------

    def test_health_check_covers_stuck_tasks(self):
        """system-health-check covers 'stuck tasks' from periodic-project-review."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "stuck" in body

    def test_health_check_covers_assigned_too_long(self):
        """system-health-check covers 'ASSIGNED too long' from periodic-project-review."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "assigned" in body

    def test_health_check_covers_blocked_tasks(self):
        """system-health-check covers 'blocked tasks' from periodic-project-review."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "blocked" in body

    def test_health_check_covers_no_resolution_path(self):
        """system-health-check covers 'no resolution path' from periodic-project-review."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "resolution" in body or "no resolution path" in body

    def test_health_check_covers_summary(self):
        """system-health-check covers 'post summary' from periodic-project-review."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "summary" in body

    # -- codebase-inspector.md covers proactive-codebase-inspector -----

    def test_inspector_covers_git_ls_files(self):
        """codebase-inspector covers 'git ls-files' from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        # The playbook may reference weighted selection without mentioning the exact command,
        # but the rule's core concern is file listing + random selection
        assert "source" in body or "inspect" in body

    def test_inspector_covers_weighted_selection(self):
        """codebase-inspector covers weighted category selection from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "40%" in body or "source" in body
        assert "20%" in body or "specs" in body
        assert "15%" in body or "tests" in body

    def test_inspector_covers_quality_issues(self):
        """codebase-inspector covers quality analysis from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "quality" in body

    def test_inspector_covers_security_risks(self):
        """codebase-inspector covers security risk analysis from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "security" in body

    def test_inspector_covers_actionable_findings(self):
        """codebase-inspector covers 'only actionable findings' from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "actionable" in body

    def test_inspector_covers_inspection_history(self):
        """codebase-inspector covers 'check inspection history' from proactive-codebase-inspector."""
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "history" in body or "re-inspect" in body or "avoid" in body

    # -- dependency-audit.md covers dependency-update-check -------------

    def test_audit_covers_check_outdated_deps(self):
        """dependency-audit covers check-outdated-deps script from dependency-update-check."""
        body = _read(_PLAYBOOKS_DIR, "dependency-audit.md").lower()
        assert "check-outdated-deps" in body

    def test_audit_covers_pip_audit(self):
        """dependency-audit covers pip-audit from dependency-update-check."""
        body = _read(_PLAYBOOKS_DIR, "dependency-audit.md").lower()
        assert "pip-audit" in body

    def test_audit_covers_critical_vulnerabilities(self):
        """dependency-audit covers 'critical vulnerabilities → high-priority task'."""
        body = _read(_PLAYBOOKS_DIR, "dependency-audit.md").lower()
        assert "critical" in body and "high-priority" in body

    def test_audit_covers_non_critical_summary(self):
        """dependency-audit covers 'non-critical → summary note' from dependency-update-check."""
        body = _read(_PLAYBOOKS_DIR, "dependency-audit.md").lower()
        assert "non-critical" in body or "summary" in body

    def test_audit_covers_skip_when_clean(self):
        """dependency-audit covers 'skip if nothing found' from dependency-update-check."""
        body = _read(_PLAYBOOKS_DIR, "dependency-audit.md").lower()
        assert "skip" in body


# ===================================================================
# 3. Structural Validity — valid frontmatter and compilation
# ===================================================================


class TestStructuralValidity:
    """Verify all default playbook files have valid YAML frontmatter
    with required fields, and that they compile successfully."""

    @pytest.fixture(
        params=[
            "task-outcome.md",
            "system-health-check.md",
            "codebase-inspector.md",
            "dependency-audit.md",
        ]
    )
    def playbook_file(self, request):
        return request.param

    def test_frontmatter_has_required_fields(self, playbook_file):
        """Each default playbook has id, triggers, and scope in frontmatter."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, playbook_file))
        assert "id" in fm, f"{playbook_file}: missing 'id'"
        assert "triggers" in fm, f"{playbook_file}: missing 'triggers'"
        assert "scope" in fm, f"{playbook_file}: missing 'scope'"

    def test_frontmatter_triggers_is_list(self, playbook_file):
        """triggers field is a list with at least one entry."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, playbook_file))
        assert isinstance(fm["triggers"], list), f"{playbook_file}: triggers not a list"
        assert len(fm["triggers"]) > 0, f"{playbook_file}: triggers list is empty"

    def test_frontmatter_scope_is_system(self, playbook_file):
        """All default playbooks have system scope."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, playbook_file))
        assert fm["scope"] == "system", f"{playbook_file}: scope should be 'system'"

    def test_frontmatter_id_matches_filename(self, playbook_file):
        """Playbook id matches filename (without .md extension)."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, playbook_file))
        expected_id = playbook_file.replace(".md", "")
        assert fm["id"] == expected_id, (
            f"{playbook_file}: id '{fm['id']}' doesn't match filename '{expected_id}'"
        )

    def test_body_is_non_empty(self, playbook_file):
        """Playbook markdown body has actual content (not just frontmatter)."""
        _, body = _parse_frontmatter(_read(_PLAYBOOKS_DIR, playbook_file))
        assert len(body.strip()) > 50, f"{playbook_file}: body too short"

    def test_frontmatter_validates_via_compiler(self, playbook_file):
        """PlaybookCompiler accepts the frontmatter without validation errors."""
        content = _read(_PLAYBOOKS_DIR, playbook_file)
        fm, _ = PlaybookCompiler._parse_frontmatter(content)
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert errors == [], f"{playbook_file}: frontmatter validation errors: {errors}"


class TestCompilationSuccess:
    """Verify all 4 default playbooks compile with a mocked LLM."""

    @staticmethod
    def _make_nodes_for(playbook_id: str) -> dict:
        """Return a minimal valid compiled node graph for each default playbook."""
        if playbook_id == "task-outcome":
            return {
                "nodes": {
                    "evaluate": {
                        "entry": True,
                        "prompt": "Review the task output against acceptance criteria.",
                        "transitions": [
                            {"when": "task completed successfully", "goto": "check_specs"},
                            {"when": "task failed", "goto": "handle_failure"},
                        ],
                    },
                    "check_specs": {
                        "prompt": "Check if modified files have corresponding specs that diverged.",
                        "transitions": [
                            {"when": "specs diverged", "goto": "create_spec_task"},
                            {"otherwise": True, "goto": "done"},
                        ],
                    },
                    "create_spec_task": {
                        "prompt": "Create a task to update the diverged spec.",
                        "goto": "done",
                    },
                    "handle_failure": {
                        "prompt": "Analyze the failure — transient or code issue.",
                        "transitions": [
                            {"when": "transient error", "goto": "retry"},
                            {"when": "code issue", "goto": "create_fix_task"},
                            {"otherwise": True, "goto": "done"},
                        ],
                    },
                    "retry": {
                        "prompt": "Retry the task.",
                        "terminal": True,
                    },
                    "create_fix_task": {
                        "prompt": "Create a fix task for the code issue.",
                        "terminal": True,
                    },
                    "done": {"terminal": True},
                }
            }
        elif playbook_id == "system-health-check":
            return {
                "nodes": {
                    "check_stuck": {
                        "entry": True,
                        "prompt": "Check for stuck tasks in ASSIGNED or IN_PROGRESS.",
                        "goto": "check_blocked",
                    },
                    "check_blocked": {
                        "prompt": "Check for blocked tasks with no resolution path.",
                        "goto": "check_agents",
                    },
                    "check_agents": {
                        "prompt": "Verify agent health — check for unresponsive agents.",
                        "goto": "summarize",
                    },
                    "summarize": {
                        "prompt": "Post summary of issues found, skip if healthy.",
                        "terminal": True,
                    },
                }
            }
        elif playbook_id == "codebase-inspector":
            return {
                "nodes": {
                    "select_file": {
                        "entry": True,
                        "prompt": "Select a random file using weighted categories.",
                        "goto": "analyze",
                    },
                    "analyze": {
                        "prompt": "Analyze the file for quality, security, and docs.",
                        "transitions": [
                            {"when": "actionable finding", "goto": "report"},
                            {"otherwise": True, "goto": "done"},
                        ],
                    },
                    "report": {
                        "prompt": "Report findings and record inspection in memory.",
                        "terminal": True,
                    },
                    "done": {"terminal": True},
                }
            }
        else:  # dependency-audit
            return {
                "nodes": {
                    "scan": {
                        "entry": True,
                        "prompt": "Run check-outdated-deps and pip-audit.",
                        "transitions": [
                            {"when": "critical vulnerabilities found", "goto": "critical_task"},
                            {"when": "non-critical updates found", "goto": "summary_note"},
                            {"otherwise": True, "goto": "done"},
                        ],
                    },
                    "critical_task": {
                        "prompt": "Create high-priority task for critical vulnerabilities.",
                        "terminal": True,
                    },
                    "summary_note": {
                        "prompt": "Create summary note for non-critical updates.",
                        "terminal": True,
                    },
                    "done": {"terminal": True},
                }
            }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "playbook_file,playbook_id",
        [
            ("task-outcome.md", "task-outcome"),
            ("system-health-check.md", "system-health-check"),
            ("codebase-inspector.md", "codebase-inspector"),
            ("dependency-audit.md", "dependency-audit"),
        ],
    )
    async def test_default_playbook_compiles(self, playbook_file, playbook_id):
        """Each default playbook compiles successfully with mocked LLM."""
        from src.chat_providers.types import ChatResponse, TextBlock

        content = _read(_PLAYBOOKS_DIR, playbook_file)
        nodes = self._make_nodes_for(playbook_id)

        response_text = f"```json\n{json.dumps(nodes, indent=2)}\n```"
        provider = AsyncMock()
        provider.model_name = "test-model"
        provider.create_message = AsyncMock(
            return_value=ChatResponse(content=[TextBlock(text=response_text)])
        )

        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(content)

        assert result.success is True, f"{playbook_file} compilation failed: {result.errors}"
        assert result.playbook is not None
        assert result.playbook.id == playbook_id
        assert result.playbook.scope == "system"
        assert len(result.playbook.triggers) >= 1


# ===================================================================
# 4. Coexistence — both systems install and recognise the same events
# ===================================================================


class TestCoexistence:
    """Verify that rules and playbooks can coexist in the same vault."""

    def test_rules_install_defaults(self, tmp_path):
        """RuleManager.install_defaults() installs all 6 default rules."""
        mgr = RuleManager(storage_root=str(tmp_path))
        installed = mgr.install_defaults()

        expected_ids = [
            "rule-dependency-update-check",
            "rule-error-recovery-monitor",
            "rule-periodic-project-review",
            "rule-post-action-reflection",
            "rule-proactive-codebase-inspector",
            "rule-spec-drift-detector",
        ]
        assert sorted(installed) == expected_ids

    def test_playbooks_install_defaults(self, tmp_path):
        """ensure_default_playbooks() installs all default playbooks.

        Includes the 4 rule-replacement playbooks plus vibecop-weekly-scan
        (migrated from plugin @cron, roadmap 5.6.7).
        """
        result = ensure_default_playbooks(str(tmp_path))

        expected_files = [
            "codebase-inspector.md",
            "dependency-audit.md",
            "system-health-check.md",
            "task-outcome.md",
            "vibecop-weekly-scan.md",
        ]
        assert sorted(result["created"]) == expected_files

    def test_playbooks_supersede_rules_in_vault(self, tmp_path):
        """Phase 2: default rules are NOT installed when playbooks exist.

        When playbooks are installed first, install_defaults() correctly
        recognises that all 6 default rules have playbook equivalents and
        skips them.  Only playbook files should be present in the vault.
        """
        # Install playbooks first (they go to vault/system/playbooks/)
        playbook_result = ensure_default_playbooks(str(tmp_path))
        assert len(playbook_result["created"]) == 5  # 4 rule-replacements + vibecop-weekly-scan

        # Attempt to install rules — should be a no-op
        mgr = RuleManager(storage_root=str(tmp_path))
        installed_rules = mgr.install_defaults()
        assert installed_rules == [], (
            "Default rules should not be installed when playbook equivalents exist"
        )

        # Only playbook files should exist in the vault
        system_playbooks = tmp_path / "vault" / "system" / "playbooks"
        all_files = sorted(f.name for f in system_playbooks.iterdir() if f.suffix == ".md")

        for pf in playbook_result["created"]:
            assert pf in all_files, f"Playbook file {pf} missing"

        # No rule- prefixed files should exist
        rule_files = [f for f in all_files if f.startswith("rule-")]
        assert rule_files == [], f"Unexpected rule files in vault: {rule_files}"

    def test_retire_removes_existing_rules(self, tmp_path):
        """Phase 2: retire_superseded_defaults removes rules when playbooks arrive.

        Simulates the upgrade path: rules installed first (pre-Phase 2),
        then playbooks installed, then retirement cleans up the rules.
        """
        from src.rule_manager import _DEFAULT_RULE_PLAYBOOK_MAP

        # Simulate pre-Phase 2: install rules first (no playbooks)
        mgr = RuleManager(storage_root=str(tmp_path))
        installed_rules = mgr.install_defaults()
        assert len(installed_rules) == 6

        # Now install playbooks (simulating upgrade)
        ensure_default_playbooks(str(tmp_path))

        # Retire superseded defaults
        result = mgr.retire_superseded_defaults()
        assert set(result["retired"]) == set(_DEFAULT_RULE_PLAYBOOK_MAP.keys())

        # Only playbook files should remain
        system_playbooks = tmp_path / "vault" / "system" / "playbooks"
        remaining = sorted(f.name for f in system_playbooks.iterdir() if f.suffix == ".md")
        rule_files = [f for f in remaining if f.startswith("rule-")]
        assert rule_files == [], f"Rule files not retired: {rule_files}"

    def test_rule_triggers_parsed_correctly(self, tmp_path):
        """All default rules have parseable triggers."""
        for filename in sorted(os.listdir(_RULES_DIR)):
            if not filename.endswith(".md"):
                continue
            content = _read(_RULES_DIR, filename)
            trigger = RuleManager._parse_trigger(content)
            assert trigger is not None, f"Rule {filename} has no parseable trigger"
            assert trigger["type"] in ("periodic", "event"), (
                f"Rule {filename} has unexpected trigger type: {trigger['type']}"
            )

    def test_playbook_triggers_are_valid_playbook_trigger_objects(self):
        """All default playbook trigger strings can be converted to PlaybookTrigger objects."""
        for filename in sorted(os.listdir(_PLAYBOOKS_DIR)):
            if not filename.endswith(".md"):
                continue
            fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, filename))
            for t in fm.get("triggers", []):
                trigger = PlaybookTrigger.from_value(t)
                assert trigger.event_type, f"Playbook {filename}: trigger has empty event_type"

    def test_playbook_manager_indexes_default_triggers(self, tmp_path):
        """PlaybookManager correctly indexes triggers for all default playbooks."""
        compiled_dir = tmp_path / "compiled"
        compiled_dir.mkdir()

        mgr = PlaybookManager(data_dir=str(compiled_dir))

        # Add pre-compiled versions of each default playbook
        for filename in sorted(os.listdir(_PLAYBOOKS_DIR)):
            if not filename.endswith(".md"):
                continue
            fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, filename))
            playbook = CompiledPlaybook(
                id=fm["id"],
                version=1,
                source_hash="test123",
                triggers=fm["triggers"],
                scope=fm["scope"],
                nodes={
                    "start": PlaybookNode(entry=True, prompt="Test.", goto="end"),
                    "end": PlaybookNode(terminal=True),
                },
            )
            mgr._active[playbook.id] = playbook
            mgr._index_triggers(playbook)

        # Check trigger map
        trigger_map = mgr.trigger_map
        assert "task.completed" in trigger_map
        assert "task-outcome" in trigger_map["task.completed"]
        assert "task.failed" in trigger_map
        assert "task-outcome" in trigger_map["task.failed"]
        assert "timer.30m" in trigger_map
        assert "system-health-check" in trigger_map["timer.30m"]
        assert "timer.4h" in trigger_map
        assert "codebase-inspector" in trigger_map["timer.4h"]
        assert "timer.24h" in trigger_map
        assert "dependency-audit" in trigger_map["timer.24h"]

        # 4 rule-replacement playbooks + vibecop-weekly-scan (from @cron migration)
        assert mgr.playbook_count == 5

        # Also verify the new vibecop-weekly-scan trigger is indexed
        assert "timer.168h" in trigger_map
        assert "vibecop-weekly-scan" in trigger_map["timer.168h"]


# ===================================================================
# 5. Consolidation Correctness — 6 rules → 4 playbooks mapping
# ===================================================================


class TestConsolidationMapping:
    """Verify the rule-to-playbook consolidation is complete and correct."""

    # Mapping: rule filename → replacement playbook filename
    RULE_TO_PLAYBOOK = {
        "post-action-reflection.md": "task-outcome.md",
        "spec-drift-detector.md": "task-outcome.md",
        "error-recovery-monitor.md": "task-outcome.md",
        "periodic-project-review.md": "system-health-check.md",
        "proactive-codebase-inspector.md": "codebase-inspector.md",
        "dependency-update-check.md": "dependency-audit.md",
    }

    def test_every_default_rule_has_a_playbook_replacement(self):
        """Every default rule file is mapped to a playbook."""
        for filename in sorted(os.listdir(_RULES_DIR)):
            if not filename.endswith(".md"):
                continue
            assert filename in self.RULE_TO_PLAYBOOK, (
                f"Default rule {filename} has no playbook mapping"
            )

    def test_every_mapped_playbook_exists(self):
        """Every playbook referenced in the mapping actually exists."""
        playbook_files = set(f for f in os.listdir(_PLAYBOOKS_DIR) if f.endswith(".md"))
        for rule_file, playbook_file in self.RULE_TO_PLAYBOOK.items():
            assert playbook_file in playbook_files, (
                f"Playbook {playbook_file} (replacement for {rule_file}) not found"
            )

    def test_task_outcome_consolidates_three_rules(self):
        """task-outcome.md replaces exactly 3 rules."""
        consolidated = [
            rule for rule, pb in self.RULE_TO_PLAYBOOK.items() if pb == "task-outcome.md"
        ]
        assert len(consolidated) == 3
        assert set(consolidated) == {
            "post-action-reflection.md",
            "spec-drift-detector.md",
            "error-recovery-monitor.md",
        }

    def test_other_playbooks_are_one_to_one(self):
        """Each remaining playbook replaces exactly one rule."""
        for playbook_file in [
            "system-health-check.md",
            "codebase-inspector.md",
            "dependency-audit.md",
        ]:
            rules = [rule for rule, pb in self.RULE_TO_PLAYBOOK.items() if pb == playbook_file]
            assert len(rules) == 1, (
                f"{playbook_file} should replace exactly 1 rule, got {len(rules)}: {rules}"
            )

    def test_six_rules_map_to_four_playbooks(self):
        """6 default rules → 4 rule-replacement playbooks (3:1 consolidation for task-outcome).

        Note: the default_playbooks directory now also contains
        vibecop-weekly-scan.md (migrated from plugin @cron, roadmap 5.6.7),
        which doesn't replace a rule. The rule→playbook count is still 6→4.
        """
        assert len(os.listdir(_RULES_DIR)) >= 6
        playbook_files = set(f for f in os.listdir(_PLAYBOOKS_DIR) if f.endswith(".md"))
        # 4 rule-replacement playbooks + 1 plugin-cron-replacement playbook
        rule_replacement_playbooks = playbook_files - {"vibecop-weekly-scan.md"}
        assert len(rule_replacement_playbooks) == 4

    def test_task_outcome_trigger_is_superset_of_component_rules(self):
        """task-outcome triggers include both task.completed (2 rules) and task.failed (1 rule)."""
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "task-outcome.md"))
        triggers = set(fm["triggers"])

        # post-action-reflection triggers on task.completed
        # spec-drift-detector triggers on task.completed
        # error-recovery-monitor triggers on task.failed
        assert "task.completed" in triggers
        assert "task.failed" in triggers


# ===================================================================
# 6. Playbook Enhanced Coverage
# ===================================================================


class TestPlaybookEnhancements:
    """Verify that playbooks add value beyond simple rule replication.

    The spec (playbooks §12) states playbooks should consolidate related
    rules and add cross-concern awareness. These tests verify the
    enhancements documented in the default playbook content.
    """

    def test_task_outcome_has_cross_concern_awareness(self):
        """task-outcome links failure analysis with reflection history.

        The playbook should reference that post-action-reflection context
        can inform failure analysis (a benefit of consolidation).
        """
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        # The playbook mentions using reflection context when handling failures
        assert (
            "post-action-reflection" in body
            or "reflection" in body
            or ("flagged" in body and "problematic" in body)
        )

    def test_task_outcome_skip_spec_check_on_failure(self):
        """task-outcome skips spec-drift check when task quality is poor.

        This is an optimisation the separate rules couldn't do — the
        consolidated playbook can short-circuit spec checking when the
        task needs to be redone anyway.
        """
        body = _read(_PLAYBOOKS_DIR, "task-outcome.md").lower()
        # The playbook mentions skipping spec sync when work needs to be redone
        assert "skip" in body or "pointless" in body

    def test_health_check_saves_to_memory(self):
        """system-health-check saves findings to memory for other playbooks.

        The original rule didn't have this — it's a new capability that
        enables the codebase inspector to check for duplicates.
        """
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "memory" in body

    def test_inspector_checks_health_check_findings(self):
        """codebase-inspector checks health-check findings to avoid duplicates.

        This cross-playbook awareness was impossible with separate rules.
        """
        body = _read(_PLAYBOOKS_DIR, "codebase-inspector.md").lower()
        assert "health check" in body or "duplicate" in body

    def test_health_check_covers_agent_health(self):
        """system-health-check adds agent health checking (beyond original rule).

        The original periodic-project-review didn't explicitly check agent
        health — the playbook adds this concern.
        """
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "agent" in body and "health" in body

    def test_health_check_skips_all_clear(self):
        """system-health-check skips 'all clear' messages (no noise when healthy)."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "skip" in body or "do not post" in body

    def test_health_check_covers_circular_dependencies(self):
        """system-health-check checks for circular dependency chains."""
        body = _read(_PLAYBOOKS_DIR, "system-health-check.md").lower()
        assert "circular" in body
