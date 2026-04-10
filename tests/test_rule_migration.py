"""Tests for Phase 2 rule-to-playbook migration.

Validates:
1. Trigger conversion (rule trigger config → playbook trigger strings)
2. Single-rule markdown generation (rule file → playbook markdown)
3. Batch migration (scan + convert all rules in a directory)
4. Idempotency (re-running migration skips existing playbooks)
5. Passive rule handling (migrated to memory, not playbooks)
6. Default rule skipping (handled by ensure_default_playbooks)
7. Edge cases (no trigger, already playbook format, empty files)
"""

from __future__ import annotations

import os
import textwrap

import pytest
import yaml

from src.rule_migration import (
    _build_memory_file,
    _build_playbook_body,
    _compute_cooldown,
    _extract_intent_section,
    _extract_logic_section,
    _extract_title,
    _extract_trigger_section,
    _is_default_rule,
    _is_playbook_file,
    _parse_frontmatter,
    _parse_trigger_from_body,
    _rule_id_to_playbook_id,
    _seconds_to_timer_trigger,
    _strip_trigger_and_title,
    generate_playbook_markdown,
    migrate_rules_to_playbooks,
    rule_trigger_to_playbook_triggers,
)


# ---------------------------------------------------------------------------
# Timer trigger conversion
# ---------------------------------------------------------------------------


class TestSecondsToTimerTrigger:
    """Test _seconds_to_timer_trigger: interval seconds → timer.{N}{unit}."""

    def test_minutes(self):
        assert _seconds_to_timer_trigger(300) == "timer.5m"

    def test_half_hour(self):
        assert _seconds_to_timer_trigger(1800) == "timer.30m"

    def test_one_hour(self):
        assert _seconds_to_timer_trigger(3600) == "timer.1h"

    def test_four_hours(self):
        assert _seconds_to_timer_trigger(14400) == "timer.4h"

    def test_twenty_four_hours(self):
        assert _seconds_to_timer_trigger(86400) == "timer.24h"

    def test_one_week(self):
        assert _seconds_to_timer_trigger(604800) == "timer.168h"

    def test_non_hour_multiple(self):
        """90 seconds → 1 minute (90 // 60 = 1)."""
        assert _seconds_to_timer_trigger(90) == "timer.1m"

    def test_zero(self):
        assert _seconds_to_timer_trigger(0) == "timer.1m"

    def test_negative(self):
        assert _seconds_to_timer_trigger(-100) == "timer.1m"

    def test_45_minutes(self):
        assert _seconds_to_timer_trigger(2700) == "timer.45m"


# ---------------------------------------------------------------------------
# Rule trigger → playbook trigger conversion
# ---------------------------------------------------------------------------


class TestRuleTriggerToPlaybookTriggers:
    """Test rule_trigger_to_playbook_triggers."""

    def test_periodic_30_minutes(self):
        config = {"type": "periodic", "interval_seconds": 1800}
        assert rule_trigger_to_playbook_triggers(config) == ["timer.30m"]

    def test_periodic_4_hours(self):
        config = {"type": "periodic", "interval_seconds": 14400}
        assert rule_trigger_to_playbook_triggers(config) == ["timer.4h"]

    def test_periodic_24_hours(self):
        config = {"type": "periodic", "interval_seconds": 86400}
        assert rule_trigger_to_playbook_triggers(config) == ["timer.24h"]

    def test_event_task_completed(self):
        config = {"type": "event", "event_type": "task.completed"}
        assert rule_trigger_to_playbook_triggers(config) == ["task.completed"]

    def test_event_task_failed(self):
        config = {"type": "event", "event_type": "task.failed"}
        assert rule_trigger_to_playbook_triggers(config) == ["task.failed"]

    def test_event_agent_crash(self):
        config = {"type": "event", "event_type": "error.agent_crash"}
        assert rule_trigger_to_playbook_triggers(config) == ["error.agent_crash"]

    def test_cron(self):
        config = {"type": "cron", "cron": "0 0 * * *"}
        assert rule_trigger_to_playbook_triggers(config) == ["cron.0 0 * * *"]

    def test_none_config(self):
        assert rule_trigger_to_playbook_triggers(None) == []

    def test_empty_config(self):
        assert rule_trigger_to_playbook_triggers({}) == []

    def test_unknown_type(self):
        assert rule_trigger_to_playbook_triggers({"type": "unknown"}) == []

    def test_periodic_zero_interval(self):
        config = {"type": "periodic", "interval_seconds": 0}
        assert rule_trigger_to_playbook_triggers(config) == []


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


