"""Validate plugin @cron() hooks migrated to timer-triggered playbooks.

Roadmap 5.6.7 — Migrate plugin cron hooks to timer-triggered playbooks
per playbooks spec section 16 (Plugin Integration).

This test validates:

  1. The VibeCop ``@cron("0 6 * * 1")`` weekly scan decorator has been
     removed and replaced by ``vibecop-weekly-scan.md`` playbook with a
     ``timer.168h`` trigger.

  2. The playbook covers the same operational steps as the former cron
     method (list projects, scan with vibecop_scan, create tasks for
     errors, notify on findings).

  3. The playbook is structurally valid (frontmatter, scope, trigger).

  4. The ``cron`` import is removed from the VibeCop plugin module.

  5. No @cron-decorated methods remain in any internal plugin (all have
     been migrated).

  6. The default playbook installation picks up the new playbook.
"""

from __future__ import annotations

import inspect
import os

import pytest
import yaml

from src.plugins.internal.vibecop import VibeCopPlugin
from src.timer_service import parse_interval
from src.vault import ensure_default_playbooks


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
# vibecop-weekly-scan.md is no longer auto-installed; it lives with the
# other example playbooks under src/prompts/example_playbooks/ so users
# can opt in by copying it into their vault.
_PLAYBOOKS_DIR = os.path.join(_SRC_DIR, "prompts", "example_playbooks")
_PLUGINS_DIR = os.path.join(_SRC_DIR, "plugins", "internal")


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
# 1. Cron Decorator Removal
# ===================================================================


class TestCronDecoratorRemoval:
    """Verify that @cron decorators have been removed from migrated plugins."""

    def test_vibecop_weekly_scan_has_no_cron_decorator(self):
        """weekly_project_scan should no longer have _cron_expression attribute."""
        plugin = VibeCopPlugin()
        method = plugin.weekly_project_scan
        assert not hasattr(method, "_cron_expression"), (
            "weekly_project_scan still has @cron decorator — migration incomplete"
        )

    def test_vibecop_weekly_scan_has_no_cron_config_key(self):
        """weekly_project_scan should no longer have _cron_config_key attribute."""
        plugin = VibeCopPlugin()
        method = plugin.weekly_project_scan
        assert not hasattr(method, "_cron_config_key"), (
            "weekly_project_scan still has _cron_config_key — migration incomplete"
        )

    def test_vibecop_does_not_import_cron(self):
        """The VibeCop plugin module should not import the cron decorator."""
        import src.plugins.internal.vibecop as vibecop_module

        # Check module-level namespace
        assert not hasattr(vibecop_module, "cron"), (
            "VibeCop module still imports the cron decorator"
        )

    def test_vibecop_weekly_scan_method_still_exists(self):
        """The method itself should still exist for manual invocation."""
        plugin = VibeCopPlugin()
        assert hasattr(plugin, "weekly_project_scan")
        assert callable(plugin.weekly_project_scan)

    def test_vibecop_weekly_scan_is_async(self):
        """The method should remain async-compatible."""
        assert inspect.iscoroutinefunction(VibeCopPlugin.weekly_project_scan)

    def test_no_cron_decorated_methods_remain_in_internal_plugins(self):
        """No internal plugin should have @cron-decorated methods after migration.

        This is a sweep test — if a new plugin adds @cron in the future, this
        test will catch it and remind the author to use a playbook instead.
        """
        from src.plugins.internal import discover_internal_plugins

        for module_name, plugin_class in discover_internal_plugins():
            instance = plugin_class()
            for attr_name in dir(instance):
                try:
                    method = getattr(instance, attr_name)
                except Exception:
                    continue
                if callable(method) and hasattr(method, "_cron_expression"):
                    pytest.fail(
                        f"Plugin {module_name}.{plugin_class.__name__} still has "
                        f"@cron-decorated method '{attr_name}' — migrate to a "
                        f"timer-triggered playbook (see playbooks spec §16)"
                    )


# ===================================================================
# 2. Replacement Playbook Existence & Structure
# ===================================================================


