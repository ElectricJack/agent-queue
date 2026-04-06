"""Tests for the internal vibecop plugin.

Tests cover:
- VibeCopRunner (binary resolution, command execution, output normalization)
- Findings formatter (detailed, summary, edge cases)
- VibeCopPlugin lifecycle (initialize, shutdown, config changes)
- Command handlers (scan, check, status)
- Event handling (task completion auto-scan)
- Severity filtering
- Discord notification formatting
- Helper functions
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.plugins.internal.vibecop import (
    TOOL_CATEGORY,
    TOOL_DEFINITIONS,
    VibeCopPlugin,
    VibeCopRunner,
    _counts_header,
    _filter_by_severity,
    _format_detailed,
    _format_discord_notification,
    _format_finding_entry,
    _format_finding_oneline,
    _format_summary,
    _group_by_severity,
    _normalize_finding,
    _normalize_output,
    _severity_counts,
    format_findings,
)
from src.plugins.base import InternalPlugin, PluginContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    file: str = "src/app.py",
    line: int = 42,
    column: int = 5,
    severity: str = "warning",
    detector: str = "god-function",
    category: str = "quality",
    message: str = "Function is too complex",
    suggestion: str = "Break into smaller functions",
) -> dict:
    """Create a normalized finding dict for testing."""
    return {
        "file": file,
        "line": line,
        "column": column,
        "severity": severity,
        "detector": detector,
        "category": category,
        "message": message,
        "suggestion": suggestion,
    }


def _make_findings(count: int = 5, severity: str = "warning") -> list[dict]:
    """Create multiple test findings."""
    return [
        _make_finding(
            file=f"src/file{i}.py",
            line=i * 10,
            severity=severity,
            detector=f"detector-{i}",
            message=f"Finding {i}",
        )
        for i in range(count)
    ]


@pytest.fixture
def mock_ctx():
    """Create a mock PluginContext for testing."""
    ctx = MagicMock(spec=PluginContext)
    ctx.get_config.return_value = {}
    ctx.get_service.return_value = MagicMock()
    ctx.register_command = MagicMock()
    ctx.register_tool = MagicMock()
    ctx.register_event_type = MagicMock()
    ctx.subscribe = MagicMock()
    ctx.emit_event = AsyncMock()
    ctx.notify = AsyncMock()
    ctx.execute_command = AsyncMock(return_value={"success": True})
    ctx.logger = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Test module-level constants for internal plugin discovery."""

    def test_tool_category(self):
        assert TOOL_CATEGORY == "vibecop"

    def test_tool_definitions_count(self):
        assert len(TOOL_DEFINITIONS) == 3

    def test_tool_definitions_names(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert names == {"vibecop_scan", "vibecop_check", "vibecop_status"}

    def test_tool_definitions_have_schemas(self):
        for tool_def in TOOL_DEFINITIONS:
            assert "name" in tool_def
            assert "description" in tool_def
            assert "input_schema" in tool_def
            assert tool_def["input_schema"]["type"] == "object"

    def test_vibecop_scan_schema(self):
        scan_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "vibecop_scan")
        props = scan_def["input_schema"]["properties"]
        assert "path" in props
        assert "diff_ref" in props
        assert "max_findings" in props
        assert "severity_threshold" in props

    def test_vibecop_check_schema(self):
        check_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "vibecop_check")
        props = check_def["input_schema"]["properties"]
        assert "files" in props
        assert check_def["input_schema"]["required"] == ["files"]

    def test_vibecop_status_schema(self):
        status_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "vibecop_status")
        assert status_def["input_schema"]["properties"] == {}


# ---------------------------------------------------------------------------
# VibeCopRunner
# ---------------------------------------------------------------------------