class TestSectionExtraction:
    """Test extraction of rule body sections."""

    SAMPLE_BODY = textwrap.dedent("""\
        # My Rule

        ## Intent
        Check for problems proactively.

        ## Trigger
        Every 30 minutes.

        ## Logic
        1. Check task queue
        2. Report issues
    """)

    def test_extract_title(self):
        assert _extract_title(self.SAMPLE_BODY) == "My Rule"

    def test_extract_intent(self):
        assert _extract_intent_section(self.SAMPLE_BODY) == "Check for problems proactively."

    def test_extract_trigger(self):
        assert _extract_trigger_section(self.SAMPLE_BODY) == "Every 30 minutes."

    def test_extract_logic(self):
        logic = _extract_logic_section(self.SAMPLE_BODY)
        assert "Check task queue" in logic
        assert "Report issues" in logic

    def test_no_title(self):
        assert _extract_title("No title here\nJust text") == ""

    def test_no_intent(self):
        assert _extract_intent_section("# Title\n## Trigger\nEvery hour") == ""

    def test_no_logic(self):
        assert _extract_logic_section("# Title\n## Trigger\nEvery hour") == ""

    def test_no_trigger(self):
        assert _extract_trigger_section("# Title\n## Logic\nDo stuff") == ""


# ---------------------------------------------------------------------------
# Trigger parsing from body
# ---------------------------------------------------------------------------


class TestParseTriggerFromBody:
    """Test _parse_trigger_from_body: natural language → trigger config."""

    def test_every_n_minutes(self):
        body = "# Rule\n## Trigger\nEvery 5 minutes.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 300}

    def test_every_30_minutes(self):
        body = "# Rule\n## Trigger\nCheck every 30 minutes.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 1800}

    def test_every_4_hours(self):
        body = "# Rule\n## Trigger\nCheck every 4 hours.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 14400}

    def test_every_24_hours(self):
        body = "# Rule\n## Trigger\nCheck every 24 hours.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 86400}

    def test_daily(self):
        body = "# Rule\n## Trigger\nDaily check.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 86400}

    def test_weekly(self):
        body = "# Rule\n## Trigger\nWeekly review.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 604800}

    def test_hourly(self):
        body = "# Rule\n## Trigger\nHourly.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 3600}

    def test_task_completed(self):
        body = "# Rule\n## Trigger\nWhen a task is completed.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "event", "event_type": "task.completed"}

    def test_task_failed(self):
        body = "# Rule\n## Trigger\nWhen a task fails.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "event", "event_type": "task.failed"}

    def test_agent_crash(self):
        body = "# Rule\n## Trigger\nWhen an agent crashes.\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "event", "event_type": "error.agent_crash"}

    def test_no_trigger(self):
        body = "# Rule\nJust some text without a trigger section."
        result = _parse_trigger_from_body(body)
        assert result is None

    def test_inline_trigger(self):
        body = "# Rule\n**Trigger:** Every 10 minutes\n## Logic\nDo stuff."
        result = _parse_trigger_from_body(body)
        assert result == {"type": "periodic", "interval_seconds": 600}


# ---------------------------------------------------------------------------
# Cooldown calculation
# ---------------------------------------------------------------------------


