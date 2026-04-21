"""Unit tests for the AgentQueue CLI.

Tests CLI commands, adapters, auto-generated commands, and formatters.
The REST client is mocked via httpx so no daemon is needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from src.cli.adapters import (
    DictProxy,
    agent_proxy,
    project_proxy,
    task_proxy,
)
from src.cli.auto_commands import EXCLUDED
from src.cli.client import CLIClient
from src.cli.exceptions import CommandError, DaemonNotRunningError
from src.cli.formatters import (
    format_agent_table,
    format_project_table,
    format_status_overview,
    format_task_detail,
    format_task_table,
)
from src.cli.styles import STATUS_ICONS, STATUS_STYLES, priority_style


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


# Mock response helpers


def _mock_response(data, status_code=200):
    """Create a mock httpx response."""
    mock = AsyncMock()
    mock.status_code = status_code
    mock.json.return_value = data
    mock.raise_for_status = lambda: None
    return mock


def _ok(result):
    """Wrap a result dict in the API success envelope."""
    return {"ok": True, "result": result}


def _err(msg):
    """Wrap an error in the API error envelope."""
    return {"ok": False, "error": msg}


# ---------------------------------------------------------------------------
# DictProxy tests
# ---------------------------------------------------------------------------


class TestDictProxy:
    def test_attribute_access(self):
        p = DictProxy({"name": "Alice", "age": 30})
        assert p.name == "Alice"
        assert p.age == 30

    def test_missing_returns_none(self):
        p = DictProxy({"name": "Alice"})
        assert p.missing_key is None

    def test_aliases(self):
        p = DictProxy({"assigned_agent": "ws-1"}, aliases={"assigned_agent_id": "assigned_agent"})
        assert p.assigned_agent_id == "ws-1"
        assert p.assigned_agent == "ws-1"

    def test_get_method(self):
        p = DictProxy({"key": "value"})
        assert p.get("key") == "value"
        assert p.get("missing", "default") == "default"

    def test_repr(self):
        p = DictProxy({"x": 1})
        assert "DictProxy" in repr(p)


# ---------------------------------------------------------------------------
# Typed proxy tests
# ---------------------------------------------------------------------------


class TestTaskProxy:
    def test_status_string(self):
        t = task_proxy({"status": "IN_PROGRESS", "title": "Test"})
        assert t.status == "IN_PROGRESS"

    def test_status_normalised_uppercase(self):
        t = task_proxy({"status": "in_progress", "title": "Test"})
        assert t.status == "IN_PROGRESS"

    def test_task_type_string(self):
        t = task_proxy({"status": "READY", "task_type": "feature"})
        assert t.task_type == "feature"

    def test_task_type_none(self):
        t = task_proxy({"status": "READY", "task_type": None})
        assert t.task_type is None

    def test_assigned_agent_alias(self):
        t = task_proxy({"status": "READY", "assigned_agent": "ws-1"})
        assert t.assigned_agent_id == "ws-1"

    def test_optional_fields_default_none(self):
        t = task_proxy({"status": "READY"})
        assert t.branch_name is None
        assert t.pr_url is None
        assert t.parent_task_id is None


class TestProjectProxy:
    def test_status_string(self):
        p = project_proxy({"status": "ACTIVE", "name": "Test"})
        assert p.status == "ACTIVE"

    def test_defaults(self):
        p = project_proxy({"status": "ACTIVE"})
        assert p.total_tokens_used == 0
        assert p.discord_channel_id is None

    def test_equality_comparison(self):
        """Formatters compare project.status == 'ACTIVE'."""
        p = project_proxy({"status": "ACTIVE"})
        assert p.status == "ACTIVE"


class TestAgentProxy:
    def test_state_normalised_uppercase(self):
        """CommandHandler returns lowercase 'busy'/'idle'."""
        a = agent_proxy({"state": "busy", "workspace_id": "ws-1", "name": "Agent 1"})
        assert a.state == "BUSY"

    def test_id_alias(self):
        a = agent_proxy({"workspace_id": "ws-1", "state": "idle"})
        assert a.id == "ws-1"

    def test_defaults(self):
        a = agent_proxy({"state": "idle"})
        assert a.session_tokens_used == 0
        assert a.agent_type == "claude"


# ---------------------------------------------------------------------------
# Formatter compatibility tests (proxied dicts through real formatters)
# ---------------------------------------------------------------------------


class TestFormatterCompatibility:
    """Verify that proxied dicts work with the existing formatters."""

    def test_format_task_table(self):
        tasks = [
            task_proxy(
                {
                    "id": "task-1",
                    "project_id": "proj",
                    "status": "IN_PROGRESS",
                    "priority": 100,
                    "task_type": "feature",
                    "title": "Test task",
                    "assigned_agent": "ws-1",
                }
            ),
        ]
        table = format_task_table(tasks, title="Test")
        assert table is not None

    def test_format_task_detail(self):
        t = task_proxy(
            {
                "id": "task-1",
                "project_id": "proj",
                "status": "IN_PROGRESS",
                "priority": 100,
                "task_type": "bugfix",
                "title": "Fix bug",
                "assigned_agent": None,
                "description": "A bug fix",
                "requires_approval": False,
            }
        )
        panel = format_task_detail(t, deps_on=["dep-1"], dependents=["block-1"])
        assert panel is not None

    def test_format_task_detail_with_subtask_stats(self):
        t = task_proxy(
            {
                "id": "task-1",
                "project_id": "proj",
                "status": "IN_PROGRESS",
                "priority": 100,
                "title": "Parent task",
                "description": "Has subtasks",
            }
        )
        panel = format_task_detail(t, subtask_stats=(3, 5))
        assert panel is not None

    def test_format_agent_table(self):
        agents = [
            agent_proxy(
                {
                    "workspace_id": "ws-1",
                    "name": "Agent 1",
                    "state": "busy",
                    "current_task_id": "task-1",
                }
            ),
            agent_proxy(
                {
                    "workspace_id": "ws-2",
                    "name": "Agent 2",
                    "state": "idle",
                    "current_task_id": None,
                }
            ),
        ]
        table = format_agent_table(agents)
        assert table is not None

    def test_format_project_table(self):
        projects = [
            project_proxy(
                {
                    "id": "proj",
                    "name": "Test Project",
                    "status": "ACTIVE",
                    "discord_channel_id": "123456",
                    "max_concurrent_agents": 2,
                }
            ),
        ]
        table = format_project_table(projects)
        assert table is not None

    def test_format_status_overview(self):
        projects = [project_proxy({"id": "p", "name": "P", "status": "ACTIVE"})]
        task_counts = {"IN_PROGRESS": 2, "READY": 5, "COMPLETED": 10}
        panel = format_status_overview(projects, task_counts)
        assert panel is not None


# ---------------------------------------------------------------------------
# Formatter registry tests
# ---------------------------------------------------------------------------


class TestFormatterRegistry:
    def test_formatters_registered(self):
        from src.cli.formatter_registry import FORMATTERS

        expected = {
            "list_tasks",
            "get_task",
            "list_agents",
            "list_projects",
        }
        assert expected.issubset(set(FORMATTERS.keys()))

    def test_apply_formatter_list(self):
        from rich.console import Console
        from io import StringIO
        from src.cli.formatter_registry import apply_formatter

        buf = StringIO()
        console = Console(file=buf, width=120)
        result = {
            "tasks": [
                {
                    "id": "t-1",
                    "project_id": "p",
                    "title": "Test",
                    "status": "READY",
                    "priority": 100,
                    "assigned_agent": None,
                    "task_type": None,
                },
            ],
            "total": 1,
        }
        assert apply_formatter("list_tasks", result, console) is True
        output = buf.getvalue()
        assert "Test" in output

    def test_apply_formatter_detail(self):
        from rich.console import Console
        from io import StringIO
        from src.cli.formatter_registry import apply_formatter

        buf = StringIO()
        console = Console(file=buf, width=120)
        result = {
            "id": "t-1",
            "project_id": "p",
            "title": "Detail Test",
            "status": "IN_PROGRESS",
            "priority": 100,
            "description": "A task",
            "assigned_agent": None,
            "depends_on": [],
            "blocks": [],
        }
        assert apply_formatter("get_task", result, console) is True
        output = buf.getvalue()
        assert "Detail Test" in output

    def test_apply_formatter_unknown(self):
        from rich.console import Console
        from io import StringIO
        from src.cli.formatter_registry import apply_formatter

        buf = StringIO()
        console = Console(file=buf)
        assert apply_formatter("unknown_command", {}, console) is False

    def test_apply_formatter_empty_list(self):
        from rich.console import Console
        from io import StringIO
        from src.cli.formatter_registry import apply_formatter

        buf = StringIO()
        console = Console(file=buf, width=120)
        result = {"tasks": [], "total": 0}
        assert apply_formatter("list_tasks", result, console) is True
        output = buf.getvalue()
        assert "No tasks" in output


# ---------------------------------------------------------------------------
# CLIClient tests
# ---------------------------------------------------------------------------


class TestCLIClient:
    """Tests for CLIClient routing through typed API endpoints."""

    @staticmethod
    def _mock_httpx_for_typed(status_code: int, json_body: dict):
        """Create a mock httpx.AsyncClient that returns a typed response.

        The mock handles both the health check GET and the typed POST endpoint.
        """
        import httpx

        mock_http = AsyncMock(spec=httpx.AsyncClient)

        health_resp = MagicMock(spec=httpx.Response)
        health_resp.json = lambda: {"status": "ok"}
        health_resp.raise_for_status = lambda: None

        typed_resp = MagicMock(spec=httpx.Response)
        typed_resp.status_code = status_code
        typed_resp.json = lambda: json_body
        typed_resp.content = b""
        typed_resp.headers = {}

        mock_http.get.return_value = health_resp
        mock_http.request.return_value = typed_resp
        # Also mock .post for fallback path
        fallback_resp = MagicMock(spec=httpx.Response)
        fallback_resp.json = lambda: _ok(json_body)
        mock_http.post.return_value = fallback_resp
        mock_http.aclose = AsyncMock()

        return mock_http

    @pytest.mark.asyncio
    async def test_execute_success(self):
        """Execute routes through /api/execute and returns parsed result."""
        mock_http = self._mock_httpx_for_typed(200, {"tasks": [], "total": 0})

        with patch("src.cli.client.httpx.AsyncClient", return_value=mock_http):
            client = CLIClient(base_url="http://localhost:8081")
            await client.connect()
            result = await client.execute("list_tasks", {"project_id": "test"})
            assert result["tasks"] == []
            assert result["total"] == 0
            mock_http.post.assert_called_once()
            await client.close()

    @pytest.mark.asyncio
    async def test_execute_error(self):
        """Execute raises CommandError when server returns error."""
        import httpx

        mock_http = self._mock_httpx_for_typed(200, {})
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.json = lambda: {"ok": False, "error": "Task not found"}
        mock_http.post.return_value = error_resp

        with patch("src.cli.client.httpx.AsyncClient", return_value=mock_http):
            client = CLIClient(base_url="http://localhost:8081")
            await client.connect()
            with pytest.raises(CommandError, match="Task not found"):
                await client.execute("get_task", {"task_id": "nope"})
            await client.close()

    @pytest.mark.asyncio
    async def test_execute_uses_generic_endpoint(self):
        """All commands route through /api/execute."""
        mock_http = self._mock_httpx_for_typed(200, {"custom": "result"})
        import httpx

        fallback_resp = MagicMock(spec=httpx.Response)
        fallback_resp.json = lambda: _ok({"custom": "result"})
        mock_http.post.return_value = fallback_resp

        with patch("src.cli.client.httpx.AsyncClient", return_value=mock_http):
            client = CLIClient(base_url="http://localhost:8081")
            await client.connect()
            result = await client.execute("some_command", {"x": 1})
            assert result["custom"] == "result"
            mock_http.post.assert_called_once()
            await client.close()


# ---------------------------------------------------------------------------
# Styles tests (unchanged from original)
# ---------------------------------------------------------------------------


class TestStyles:
    def test_status_icons_complete(self):
        expected = {
            "DEFINED",
            "READY",
            "ASSIGNED",
            "IN_PROGRESS",
            "WAITING_INPUT",
            "PAUSED",
            "VERIFYING",
            "AWAITING_APPROVAL",
            "AWAITING_PLAN_APPROVAL",
            "COMPLETED",
            "FAILED",
            "BLOCKED",
        }
        assert expected.issubset(set(STATUS_ICONS.keys()))

    def test_status_styles_complete(self):
        assert set(STATUS_ICONS.keys()) == set(STATUS_STYLES.keys())

    def test_priority_style_tiers(self):
        assert priority_style(200) == "bold red"
        assert priority_style(150) == "bold yellow"
        assert priority_style(100) == "white"
        assert priority_style(10) == "dim white"


# ---------------------------------------------------------------------------
# Auto-generated commands tests
# ---------------------------------------------------------------------------


class TestAutoCommands:
    def test_no_cmd_group(self):
        """The flat 'cmd' group should no longer exist."""
        from src.cli.app import cli

        assert "cmd" not in cli.commands

    def test_category_groups_exist(self):
        """Each tool_registry category should have a CLI group."""
        from src.cli.app import cli

        expected = {
            "git",
            "memory",
            "note",
            "file",
            "system",
            "task",
            "agent",
            "project",
            "plugin",
        }
        actual = set(cli.commands.keys())
        for group in expected:
            assert group in actual, f"Missing CLI group: {group}"

    def test_git_group_has_commands(self):
        """The git group should have auto-generated commands."""
        from src.cli.app import cli

        git_group = cli.commands.get("git")
        assert git_group is not None
        assert len(git_group.commands) > 10
        # Prefix should be stripped: git_commit → commit
        assert "commit" in git_group.commands

    def test_task_group_merges_handcrafted_and_auto(self):
        """Task group should contain both hand-crafted and auto-generated commands."""
        from src.cli.app import cli

        task_group = cli.commands.get("task")
        assert task_group is not None
        # Hand-crafted (interactive commands)
        assert "create" in task_group.commands
        assert "approve" in task_group.commands
        assert "search" in task_group.commands
        # Auto-generated (from task category + formatter registry)
        assert "list" in task_group.commands
        assert "get" in task_group.commands
        assert "archive" in task_group.commands
        assert "skip" in task_group.commands

    def test_handcrafted_create_has_wizard_options(self):
        """Hand-crafted create should have interactive wizard options."""
        from src.cli.app import cli

        task_group = cli.commands.get("task")
        create_cmd = task_group.commands.get("create")
        assert create_cmd is not None
        param_names = {p.name for p in create_cmd.params}
        assert "title" in param_names
        assert "project" in param_names

    def test_excluded_not_present(self):
        """Dangerous commands should not appear in any group."""
        from src.cli.app import cli

        all_cmd_names: set[str] = set()
        for group_name, group in cli.commands.items():
            if hasattr(group, "commands"):
                all_cmd_names.update(group.commands.keys())
        for ex in EXCLUDED:
            click_name = ex.replace("_", "-")
            assert click_name not in all_cmd_names, f"{ex} should be excluded"

    def test_auto_command_help(self, runner):
        """Auto-generated command should have --help from JSON Schema."""
        from src.cli.app import cli

        # memory_search is now under aq memory as "search"
        result = runner.invoke(cli, ["memory", "search", "--help"])
        assert result.exit_code == 0
        assert "--project-id" in result.output
        assert "--query" in result.output

    def test_prefix_stripping(self):
        """Category prefixes/suffixes should be stripped from command names."""
        from src.cli.auto_commands import _strip_category_prefix

        assert _strip_category_prefix("git_commit", "git") == "commit"
        assert _strip_category_prefix("memory_search", "memory") == "search"
        assert _strip_category_prefix("compact_memory", "memory") == "compact"
        assert _strip_category_prefix("archive_task", "task") == "archive"
        assert _strip_category_prefix("fire_hook", "hooks") == "fire"
        assert _strip_category_prefix("get_task_result", "task") == "get_result"


# ---------------------------------------------------------------------------
# CLI command integration tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestCLICommands:
    """Test CLI commands with mocked REST API responses."""

    def _mock_client(self, execute_results: dict):
        """Create a mock CLIClient context manager."""
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.close = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def mock_execute(command, args=None):
            if command in execute_results:
                result = execute_results[command]
                if isinstance(result, Exception):
                    raise result
                return result
            return {}

        mock_client.execute = AsyncMock(side_effect=mock_execute)
        return mock_client

    def test_task_list_with_formatter(self, runner):
        """Auto-generated list_tasks should use Rich formatter, not raw JSON."""
        from src.cli.app import cli

        mock = self._mock_client(
            {
                "list_tasks": {
                    "tasks": [
                        {
                            "id": "task-1",
                            "project_id": "proj",
                            "title": "Test task",
                            "status": "IN_PROGRESS",
                            "priority": 100,
                            "task_type": "feature",
                            "assigned_agent": "ws-1",
                        },
                    ],
                    "total": 1,
                },
            }
        )

        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "list"])
            assert result.exit_code == 0
            assert "Test task" in result.output
            # Should NOT be raw JSON (formatter should render a table)
            assert '"task-1"' not in result.output

    def test_task_get_with_formatter(self, runner):
        """Auto-generated get_task should use Rich detail formatter."""
        from src.cli.app import cli

        mock = self._mock_client(
            {
                "get_task": {
                    "id": "task-1",
                    "project_id": "proj",
                    "title": "Test task",
                    "status": "IN_PROGRESS",
                    "priority": 100,
                    "description": "A test task",
                    "assigned_agent": None,
                    "task_type": "feature",
                    "requires_approval": False,
                    "depends_on": [],
                    "blocks": [],
                },
            }
        )

        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "get", "--task-id", "task-1"])
            assert result.exit_code == 0
            assert "Test task" in result.output

    def test_task_create_with_profile_flag(self, runner):
        """--profile is passed through as profile_id in create_task args."""
        from src.cli.app import cli

        captured_args = {}

        async def mock_execute(command, args=None):
            if command == "create_task":
                captured_args.update(args or {})
                return {"created": "task-42", "title": args.get("title", "")}
            return {}

        mock = self._mock_client({})
        mock.execute = AsyncMock(side_effect=mock_execute)

        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task",
                    "create",
                    "--project",
                    "proj",
                    "--title",
                    "Pick a model",
                    "--description",
                    "test task",
                    "--profile",
                    "claude-opus",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured_args["profile_id"] == "claude-opus"
        assert captured_args["project_id"] == "proj"
        assert captured_args["title"] == "Pick a model"

    def test_task_create_with_agent_type_flag(self, runner):
        """--agent-type is passed through as agent_type in create_task args."""
        from src.cli.app import cli

        captured_args = {}

        async def mock_execute(command, args=None):
            if command == "create_task":
                captured_args.update(args or {})
                return {"created": "task-43", "title": args.get("title", "")}
            return {}

        mock = self._mock_client({})
        mock.execute = AsyncMock(side_effect=mock_execute)

        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task",
                    "create",
                    "--project",
                    "proj",
                    "--title",
                    "T",
                    "--description",
                    "D",
                    "--agent-type",
                    "claude-code",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured_args["agent_type"] == "claude-code"

    def test_task_create_without_profile_flag_omits_field(self, runner):
        """When --profile is not given, profile_id is absent from create_task args."""
        from src.cli.app import cli

        captured_args = {}

        async def mock_execute(command, args=None):
            if command == "create_task":
                captured_args.update(args or {})
                return {"created": "task-44", "title": args.get("title", "")}
            return {}

        mock = self._mock_client({})
        mock.execute = AsyncMock(side_effect=mock_execute)

        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task",
                    "create",
                    "--project",
                    "proj",
                    "--title",
                    "T",
                    "--description",
                    "D",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "profile_id" not in captured_args
        assert "agent_type" not in captured_args

    def test_project_list_with_formatter(self, runner):
        """Auto-generated list_projects should use Rich formatter."""
        from src.cli.app import cli

        mock = self._mock_client(
            {
                "list_projects": {
                    "projects": [
                        {
                            "id": "proj",
                            "name": "Test",
                            "status": "ACTIVE",
                            "max_concurrent_agents": 2,
                        },
                    ],
                },
            }
        )

        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(cli, ["project", "list"])
            assert result.exit_code == 0
            assert "Test" in result.output


# ---------------------------------------------------------------------------
# Daemon command tests
# ---------------------------------------------------------------------------


class TestDaemonCommands:
    def test_start_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start the agent-queue daemon" in result.output

    def test_stop_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop the agent-queue daemon" in result.output

    def test_restart_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart the agent-queue daemon" in result.output

    def test_logs_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["logs", "--help"])
        assert result.exit_code == 0
        assert "daemon logs" in result.output.lower()

    def test_read_pid_no_file(self, tmp_path):
        from src.cli.daemon import _read_pid

        with patch("src.cli.daemon.PID_FILE", str(tmp_path / "nonexistent.pid")):
            assert _read_pid() is None

    def test_read_pid_stale(self, tmp_path):
        """Stale PID file (process not running) should return None and clean up."""
        from src.cli.daemon import _read_pid

        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("999999999")  # PID that almost certainly doesn't exist
        with patch("src.cli.daemon.PID_FILE", str(pid_file)):
            assert _read_pid() is None
            assert not pid_file.exists()  # Should have been cleaned up

    def test_is_daemon_running_false(self):
        from src.cli.daemon import is_daemon_running

        with patch("src.cli.daemon._find_daemon_pid", return_value=None):
            assert is_daemon_running() is False

    def test_is_daemon_running_true(self):
        from src.cli.daemon import is_daemon_running

        with patch("src.cli.daemon._find_daemon_pid", return_value=12345):
            assert is_daemon_running() is True

    def test_stop_not_running(self, runner):
        from src.cli.app import cli

        with patch("src.cli.daemon._find_daemon_pid", return_value=None):
            result = runner.invoke(cli, ["stop"])
            assert result.exit_code == 0
            assert "not running" in result.output


# ---------------------------------------------------------------------------
# Error handling with daemon-start prompt tests
# ---------------------------------------------------------------------------


class TestDaemonNotRunningPrompt:
    """Test that _handle_errors offers to start the daemon."""

    def test_offers_to_start_on_connection_error(self, runner):
        """When daemon is down and user says 'n', should exit cleanly."""
        from src.cli.app import cli

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(
            side_effect=DaemonNotRunningError("http://localhost:8081")
        )
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.cli.app._get_client", return_value=mock_client):
            result = runner.invoke(cli, ["task", "list"], input="n\n")
            assert result.exit_code == 1
            assert "not running" in result.output.lower()
            assert "aq start" in result.output

    def test_starts_daemon_on_yes(self, runner):
        """When user says 'y', should attempt to start and retry."""
        from src.cli.app import cli

        call_count = 0

        # First call raises, second call succeeds (after daemon start)
        async def mock_aenter():
            nonlocal call_count, mock_client
            call_count += 1
            if call_count == 1:
                raise DaemonNotRunningError("http://localhost:8081")
            return mock_client

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(side_effect=mock_aenter)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.execute = AsyncMock(
            return_value={
                "display_mode": "flat",
                "tasks": [],
                "total": 0,
                "hidden_completed": 0,
                "filtered": True,
            }
        )

        with (
            patch("src.cli.app._get_client", return_value=mock_client),
            patch("src.cli.daemon.start_daemon", return_value=True),
        ):
            result = runner.invoke(cli, ["task", "list"], input="y\n")
            assert result.exit_code == 0