class TestVibeCopRunner:
    """Tests for the VibeCopRunner async CLI wrapper."""

    def test_init_defaults(self):
        runner = VibeCopRunner()
        assert runner._vibecop_path is None
        assert runner._node_path is None
        assert runner._timeout == 60

    def test_init_custom(self):
        runner = VibeCopRunner(
            vibecop_path="/usr/bin/vibecop",
            node_path="/usr/bin/node",
            timeout=120,
        )
        assert runner._vibecop_path == "/usr/bin/vibecop"
        assert runner._node_path == "/usr/bin/node"
        assert runner._timeout == 120

    @patch("shutil.which", return_value=None)
    def test_resolve_vibecop_cmd_not_found(self, mock_which):
        runner = VibeCopRunner()
        assert runner._resolve_vibecop_cmd() is None

    @patch("shutil.which")
    def test_resolve_vibecop_cmd_npx(self, mock_which):
        def which_side_effect(cmd):
            if cmd == "npx":
                return "/usr/bin/npx"
            return None

        mock_which.side_effect = which_side_effect
        runner = VibeCopRunner()
        result = runner._resolve_vibecop_cmd()
        assert result == ["/usr/bin/npx", "vibecop"]

    @patch("shutil.which")
    def test_resolve_vibecop_cmd_global(self, mock_which):
        def which_side_effect(cmd):
            if cmd == "vibecop":
                return "/usr/bin/vibecop"
            return None

        mock_which.side_effect = which_side_effect
        runner = VibeCopRunner()
        result = runner._resolve_vibecop_cmd()
        assert result == ["/usr/bin/vibecop"]

    @patch("shutil.which", return_value="/usr/bin/vibecop")
    def test_resolve_vibecop_cmd_configured_on_path(self, mock_which):
        runner = VibeCopRunner(vibecop_path="vibecop")
        result = runner._resolve_vibecop_cmd()
        assert result == ["vibecop"]

    @patch("shutil.which", return_value=None)
    def test_resolve_node_cmd_not_found(self, mock_which):
        runner = VibeCopRunner()
        assert runner._resolve_node_cmd() is None

    @patch("shutil.which", return_value="/usr/bin/node")
    def test_resolve_node_cmd_found(self, mock_which):
        runner = VibeCopRunner()
        assert runner._resolve_node_cmd() == "/usr/bin/node"

    @pytest.mark.asyncio
    @patch("shutil.which", return_value=None)
    async def test_scan_not_installed(self, mock_which):
        runner = VibeCopRunner()
        result = await runner.scan()
        assert result["success"] is False
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    @patch("shutil.which", return_value=None)
    async def test_check_not_installed(self, mock_which):
        runner = VibeCopRunner()
        result = await runner.check(files=["test.py"])
        assert result["success"] is False
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_run_timeout(self):
        runner = VibeCopRunner(timeout=0)

        # Mock create_subprocess_exec to simulate a slow process
        async def slow_communicate():
            await asyncio.sleep(10)
            return b"", b""

        mock_proc = MagicMock()
        mock_proc.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run(["fake_cmd"])
            assert result["success"] is False
            assert any("timed out" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_run_file_not_found(self):
        runner = VibeCopRunner()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file"),
        ):
            result = await runner._run(["nonexistent_cmd"])
            assert result["success"] is False
            assert any("not found" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_run_os_error(self):
        runner = VibeCopRunner()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("Permission denied"),
        ):
            result = await runner._run(["bad_cmd"])
            assert result["success"] is False
            assert any("execute" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_run_json_parse_success(self):
        runner = VibeCopRunner()

        findings_json = json.dumps(
            {
                "findings": [
                    {
                        "file": "test.py",
                        "line": 10,
                        "severity": "warning",
                        "detector": "god-function",
                        "message": "Too complex",
                    }
                ],
                "files_scanned": 1,
            }
        )

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (findings_json.encode(), b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run(["vibecop", "scan"])
            assert result["success"] is True
            assert len(result["findings"]) == 1
            assert result["findings"][0]["file"] == "test.py"

    @pytest.mark.asyncio
    async def test_run_json_parse_failure(self):
        runner = VibeCopRunner()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"not json at all", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run(["vibecop", "scan"])
            assert result["success"] is False
            assert any("parse" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_run_empty_stdout_error_exit(self):
        runner = VibeCopRunner()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"some error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run(["vibecop", "scan"])
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_run_empty_stdout_clean_exit(self):
        runner = VibeCopRunner()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run(["vibecop", "scan"])
            assert result["success"] is True
            assert result["findings"] == []


# ---------------------------------------------------------------------------
# Output normalization
# ---------------------------------------------------------------------------


class TestNormalizeOutput:
    """Tests for output normalization functions."""

    def test_normalize_finding_standard(self):
        raw = {
            "file": "test.py",
            "line": 10,
            "column": 5,
            "severity": "error",
            "detector": "sql-injection",
            "category": "security",
            "message": "SQL injection risk",
            "suggestion": "Use parameterized queries",
        }
        result = _normalize_finding(raw)
        assert result["file"] == "test.py"
        assert result["line"] == 10
        assert result["severity"] == "error"
        assert result["detector"] == "sql-injection"
        assert result["message"] == "SQL injection risk"

    def test_normalize_finding_alternate_keys(self):
        raw = {
            "filePath": "test.ts",
            "lineNumber": 20,
            "startColumn": 3,
            "ruleId": "god-function",
            "description": "Too complex",
            "fix": "Break it up",
        }
        result = _normalize_finding(raw)
        assert result["file"] == "test.ts"
        assert result["line"] == 20
        assert result["column"] == 3
        assert result["detector"] == "god-function"
        assert result["message"] == "Too complex"
        assert result["suggestion"] == "Break it up"

    def test_normalize_finding_path_key(self):
        raw = {"path": "app.js", "startLine": 5, "rule": "dead-code"}
        result = _normalize_finding(raw)
        assert result["file"] == "app.js"
        assert result["line"] == 5
        assert result["detector"] == "dead-code"

    def test_normalize_finding_defaults(self):
        result = _normalize_finding({})
        assert result["file"] == ""
        assert result["line"] == 0
        assert result["column"] == 0
        assert result["severity"] == "warning"
        assert result["detector"] == ""
        assert result["message"] == ""
        assert result["suggestion"] == ""

    def test_normalize_output_list(self):
        data = [
            {"file": "a.py", "line": 1, "severity": "error"},
            {"file": "b.py", "line": 2, "severity": "warning"},
        ]
        result = _normalize_output(data)
        assert result["success"] is True
        assert len(result["findings"]) == 2
        assert result["files_scanned"] == 2

    def test_normalize_output_dict_findings(self):
        data = {
            "findings": [{"file": "a.py", "severity": "error"}],
            "files_scanned": 5,
        }
        result = _normalize_output(data)
        assert result["success"] is True
        assert len(result["findings"]) == 1
        assert result["files_scanned"] == 5

    def test_normalize_output_dict_results_key(self):
        data = {"results": [{"file": "a.py"}], "totalFiles": 3}
        result = _normalize_output(data)
        assert len(result["findings"]) == 1
        assert result["files_scanned"] == 3

    def test_normalize_output_dict_violations_key(self):
        data = {"violations": [{"filePath": "a.ts"}], "filesScanned": 7}
        result = _normalize_output(data)
        assert len(result["findings"]) == 1
        assert result["files_scanned"] == 7

    def test_normalize_output_string_errors(self):
        data = {"findings": [], "errors": "Something went wrong"}
        result = _normalize_output(data)
        assert result["errors"] == ["Something went wrong"]

    def test_normalize_output_list_errors(self):
        data = {"findings": [], "errors": ["err1", "err2"]}
        result = _normalize_output(data)
        assert result["errors"] == ["err1", "err2"]


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatter:
    """Tests for the findings formatter functions."""

    def test_format_findings_empty(self):
        result = format_findings([])
        assert "No findings" in result
        assert "clean" in result.lower()

    def test_format_findings_detailed(self):
        findings = _make_findings(3, "warning")
        result = format_findings(findings, mode="detailed")
        assert "Vibecop:" in result
        assert "3 finding(s)" in result

    def test_format_findings_summary(self):
        findings = _make_findings(3, "error")
        result = format_findings(findings, mode="summary")
        assert "3 finding(s)" in result

    def test_format_detailed_groups_by_severity(self):
        findings = [
            _make_finding(severity="error", detector="sql-injection"),
            _make_finding(severity="warning", detector="god-function"),
            _make_finding(severity="info", detector="style"),
        ]
        result = _format_detailed(findings)
        assert "Errors" in result
        assert "Warnings" in result
        assert "Info" in result

    def test_format_detailed_truncation(self):
        # Create many findings to trigger truncation
        findings = _make_findings(200, "warning")
        result = _format_detailed(findings)
        assert "truncated" in result.lower()

    def test_format_summary_top_5(self):
        findings = _make_findings(10, "error")
        result = _format_summary(findings)
        # Summary shows top 5 per severity
        lines = result.strip().split("\n")
        # Header + up to 5 error lines
        assert len(lines) <= 11

    def test_format_finding_entry(self):
        finding = _make_finding()
        result = _format_finding_entry(finding, "[WARN]")
        assert "[WARN]" in result
        assert "src/app.py:42:5" in result
        assert "god-function" in result
        assert "Function is too complex" in result
        assert "Fix:" in result

    def test_format_finding_entry_no_suggestion(self):
        finding = _make_finding(suggestion="")
        result = _format_finding_entry(finding, "[ERROR]")
        assert "Fix:" not in result

    def test_format_finding_entry_no_line(self):
        finding = _make_finding(line=0, column=0)
        result = _format_finding_entry(finding, "[INFO]")
        assert "src/app.py" in result
        assert ":0" not in result

    def test_format_finding_oneline(self):
        finding = _make_finding()
        result = _format_finding_oneline(finding, "[WARN]")
        assert "[WARN]" in result
        assert "src/app.py:42" in result
        assert "god-function" in result

    def test_format_finding_oneline_long_message_truncated(self):
        finding = _make_finding(message="A" * 100)
        result = _format_finding_oneline(finding, "[WARN]")
        assert "..." in result
        # Message should be truncated to ~80 chars
        assert len(finding["message"]) > len(result.split("]")[-1].strip())


class TestSeverityHelpers:
    """Tests for severity-related helper functions."""

    def test_group_by_severity(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
            _make_finding(severity="info"),
        ]
        grouped = _group_by_severity(findings)
        assert len(grouped["error"]) == 2
        assert len(grouped["warning"]) == 1
        assert len(grouped["info"]) == 1

    def test_group_by_severity_empty(self):
        assert _group_by_severity([]) == {}

    def test_severity_counts(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
        ]
        counts = _severity_counts(findings)
        assert counts == {"error": 2, "warning": 1, "info": 0}

    def test_severity_counts_empty(self):
        counts = _severity_counts([])
        assert counts == {"error": 0, "warning": 0, "info": 0}

    def test_counts_header(self):
        counts = {"error": 2, "warning": 3, "info": 1}
        header = _counts_header(counts)
        assert "6 finding(s)" in header
        assert "2 error(s)" in header
        assert "3 warning(s)" in header
        assert "1 info(s)" in header

    def test_counts_header_zero_counts_omitted(self):
        counts = {"error": 0, "warning": 5, "info": 0}
        header = _counts_header(counts)
        assert "5 finding(s)" in header
        assert "error" not in header
        assert "info" not in header

    def test_filter_by_severity_warning(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
            _make_finding(severity="info"),
        ]
        filtered = _filter_by_severity(findings, "warning")
        assert len(filtered) == 2
        severities = {f["severity"] for f in filtered}
        assert severities == {"error", "warning"}

    def test_filter_by_severity_error(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
            _make_finding(severity="info"),
        ]
        filtered = _filter_by_severity(findings, "error")
        assert len(filtered) == 1
        assert filtered[0]["severity"] == "error"

    def test_filter_by_severity_info(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
            _make_finding(severity="info"),
        ]
        filtered = _filter_by_severity(findings, "info")
        assert len(filtered) == 3

    def test_filter_by_severity_unknown_threshold(self):
        findings = [_make_finding(severity="error"), _make_finding(severity="warning")]
        # Unknown threshold defaults to cutoff=1 (warning level)
        filtered = _filter_by_severity(findings, "unknown")
        assert len(filtered) == 2


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------


class TestDiscordNotification:
    """Tests for Discord notification formatting."""

    def test_no_findings(self):
        result = _format_discord_notification(
            project_name="my-app",
            task_id="task-1",
            findings=[],
            workspace_path="/code/my-app",
            files_scanned=10,
        )
        assert "my-app" in result
        assert "No findings" in result
        assert "10 files" in result

    def test_with_findings(self):
        findings = [
            _make_finding(severity="error"),
            _make_finding(severity="warning"),
        ]
        result = _format_discord_notification(
            project_name="my-app",
            task_id="task-1",
            findings=findings,
            workspace_path="/code/my-app",
            files_scanned=5,
        )
        assert "my-app" in result
        assert "task-1" in result
        assert "1 error(s)" in result
        assert "1 warning(s)" in result
        assert "Top findings" in result

    def test_weekly_scan_type(self):
        result = _format_discord_notification(
            project_name="my-app",
            task_id=None,
            findings=[_make_finding(severity="warning")],
            workspace_path="/code/my-app",
            scan_type="weekly",
        )
        assert "Weekly Scan" in result

    def test_no_task_id(self):
        result = _format_discord_notification(
            project_name="my-app",
            task_id=None,
            findings=[],
            workspace_path="/code/my-app",
        )
        assert "my-app" in result
        assert "task" not in result.lower() or "No findings" in result

    def test_top_findings_limited(self):
        findings = _make_findings(10, "error")
        result = _format_discord_notification(
            project_name="my-app",
            task_id="t1",
            findings=findings,
            workspace_path="/w",
        )
        assert "and 5 more" in result

    def test_workspace_path_included(self):
        result = _format_discord_notification(
            project_name="p",
            task_id="t",
            findings=[_make_finding()],
            workspace_path="/some/path",
        )
        assert "/some/path" in result


# ---------------------------------------------------------------------------
# VibeCopPlugin (class & lifecycle)
# ---------------------------------------------------------------------------


class TestVibeCopPluginClass:
    """Tests for the VibeCopPlugin class itself."""

    def test_is_internal_plugin(self):
        assert issubclass(VibeCopPlugin, InternalPlugin)

    def test_has_internal_flag(self):
        assert VibeCopPlugin._internal is True

    def test_default_config(self):
        plugin = VibeCopPlugin()
        assert plugin.default_config["default_severity"] == "warning"
        assert plugin.default_config["scan_timeout"] == 60
        assert plugin.default_config["enforce_vibecop_checkout"] is True
        assert plugin.default_config["auto_scan_on_complete"] is True

    def test_config_schema(self):
        plugin = VibeCopPlugin()
        schema = plugin.config_schema
        assert "node_path" in schema
        assert "vibecop_path" in schema
        assert "default_severity" in schema
        assert "scan_timeout" in schema
        assert "enforce_vibecop_checkout" in schema
        assert "auto_scan_on_complete" in schema
        assert "weekly_scan_schedule" in schema


class TestVibeCopPluginInitialize:
    """Tests for plugin initialization."""

    @pytest.mark.asyncio
    async def test_initialize_registers_commands(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        registered_commands = {
            call.args[0] for call in mock_ctx.register_command.call_args_list
        }
        assert "vibecop_scan" in registered_commands
        assert "vibecop_check" in registered_commands
        assert "vibecop_status" in registered_commands

    @pytest.mark.asyncio
    async def test_initialize_registers_tools(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        assert mock_ctx.register_tool.call_count == 3
        tool_names = {
            call.args[0]["name"] for call in mock_ctx.register_tool.call_args_list
        }
        assert tool_names == {"vibecop_scan", "vibecop_check", "vibecop_status"}

    @pytest.mark.asyncio
    async def test_initialize_registers_event_types(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        event_types = {
            call.args[0] for call in mock_ctx.register_event_type.call_args_list
        }
        assert "vibecop.scan_completed" in event_types
        assert "vibecop.findings_detected" in event_types

    @pytest.mark.asyncio
    async def test_initialize_subscribes_to_task_completed(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        subscribed_events = {
            call.args[0] for call in mock_ctx.subscribe.call_args_list
        }
        assert "task.completed" in subscribed_events

    @pytest.mark.asyncio
    async def test_initialize_injects_rule(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        # Verify save_rule was called with the expected rule ID
        save_rule_calls = [
            c for c in mock_ctx.execute_command.call_args_list if c.args[0] == "save_rule"
        ]
        assert len(save_rule_calls) == 1
        rule_args = save_rule_calls[0].args[1]
        assert rule_args["id"] == "rule-vibecop-pre-complete-check"
        assert rule_args["project_id"] is None
        assert rule_args["type"] == "passive"
        assert "Vibecop Pre-Completion Check" in rule_args["content"]

    @pytest.mark.asyncio
    async def test_initialize_skips_rule_when_disabled(self, mock_ctx):
        mock_ctx.get_config.return_value = {"enforce_vibecop_checkout": False}
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        # Should not have called save_rule
        for call in mock_ctx.execute_command.call_args_list:
            assert call.args[0] != "save_rule"

    @pytest.mark.asyncio
    async def test_initialize_creates_runner(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)
        assert plugin._runner is not None
        assert isinstance(plugin._runner, VibeCopRunner)

    @pytest.mark.asyncio
    async def test_initialize_with_custom_config(self, mock_ctx):
        mock_ctx.get_config.return_value = {
            "vibecop_path": "/custom/vibecop",
            "node_path": "/custom/node",
            "scan_timeout": 120,
        }
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        assert plugin._runner._vibecop_path == "/custom/vibecop"
        assert plugin._runner._node_path == "/custom/node"
        assert plugin._runner._timeout == 120


class TestVibeCopPluginShutdown:
    """Tests for plugin shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_removes_rule(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)
        mock_ctx.execute_command.reset_mock()

        await plugin.shutdown(mock_ctx)

        mock_ctx.execute_command.assert_called_with(
            "delete_rule", {"id": "rule-vibecop-pre-complete-check"}
        )

    @pytest.mark.asyncio
    async def test_shutdown_clears_runner(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        await plugin.shutdown(mock_ctx)

        assert plugin._runner is None
        assert plugin._ctx is None

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_rule_removal_failure(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)
        mock_ctx.execute_command = AsyncMock(side_effect=Exception("Network error"))

        # Should not raise
        await plugin.shutdown(mock_ctx)
        assert plugin._runner is None


class TestVibeCopPluginConfigChanged:
    """Tests for config change handling."""

    @pytest.mark.asyncio
    async def test_config_changed_rebuilds_runner(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        old_runner = plugin._runner
        await plugin.on_config_changed(mock_ctx, {"scan_timeout": 120})
        assert plugin._runner is not old_runner
        assert plugin._runner._timeout == 120

    @pytest.mark.asyncio
    async def test_config_changed_adds_rule(self, mock_ctx):
        mock_ctx.get_config.return_value = {"enforce_vibecop_checkout": False}
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)
        mock_ctx.execute_command.reset_mock()

        await plugin.on_config_changed(mock_ctx, {"enforce_vibecop_checkout": True})

        # Should have called save_rule
        calls = [c for c in mock_ctx.execute_command.call_args_list if c.args[0] == "save_rule"]
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_config_changed_removes_rule(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)
        mock_ctx.execute_command.reset_mock()

        await plugin.on_config_changed(mock_ctx, {"enforce_vibecop_checkout": False})

        calls = [
            c for c in mock_ctx.execute_command.call_args_list if c.args[0] == "delete_rule"
        ]
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


class TestVibeCopScanCommand:
    """Tests for the vibecop_scan command handler."""

    @pytest.mark.asyncio
    async def test_scan_not_initialized(self, mock_ctx):
        plugin = VibeCopPlugin()
        plugin._runner = None
        plugin._ctx = mock_ctx
        result = await plugin.cmd_vibecop_scan({})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_scan_success(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        scan_result = {
            "success": True,
            "findings": [
                _make_finding(severity="error"),
                _make_finding(severity="warning"),
                _make_finding(severity="info"),
            ],
            "files_scanned": 10,
            "errors": [],
        }
        plugin._runner.scan = AsyncMock(return_value=scan_result)

        result = await plugin.cmd_vibecop_scan({"severity_threshold": "warning"})

        assert result["success"] is True
        assert result["total_findings"] == 2  # error + warning (info filtered)
        assert "summary" in result

    @pytest.mark.asyncio
    async def test_scan_with_diff_ref(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        plugin._runner.scan = AsyncMock(
            return_value={"success": True, "findings": [], "files_scanned": 0, "errors": []}
        )

        await plugin.cmd_vibecop_scan({"path": "/code", "diff_ref": "main"})

        plugin._runner.scan.assert_called_once_with(path="/code", diff_ref="main")

    @pytest.mark.asyncio
    async def test_scan_max_findings(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        findings = _make_findings(20, "error")
        plugin._runner.scan = AsyncMock(
            return_value={
                "success": True,
                "findings": findings,
                "files_scanned": 20,
                "errors": [],
            }
        )

        result = await plugin.cmd_vibecop_scan(
            {"max_findings": 5, "severity_threshold": "info"}
        )

        assert result["shown"] == 5
        assert result["total_findings"] == 20

    @pytest.mark.asyncio
    async def test_scan_failure_passthrough(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        plugin._runner.scan = AsyncMock(
            return_value={
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": ["vibecop not found"],
            }
        )

        result = await plugin.cmd_vibecop_scan({})
        assert result["success"] is False


class TestVibeCopCheckCommand:
    """Tests for the vibecop_check command handler."""

    @pytest.mark.asyncio
    async def test_check_no_files(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        result = await plugin.cmd_vibecop_check({"files": []})
        assert result["success"] is False
        assert "No files" in result["error"]

    @pytest.mark.asyncio
    async def test_check_success(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        plugin._runner.check = AsyncMock(
            return_value={
                "success": True,
                "findings": [_make_finding()],
                "files_scanned": 2,
                "errors": [],
            }
        )

        result = await plugin.cmd_vibecop_check({"files": ["a.py", "b.py"]})
        assert result["success"] is True
        assert result["total_findings"] == 1

    @pytest.mark.asyncio
    async def test_check_not_initialized(self, mock_ctx):
        plugin = VibeCopPlugin()
        plugin._runner = None
        plugin._ctx = mock_ctx
        result = await plugin.cmd_vibecop_check({"files": ["a.py"]})
        assert result["success"] is False


class TestVibeCopStatusCommand:
    """Tests for the vibecop_status command handler."""

    @pytest.mark.asyncio
    async def test_status_success(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        status = {
            "success": True,
            "installed": True,
            "version": "1.2.0",
            "node_version": "v20.0.0",
            "detectors": ["god-function", "sql-injection"],
            "errors": [],
        }
        plugin._runner.status = AsyncMock(return_value=status)

        result = await plugin.cmd_vibecop_status({})
        assert result["success"] is True
        assert result["installed"] is True

    @pytest.mark.asyncio
    async def test_status_not_initialized(self, mock_ctx):
        plugin = VibeCopPlugin()
        plugin._runner = None
        plugin._ctx = mock_ctx
        result = await plugin.cmd_vibecop_status({})
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


class TestTaskCompletedEvent:
    """Tests for the task.completed event handler."""

    @pytest.mark.asyncio
    async def test_auto_scan_disabled(self, mock_ctx):
        mock_ctx.get_config.return_value = {"auto_scan_on_complete": False}
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        await plugin._on_task_completed({"task_id": "t1", "project_id": "p1"})

        # Should not have queried workspaces
        for call in mock_ctx.execute_command.call_args_list:
            assert call.args[0] != "list_workspaces"

    @pytest.mark.asyncio
    async def test_auto_scan_no_task_id(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        await plugin._on_task_completed({"project_id": "p1"})

        # No workspace lookup should occur
        for call in mock_ctx.execute_command.call_args_list:
            assert call.args[0] != "list_workspaces"

    @pytest.mark.asyncio
    async def test_auto_scan_no_workspace(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        mock_ctx.execute_command.side_effect = [
            {"workspaces": []},  # list_workspaces
        ]

        await plugin._on_task_completed({"task_id": "t1", "project_id": "p1"})

        # Should not have emitted events
        mock_ctx.emit_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_scan_with_findings(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        mock_ctx.emit_event.reset_mock()

        ws_response = {
            "workspaces": [
                {"locked_by_task_id": "t1", "workspace_path": "/code/my-app"}
            ]
        }
        projects_response = {
            "projects": [{"id": "p1", "name": "My App"}]
        }

        call_count = 0

        async def side_effect(cmd, args=None):
            nonlocal call_count
            call_count += 1
            if cmd == "list_workspaces":
                return ws_response
            if cmd == "list_projects":
                return projects_response
            return {"success": True}

        mock_ctx.execute_command = AsyncMock(side_effect=side_effect)

        scan_result = {
            "success": True,
            "findings": [_make_finding(severity="error"), _make_finding(severity="warning")],
            "files_scanned": 5,
            "errors": [],
        }
        plugin._runner.scan = AsyncMock(return_value=scan_result)

        await plugin._on_task_completed({"task_id": "t1", "project_id": "p1"})

        # Should have emitted scan_completed and findings_detected events
        event_types = {call.args[0] for call in mock_ctx.emit_event.call_args_list}
        assert "vibecop.scan_completed" in event_types
        assert "vibecop.findings_detected" in event_types

        # Should have sent notification
        mock_ctx.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_scan_exception_handling(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        mock_ctx.execute_command.side_effect = Exception("Network error")

        # Should not raise
        await plugin._on_task_completed({"task_id": "t1", "project_id": "p1"})

    @pytest.mark.asyncio
    async def test_auto_scan_no_findings_still_notifies(self, mock_ctx):
        plugin = VibeCopPlugin()
        await plugin.initialize(mock_ctx)

        mock_ctx.execute_command.reset_mock()
        mock_ctx.emit_event.reset_mock()

        async def side_effect(cmd, args=None):
            if cmd == "list_workspaces":
                return {
                    "workspaces": [
                        {"locked_by_task_id": "t1", "workspace_path": "/code"}
                    ]
                }
            if cmd == "list_projects":
                return {"projects": [{"id": "p1", "name": "App"}]}
            return {"success": True}

        mock_ctx.execute_command = AsyncMock(side_effect=side_effect)

        plugin._runner.scan = AsyncMock(
            return_value={
                "success": True,
                "findings": [],
                "files_scanned": 3,
                "errors": [],
            }
        )

        await plugin._on_task_completed({"task_id": "t1", "project_id": "p1"})

        # Should have emitted scan_completed but not findings_detected
        event_types = {call.args[0] for call in mock_ctx.emit_event.call_args_list}
        assert "vibecop.scan_completed" in event_types
        assert "vibecop.findings_detected" not in event_types

        # Should still notify
        mock_ctx.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Plugin discovery (integration check)
# ---------------------------------------------------------------------------


class TestPluginDiscovery:
    """Verify the plugin is discoverable by the internal plugin system."""

    def test_vibecop_module_has_tool_category(self):
        from src.plugins.internal import vibecop

        assert hasattr(vibecop, "TOOL_CATEGORY")
        assert vibecop.TOOL_CATEGORY == "vibecop"

    def test_vibecop_module_has_tool_definitions(self):
        from src.plugins.internal import vibecop

        assert hasattr(vibecop, "TOOL_DEFINITIONS")
        assert len(vibecop.TOOL_DEFINITIONS) == 3

    def test_vibecop_module_has_cli_formatters(self):
        from src.plugins.internal import vibecop

        assert hasattr(vibecop, "CLI_FORMATTERS")
        assert callable(vibecop.CLI_FORMATTERS)

    def test_plugin_discoverable(self):
        """Verify discover_internal_plugins finds VibeCopPlugin."""
        from src.plugins.internal import discover_internal_plugins

        plugins = discover_internal_plugins()
        plugin_classes = {cls.__name__ for _, cls in plugins}
        assert "VibeCopPlugin" in plugin_classes

    def test_tool_definitions_collected(self):
        """Verify collect_internal_tool_definitions includes vibecop tools."""
        from src.plugins.internal import collect_internal_tool_definitions

        collected = collect_internal_tool_definitions()
        categories = {cat for cat, _ in collected}
        assert "vibecop" in categories

        vibecop_tools = next(defs for cat, defs in collected if cat == "vibecop")
        tool_names = {t["name"] for t in vibecop_tools}
        assert "vibecop_scan" in tool_names
        assert "vibecop_check" in tool_names
        assert "vibecop_status" in tool_names