class TestReplacementPlaybook:
    """Verify the vibecop-weekly-scan playbook exists and is structurally valid."""

    @pytest.fixture
    def playbook_content(self) -> str:
        return _read(_PLAYBOOKS_DIR, "vibecop-weekly-scan.md")

    @pytest.fixture
    def frontmatter(self, playbook_content) -> dict:
        fm, _ = _parse_frontmatter(playbook_content)
        return fm

    @pytest.fixture
    def body(self, playbook_content) -> str:
        _, body = _parse_frontmatter(playbook_content)
        return body

    def test_playbook_file_exists(self):
        """vibecop-weekly-scan.md should exist in default_playbooks."""
        path = os.path.join(_PLAYBOOKS_DIR, "vibecop-weekly-scan.md")
        assert os.path.isfile(path), f"Missing playbook: {path}"

    def test_playbook_has_valid_frontmatter(self, frontmatter):
        """Frontmatter should parse as valid YAML."""
        assert frontmatter, "Frontmatter is empty or invalid"

    def test_playbook_id_matches_filename(self, frontmatter):
        """Playbook id should match the filename (without .md)."""
        assert frontmatter.get("id") == "vibecop-weekly-scan"

    def test_playbook_scope_is_system(self, frontmatter):
        """VibeCop weekly scan is a system-scoped playbook."""
        assert frontmatter.get("scope") == "system"

    def test_playbook_has_timer_trigger(self, frontmatter):
        """Playbook should trigger on timer.168h (weekly)."""
        triggers = frontmatter.get("triggers", [])
        assert "timer.168h" in triggers, (
            f"Expected timer.168h trigger, got: {triggers}"
        )

    def test_timer_trigger_parses_to_one_week(self, frontmatter):
        """timer.168h should parse to 604800 seconds (7 days)."""
        triggers = frontmatter.get("triggers", [])
        timer_triggers = [t for t in triggers if isinstance(t, str) and t.startswith("timer.")]
        assert len(timer_triggers) == 1
        seconds = parse_interval(timer_triggers[0])
        assert seconds == 168 * 3600  # 604800 seconds = 7 days

    def test_timer_interval_matches_original_cron(self):
        """timer.168h ≈ cron '0 6 * * 1' (both fire once per week).

        The cron expression fired every Monday at 6 AM UTC.  The timer
        fires every 168 hours (exactly 7 days).  The schedule alignment
        differs (timer starts from service boot rather than wall-clock
        Monday), but the cadence is equivalent.
        """
        # Original cron was "0 6 * * 1" (Monday 6 AM) — fires once per 7-day cycle
        cron_period_hours = 168  # 7 * 24
        timer_seconds = parse_interval("timer.168h")
        assert timer_seconds == cron_period_hours * 3600

    def test_playbook_has_cooldown(self, frontmatter):
        """Playbook should have a cooldown to prevent rapid re-triggering."""
        assert frontmatter.get("cooldown") is not None or frontmatter.get("cooldown_seconds") is not None


# ===================================================================
# 3. Logic Coverage
# ===================================================================


class TestLogicCoverage:
    """Verify the playbook covers the same operational steps as the @cron method."""

    @pytest.fixture
    def body(self) -> str:
        content = _read(_PLAYBOOKS_DIR, "vibecop-weekly-scan.md")
        _, body = _parse_frontmatter(content)
        return body.lower()

    def test_covers_list_projects(self, body):
        """Playbook should instruct listing all projects."""
        assert "list_projects" in body or "list projects" in body

    def test_covers_active_project_filter(self, body):
        """Playbook should filter to only ACTIVE projects."""
        assert "active" in body

    def test_covers_workspace_check(self, body):
        """Playbook should check for workspace path."""
        assert "workspace" in body

    def test_covers_vibecop_scan_tool(self, body):
        """Playbook should use vibecop_scan tool."""
        assert "vibecop_scan" in body

    def test_covers_error_severity(self, body):
        """Playbook should handle error-severity findings."""
        assert "error" in body

    def test_covers_task_creation(self, body):
        """Playbook should create tasks for error findings."""
        assert "create_task" in body or "create a task" in body

    def test_covers_scan_failure_handling(self, body):
        """Playbook should handle scan failures gracefully."""
        assert "fail" in body or "skip" in body or "error" in body

    def test_covers_notification(self, body):
        """Playbook should post results/summary."""
        assert "summary" in body or "post" in body or "notify" in body

    def test_covers_skip_archived(self, body):
        """Playbook should skip non-active projects."""
        assert "archived" in body or "paused" in body or "skip" in body

    def test_original_method_steps_are_covered(self, body):
        """Cross-reference the original method's key operations.

        The original weekly_project_scan method:
        1. Lists projects via ctx.execute_command("list_projects", {})
        2. Filters to status == "ACTIVE"
        3. Skips projects without workspace
        4. Calls self._runner.scan(path=workspace_path) for each
        5. Filters findings by severity threshold
        6. Emits vibecop.scan_completed events
        7. Emits vibecop.findings_detected events for projects with findings
        8. Creates tasks for error-severity findings
        9. Sends notifications per project

        The playbook must cover steps 1-5, 8-9.  Steps 6-7 (event emission)
        are handled automatically by the playbook runner.
        """
        # Key operations that must be present
        assert "list_projects" in body or "list projects" in body, "Missing: list projects"
        assert "active" in body, "Missing: filter to active projects"
        assert "workspace" in body, "Missing: workspace path check"
        assert "vibecop_scan" in body, "Missing: vibecop_scan tool call"
        assert "create_task" in body or "create a task" in body, "Missing: task creation"