class TestComputeCooldown:
    """Test _compute_cooldown."""

    def test_periodic_30_min(self):
        assert _compute_cooldown({"type": "periodic", "interval_seconds": 1800}) == 900

    def test_periodic_min_cooldown(self):
        """Cooldown is at least 30 seconds."""
        assert _compute_cooldown({"type": "periodic", "interval_seconds": 60}) == 30

    def test_event_no_cooldown(self):
        assert _compute_cooldown({"type": "event", "event_type": "task.completed"}) is None

    def test_none_config(self):
        assert _compute_cooldown(None) is None


# ---------------------------------------------------------------------------
# ID conversion
# ---------------------------------------------------------------------------


class TestRuleIdToPlaybookId:
    """Test _rule_id_to_playbook_id."""

    def test_strips_rule_prefix(self):
        assert _rule_id_to_playbook_id("rule-check-tests") == "check-tests"

    def test_no_prefix(self):
        assert _rule_id_to_playbook_id("my-custom-rule") == "my-custom-rule"

    def test_double_rule_prefix(self):
        assert _rule_id_to_playbook_id("rule-rule-something") == "rule-something"


# ---------------------------------------------------------------------------
# Default rule detection
# ---------------------------------------------------------------------------


class TestIsDefaultRule:
    """Test _is_default_rule."""

    def test_default_rules(self):
        assert _is_default_rule("rule-post-action-reflection")
        assert _is_default_rule("rule-spec-drift-detector")
        assert _is_default_rule("rule-error-recovery-monitor")
        assert _is_default_rule("rule-periodic-project-review")
        assert _is_default_rule("rule-proactive-codebase-inspector")
        assert _is_default_rule("rule-dependency-update-check")

    def test_user_rule(self):
        assert not _is_default_rule("rule-my-custom-automation")
        assert not _is_default_rule("rule-check-test-coverage")


# ---------------------------------------------------------------------------
# Playbook format detection
# ---------------------------------------------------------------------------


class TestIsPlaybookFile:
    """Test _is_playbook_file."""

    def test_playbook_format(self):
        meta = {"id": "test", "triggers": ["task.completed"], "scope": "system"}
        assert _is_playbook_file(meta)

    def test_rule_format(self):
        meta = {"id": "rule-test", "type": "active", "project_id": None}
        assert not _is_playbook_file(meta)

    def test_empty_meta(self):
        assert not _is_playbook_file({})


# ---------------------------------------------------------------------------
# generate_playbook_markdown — the core conversion function
# ---------------------------------------------------------------------------