# ===================================================================
# 4. Vault Installation
# ===================================================================


class TestVaultInstallation:
    """Verify ensure_default_playbooks produces the minimal default set.

    Note: vibecop-weekly-scan.md is no longer an auto-installed default.
    It lives under ``src/prompts/example_playbooks/`` and users opt in by
    copying it into their vault manually.
    """

    def test_all_default_playbooks_installed(self, tmp_path):
        """Only the minimal default playbook set is auto-installed.

        Everything else now lives under ``src/prompts/example_playbooks/``
        as opt-in reference material.
        """
        result = ensure_default_playbooks(str(tmp_path))

        expected = {"memory-consolidation.md"}
        installed = set(result["created"])
        assert expected == installed, (
            f"Missing playbooks: {expected - installed}, "
            f"Extra playbooks: {installed - expected}"
        )


# ===================================================================
# 5. Timer Service Compatibility
# ===================================================================


class TestTimerServiceCompatibility:
    """Verify the playbook's timer trigger works with the TimerService."""

    def test_timer_trigger_is_valid_format(self):
        """timer.168h should be a valid timer trigger string."""
        seconds = parse_interval("timer.168h")
        assert seconds is not None, "timer.168h is not a valid timer trigger"

    def test_timer_trigger_above_minimum_interval(self):
        """timer.168h is well above the 60-second minimum."""
        seconds = parse_interval("timer.168h")
        assert seconds >= 60

    def test_extract_timer_intervals_includes_168h(self):
        """extract_timer_intervals should pick up timer.168h."""
        from src.timer_service import extract_timer_intervals

        triggers = ["task.completed", "timer.168h", "git.commit"]
        intervals = extract_timer_intervals(triggers)
        assert "timer.168h" in intervals
        assert intervals["timer.168h"] == 604800


# ===================================================================
# 6. Config Key Deprecation
# ===================================================================


class TestConfigDeprecation:
    """Verify the weekly_scan_schedule config key is properly deprecated."""

    def test_config_schema_marks_deprecated(self):
        """The weekly_scan_schedule config key should be marked deprecated."""
        schema = VibeCopPlugin.config_schema
        key_schema = schema.get("weekly_scan_schedule", {})
        assert key_schema.get("deprecated") is True, (
            "weekly_scan_schedule config key should be marked deprecated"
        )

    def test_config_schema_description_mentions_playbook(self):
        """The deprecation notice should reference the replacement playbook."""
        schema = VibeCopPlugin.config_schema
        key_schema = schema.get("weekly_scan_schedule", {})
        desc = key_schema.get("description", "")
        assert "playbook" in desc.lower(), (
            "Deprecation notice should mention the replacement playbook"
        )
        assert "vibecop-weekly-scan" in desc, (
            "Deprecation notice should name the specific playbook"
        )


# ===================================================================
# 7. Consolidation Mapping
# ===================================================================


class TestConsolidationMapping:
    """Verify the mapping from @cron methods to playbooks is complete."""

    def test_one_cron_method_maps_to_one_playbook(self):
        """VibeCop's weekly_project_scan maps to vibecop-weekly-scan playbook.

        This is a 1:1 mapping — one @cron method becomes one playbook.
        """
        # The former cron schedule was "0 6 * * 1" (Every Monday at 6 AM)
        original_period_days = 7

        # The replacement playbook
        fm, _ = _parse_frontmatter(_read(_PLAYBOOKS_DIR, "vibecop-weekly-scan.md"))
        assert fm["id"] == "vibecop-weekly-scan"

        # The timer trigger matches the same period
        timer_seconds = parse_interval("timer.168h")
        timer_days = timer_seconds / 86400
        assert timer_days == original_period_days

    def test_no_unmigrated_cron_methods(self):
        """All internal plugin @cron methods should have replacement playbooks.

        Cross-references internal plugins against default playbooks to ensure
        nothing was missed.
        """
        from src.plugins.internal import discover_internal_plugins

        unmigrated = []
        for module_name, plugin_class in discover_internal_plugins():
            instance = plugin_class()
            for attr_name in dir(instance):
                try:
                    method = getattr(instance, attr_name)
                except Exception:
                    continue
                if callable(method) and hasattr(method, "_cron_expression"):
                    unmigrated.append(f"{module_name}.{attr_name}")

        assert not unmigrated, (
            f"Found unmigrated @cron methods: {unmigrated}. "
            f"Create timer-triggered playbooks for these."
        )