class TestGeneratePlaybookMarkdown:
    """Test full rule → playbook markdown generation."""

    def _make_rule(
        self,
        rule_id: str = "rule-my-check",
        rule_type: str = "active",
        project_id: str | None = None,
        body: str = "# My Check\n\n## Intent\nCheck stuff.\n\n## Trigger\nEvery 10 minutes.\n\n## Logic\n1. Do step 1\n2. Do step 2",
    ) -> str:
        meta = {
            "id": rule_id,
            "type": rule_type,
            "project_id": project_id,
            "hooks": [],
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-02T00:00:00Z",
        }
        fm = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{fm}\n---\n\n{body}\n"

    def test_basic_periodic_rule(self):
        content = self._make_rule()
        result = generate_playbook_markdown(content)
        assert result["success"]
        assert result["playbook_id"] == "my-check"
        assert result["triggers"] == ["timer.10m"]
        assert result["scope"] == "system"
        assert result["is_passive"] is False

        # Verify generated markdown is valid
        md = result["playbook_markdown"]
        meta, body = _parse_frontmatter(md)
        assert meta["id"] == "my-check"
        assert meta["triggers"] == ["timer.10m"]
        assert meta["scope"] == "system"
        assert "cooldown" in meta
        assert "My Check" in body
        assert "Check stuff" in body
        assert "Do step 1" in body

    def test_event_trigger_rule(self):
        content = self._make_rule(
            rule_id="rule-post-task-check",
            body="# Post Task Check\n\n## Trigger\nWhen a task is completed.\n\n## Logic\n1. Review results",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        assert result["triggers"] == ["task.completed"]
        assert result["scope"] == "system"

    def test_project_scoped_rule(self):
        content = self._make_rule(project_id="my-project")
        result = generate_playbook_markdown(content)
        assert result["success"]
        assert result["scope"] == "project"

    def test_passive_rule_rejected(self):
        content = self._make_rule(rule_type="passive")
        result = generate_playbook_markdown(content)
        assert not result["success"]
        assert result["is_passive"]
        assert "memory files" in result["error"]

    def test_no_trigger(self):
        content = self._make_rule(
            body="# No Trigger Rule\n\n## Logic\n1. Do something",
        )
        result = generate_playbook_markdown(content)
        assert not result["success"]
        assert "Could not parse trigger" in result["error"]

    def test_no_id_with_override(self):
        content = "---\ntype: active\n---\n\n# Test\n\n## Trigger\nEvery hour\n\n## Logic\nDo it."
        result = generate_playbook_markdown(content, rule_id="rule-test-override")
        assert result["success"]
        assert result["playbook_id"] == "test-override"

    def test_no_id_no_override(self):
        content = "---\ntype: active\n---\n\n# Test\n\n## Trigger\nEvery hour\n\n## Logic\nDo it."
        result = generate_playbook_markdown(content)
        assert not result["success"]
        assert "no ID" in result["error"]

    def test_cooldown_for_periodic(self):
        content = self._make_rule(
            body="# Check\n\n## Trigger\nEvery 30 minutes\n\n## Logic\n1. Check stuff",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        md = result["playbook_markdown"]
        meta, _ = _parse_frontmatter(md)
        assert meta["cooldown"] == 900  # 1800 / 2

    def test_no_cooldown_for_event(self):
        content = self._make_rule(
            body="# Check\n\n## Trigger\nWhen a task is completed\n\n## Logic\n1. Check stuff",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        md = result["playbook_markdown"]
        meta, _ = _parse_frontmatter(md)
        assert "cooldown" not in meta

    def test_body_without_sections(self):
        """Rules without ## Intent / ## Logic should still produce a body."""
        content = self._make_rule(
            body="# Simple Rule\n\n## Trigger\nEvery hour\n\nJust do something useful.",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        md = result["playbook_markdown"]
        _, body = _parse_frontmatter(md)
        assert "Simple Rule" in body
        assert "Just do something useful" in body

    def test_daily_trigger(self):
        content = self._make_rule(
            body="# Daily Check\n\n## Trigger\nDaily.\n\n## Logic\n1. Review everything.",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        assert result["triggers"] == ["timer.24h"]

    def test_weekly_trigger(self):
        content = self._make_rule(
            body="# Weekly Review\n\n## Trigger\nWeekly.\n\n## Logic\n1. Full review.",
        )
        result = generate_playbook_markdown(content)
        assert result["success"]
        assert result["triggers"] == ["timer.168h"]


# ---------------------------------------------------------------------------
# Playbook body building
# ---------------------------------------------------------------------------


class TestBuildPlaybookBody:
    """Test _build_playbook_body."""

    def test_with_all_sections(self):
        body = _build_playbook_body(
            "My Rule", "Check stuff proactively.", "1. Do step 1\n2. Do step 2", ""
        )
        assert body.startswith("# My Rule")
        assert "Check stuff proactively." in body
        assert "1. Do step 1" in body

    def test_no_intent(self):
        body = _build_playbook_body("My Rule", "", "1. Do step 1\n2. Do step 2", "")
        assert "# My Rule" in body
        assert "1. Do step 1" in body

    def test_no_logic_no_intent_with_fallback(self):
        full_body = "# Title\n\n## Trigger\nEvery hour\n\nSome content here."
        body = _build_playbook_body("Title", "", "", full_body)
        assert "# Title" in body
        assert "Some content here" in body
        # Trigger section should be stripped
        assert "Every hour" not in body


# ---------------------------------------------------------------------------
# Strip trigger and title
# ---------------------------------------------------------------------------


class TestStripTriggerAndTitle:
    """Test _strip_trigger_and_title."""

    def test_strips_both(self):
        body = "# Title\n\n## Trigger\nEvery hour.\n\n## Logic\nDo stuff."
        result = _strip_trigger_and_title(body)
        assert "Title" not in result
        assert "Every hour" not in result
        assert "## Logic" in result
        assert "Do stuff" in result

    def test_no_trigger(self):
        body = "# Title\n\n## Logic\nDo stuff."
        result = _strip_trigger_and_title(body)
        assert "Title" not in result
        assert "Do stuff" in result

    def test_empty(self):
        assert _strip_trigger_and_title("").strip() == ""


# ---------------------------------------------------------------------------
# Memory file building (passive rules)
# ---------------------------------------------------------------------------


class TestBuildMemoryFile:
    """Test _build_memory_file for passive rule migration."""

    def test_basic(self):
        meta = {"id": "rule-my-guidance", "project_id": "proj-1", "created": "2026-01-01T00:00:00Z"}
        body = "# Coding Guidelines\n\nAlways use type hints."
        result = _build_memory_file(meta, body)

        parsed_meta, parsed_body = _parse_frontmatter(result)
        assert parsed_meta["id"] == "rule-my-guidance"
        assert parsed_meta["migrated_from"] == "rule"
        assert "migrated_at" in parsed_meta
        assert "type" not in parsed_meta  # rule-specific field stripped
        assert "hooks" not in parsed_meta  # rule-specific field stripped
        assert "Coding Guidelines" in parsed_body
        assert "Always use type hints" in parsed_body


# ---------------------------------------------------------------------------
# Batch migration (filesystem-based)
# ---------------------------------------------------------------------------


class TestMigrateRulesToPlaybooks:
    """Test migrate_rules_to_playbooks with a temporary directory."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a storage root with vault structure and some rules."""
        root = str(tmp_path)

        # Create vault system playbooks directory
        system_dir = os.path.join(root, "vault", "system", "playbooks")
        os.makedirs(system_dir, exist_ok=True)

        return root

    def _write_rule(self, storage_root, rule_id, rule_type="active", project_id=None, body=None):
        """Helper to write a rule file."""
        if body is None:
            body = (
                f"# {rule_id.replace('rule-', '').replace('-', ' ').title()}\n\n"
                f"## Intent\nDo something useful.\n\n"
                f"## Trigger\nEvery 10 minutes.\n\n"
                f"## Logic\n1. Step one\n2. Step two"
            )
        meta = {
            "id": rule_id,
            "type": rule_type,
            "project_id": project_id,
            "hooks": [],
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-02T00:00:00Z",
        }
        fm = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        content = f"---\n{fm}\n---\n\n{body}\n"

        if project_id:
            rules_dir = os.path.join(storage_root, "vault", "projects", project_id, "playbooks")
        else:
            rules_dir = os.path.join(storage_root, "vault", "system", "playbooks")
        os.makedirs(rules_dir, exist_ok=True)
        filepath = os.path.join(rules_dir, f"{rule_id}.md")
        with open(filepath, "w") as f:
            f.write(content)
        return filepath

    def test_migrate_single_active_rule(self, storage):
        """Migrate a single user-created active rule."""
        self._write_rule(storage, "rule-check-test-coverage")

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 1
        assert result["errors"] == 0

        # Verify the playbook file was created
        playbook_path = os.path.join(
            storage, "vault", "system", "playbooks", "check-test-coverage.md"
        )
        assert os.path.isfile(playbook_path)

        # Verify the playbook content
        with open(playbook_path) as f:
            content = f.read()
        meta, body = _parse_frontmatter(content)
        assert meta["id"] == "check-test-coverage"
        assert meta["triggers"] == ["timer.10m"]
        assert meta["scope"] == "system"

    def test_dry_run_no_files_written(self, storage):
        """Dry run should report but not write files."""
        self._write_rule(storage, "rule-check-stuff")

        result = migrate_rules_to_playbooks(storage, dry_run=True)
        assert result["migrated"] == 1
        assert result["dry_run"] is True

        # The playbook file should NOT exist
        playbook_path = os.path.join(storage, "vault", "system", "playbooks", "check-stuff.md")
        assert not os.path.isfile(playbook_path)

    def test_skip_default_rules(self, storage):
        """Default rules should be skipped."""
        self._write_rule(storage, "rule-post-action-reflection")

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 0
        assert result["skipped_default"] == 1

    def test_include_defaults(self, storage):
        """With include_defaults=True, default rules are migrated."""
        self._write_rule(
            storage,
            "rule-post-action-reflection",
            body="# Post-Action Reflection\n\n## Trigger\nWhen a task is completed.\n\n## Logic\n1. Review",
        )

        result = migrate_rules_to_playbooks(storage, include_defaults=True)
        assert result["migrated"] == 1

    def test_skip_existing_playbook(self, storage):
        """If a playbook already exists, skip the rule."""
        self._write_rule(storage, "rule-check-stuff")

        # Also create the target playbook
        playbook_path = os.path.join(storage, "vault", "system", "playbooks", "check-stuff.md")
        with open(playbook_path, "w") as f:
            f.write(
                "---\nid: check-stuff\ntriggers:\n  - timer.10m\nscope: system\n---\n\n# Check Stuff\n"
            )

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 0
        assert result["skipped_existing"] == 1

    def test_passive_rule_to_memory(self, storage):
        """Passive rules should be migrated to memory files."""
        self._write_rule(
            storage,
            "rule-coding-guidelines",
            rule_type="passive",
            body="# Coding Guidelines\n\nAlways use type hints.\nNever use bare except.",
        )

        result = migrate_rules_to_playbooks(storage)
        assert result["passive_migrated"] == 1
        assert result["migrated"] == 0

        # Verify memory file was created
        memory_path = os.path.join(
            storage, "vault", "system", "memory", "rule-coding-guidelines.md"
        )
        assert os.path.isfile(memory_path)

    def test_project_scoped_rule(self, storage):
        """Project-scoped rules should be written to the project's playbook dir."""
        self._write_rule(storage, "rule-project-check", project_id="my-project")

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 1

        playbook_path = os.path.join(
            storage, "vault", "projects", "my-project", "playbooks", "project-check.md"
        )
        assert os.path.isfile(playbook_path)

        with open(playbook_path) as f:
            content = f.read()
        meta, _ = _parse_frontmatter(content)
        assert meta["scope"] == "project"

    def test_idempotent(self, storage):
        """Running migration twice should not duplicate files."""
        self._write_rule(storage, "rule-check-stuff")

        result1 = migrate_rules_to_playbooks(storage)
        assert result1["migrated"] == 1

        result2 = migrate_rules_to_playbooks(storage)
        assert result2["migrated"] == 0
        assert result2["skipped_existing"] >= 1

    def test_skip_already_playbook_format(self, storage):
        """Files already in playbook format should be skipped."""
        # Write a file that looks like a playbook (has triggers in frontmatter)
        playbook_dir = os.path.join(storage, "vault", "system", "playbooks")
        playbook_path = os.path.join(playbook_dir, "existing-playbook.md")
        with open(playbook_path, "w") as f:
            f.write(
                "---\nid: existing-playbook\ntriggers:\n  - timer.1h\nscope: system\n---\n\n# Existing\n"
            )

        result = migrate_rules_to_playbooks(storage)
        assert result["skipped_already_playbook"] >= 1
        assert result["migrated"] == 0

    def test_multiple_rules(self, storage):
        """Migrate multiple rules at once."""
        self._write_rule(storage, "rule-check-a")
        self._write_rule(storage, "rule-check-b")
        self._write_rule(
            storage,
            "rule-passive-guide",
            rule_type="passive",
            body="# Guide\n\nBe nice.",
        )

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 2
        assert result["passive_migrated"] == 1
        assert result["errors"] == 0

    def test_legacy_memory_location(self, storage):
        """Rules in legacy memory/ locations should be migrated."""
        # Create legacy rule directory
        legacy_dir = os.path.join(storage, "memory", "global", "rules")
        os.makedirs(legacy_dir, exist_ok=True)

        meta = {
            "id": "rule-legacy-check",
            "type": "active",
            "project_id": None,
            "hooks": [],
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-02T00:00:00Z",
        }
        body = "# Legacy Check\n\n## Trigger\nEvery hour.\n\n## Logic\n1. Check stuff."
        fm = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        content = f"---\n{fm}\n---\n\n{body}\n"

        with open(os.path.join(legacy_dir, "rule-legacy-check.md"), "w") as f:
            f.write(content)

        result = migrate_rules_to_playbooks(storage)
        assert result["migrated"] == 1

        # Playbook should be in vault/system/playbooks/
        playbook_path = os.path.join(storage, "vault", "system", "playbooks", "legacy-check.md")
        assert os.path.isfile(playbook_path)

    def test_no_trigger_rule(self, storage):
        """Rules without a parseable trigger should produce an error."""
        self._write_rule(
            storage,
            "rule-no-trigger",
            body="# No Trigger\n\n## Logic\n1. Do something without a trigger.",
        )

        result = migrate_rules_to_playbooks(storage)
        assert result["errors"] == 1
        assert result["migrated"] == 0

    def test_details_contain_info(self, storage):
        """The details list should contain human-readable descriptions."""
        self._write_rule(storage, "rule-check-stuff")

        result = migrate_rules_to_playbooks(storage)
        assert len(result["details"]) > 0
        assert any("MIGRATE" in d for d in result["details"])


# ---------------------------------------------------------------------------
# Equivalence with default rules
# ---------------------------------------------------------------------------


class TestDefaultRuleEquivalence:
    """Verify that default rules produce triggers matching their playbook replacements.

    This validates the 6→4 consolidation mapping from the upstream task.
    """

    def _load_default_rule(self, filename: str) -> str:
        """Load a default rule template."""
        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "prompts", "default_rules", filename
        )
        with open(path) as f:
            return f.read()

    def _load_default_playbook(self, filename: str) -> dict:
        """Load a default playbook's frontmatter."""
        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "prompts", "default_playbooks", filename
        )
        with open(path) as f:
            content = f.read()
        meta, _ = _parse_frontmatter(content)
        return meta

    def test_post_action_reflection_trigger_matches(self):
        """post-action-reflection (task.completed) → task-outcome (task.completed, task.failed)."""
        rule_content = self._load_default_rule("post-action-reflection.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert "task.completed" in triggers

        playbook_meta = self._load_default_playbook("task-outcome.md")
        assert "task.completed" in playbook_meta["triggers"]

    def test_error_recovery_monitor_trigger_matches(self):
        """error-recovery-monitor (task.failed) → task-outcome (task.completed, task.failed)."""
        rule_content = self._load_default_rule("error-recovery-monitor.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert "task.failed" in triggers

        playbook_meta = self._load_default_playbook("task-outcome.md")
        assert "task.failed" in playbook_meta["triggers"]

    def test_spec_drift_detector_trigger_matches(self):
        """spec-drift-detector (task.completed) → task-outcome (task.completed, task.failed)."""
        rule_content = self._load_default_rule("spec-drift-detector.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert "task.completed" in triggers

        playbook_meta = self._load_default_playbook("task-outcome.md")
        assert "task.completed" in playbook_meta["triggers"]

    def test_periodic_project_review_trigger_matches(self):
        """periodic-project-review (30 min) → system-health-check (timer.30m)."""
        rule_content = self._load_default_rule("periodic-project-review.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert triggers == ["timer.30m"]

        playbook_meta = self._load_default_playbook("system-health-check.md")
        assert "timer.30m" in playbook_meta["triggers"]

    def test_codebase_inspector_trigger_matches(self):
        """proactive-codebase-inspector (4h) → codebase-inspector (timer.4h)."""
        rule_content = self._load_default_rule("proactive-codebase-inspector.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert triggers == ["timer.4h"]

        playbook_meta = self._load_default_playbook("codebase-inspector.md")
        assert "timer.4h" in playbook_meta["triggers"]

    def test_dependency_update_trigger_matches(self):
        """dependency-update-check (24h) → dependency-audit (timer.24h)."""
        rule_content = self._load_default_rule("dependency-update-check.md")
        trigger = _parse_trigger_from_body(rule_content)
        triggers = rule_trigger_to_playbook_triggers(trigger)
        assert triggers == ["timer.24h"]

        playbook_meta = self._load_default_playbook("dependency-audit.md")
        assert "timer.24h" in playbook_meta["triggers"]


# ---------------------------------------------------------------------------
# Generated playbook validity
# ---------------------------------------------------------------------------


class TestGeneratedPlaybookValidity:
    """Verify that generated playbooks have valid frontmatter for compilation."""

    def _make_rule_content(self, trigger: str) -> str:
        meta = {
            "id": "rule-test-valid",
            "type": "active",
            "project_id": None,
            "hooks": [],
        }
        body = f"# Test Rule\n\n## Intent\nTest intent.\n\n## Trigger\n{trigger}\n\n## Logic\n1. Do stuff."
        fm = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{fm}\n---\n\n{body}\n"

    def test_has_required_frontmatter_fields(self):
        """Generated playbooks must have id, triggers, and scope."""
        content = self._make_rule_content("Every 5 minutes")
        result = generate_playbook_markdown(content)
        assert result["success"]

        meta, _ = _parse_frontmatter(result["playbook_markdown"])
        assert "id" in meta
        assert "triggers" in meta
        assert "scope" in meta
        assert isinstance(meta["triggers"], list)
        assert len(meta["triggers"]) > 0

    def test_triggers_are_strings(self):
        """All triggers in the generated playbook should be strings."""
        content = self._make_rule_content("When a task is completed")
        result = generate_playbook_markdown(content)
        assert result["success"]

        meta, _ = _parse_frontmatter(result["playbook_markdown"])
        for trigger in meta["triggers"]:
            assert isinstance(trigger, str)

    def test_scope_is_valid(self):
        """Scope must be 'system' or 'project'."""
        content = self._make_rule_content("Every hour")
        result = generate_playbook_markdown(content)
        assert result["success"]

        meta, _ = _parse_frontmatter(result["playbook_markdown"])
        assert meta["scope"] in ("system", "project")

    def test_no_rule_specific_frontmatter(self):
        """Generated playbooks should NOT have rule-specific fields."""
        content = self._make_rule_content("Every hour")
        result = generate_playbook_markdown(content)
        assert result["success"]

        meta, _ = _parse_frontmatter(result["playbook_markdown"])
        assert "type" not in meta
        assert "hooks" not in meta
        assert "project_id" not in meta
        assert "created" not in meta
        assert "updated" not in meta

    def test_body_is_not_empty(self):
        """Generated playbook body should have content."""
        content = self._make_rule_content("Every hour")
        result = generate_playbook_markdown(content)
        assert result["success"]

        _, body = _parse_frontmatter(result["playbook_markdown"])
        assert len(body.strip()) > 10

    def test_timer_trigger_format_compatible(self):
        """Timer triggers must match the pattern timer.{N}m or timer.{N}h."""
        import re

        timer_re = re.compile(r"^timer\.(\d+)(m|h)$")

        for seconds, expected in [
            (300, "timer.5m"),
            (1800, "timer.30m"),
            (3600, "timer.1h"),
            (14400, "timer.4h"),
            (86400, "timer.24h"),
        ]:
            content = self._make_rule_content(f"Every {seconds // 60} minutes")
            result = generate_playbook_markdown(content)
            if result["success"]:
                for t in result["triggers"]:
                    if t.startswith("timer."):
                        assert timer_re.match(t), f"Timer trigger '{t}' doesn't match pattern"
