"""Global ``aq`` options are accepted at every command position.

``docs/specs/design/aq-surface.md`` §4.0: ``--json``, ``--brief`` and
``--api-url`` mean the same thing wherever they appear — before the group,
between a group and its subcommand, or trailing after the leaf command and
its arguments.  Before this, only the prefix form parsed, so the published
``aq status --json`` failed with "no such option" while ``aq --json status``
worked.

The two documented exclusions are covered here too: passthrough commands
(``aq test``) hand their argv to a child program untouched, and a command
that declares its own option of the same name (``aq doctor --json``) keeps
its local meaning.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from src.cli.global_options import (
    GLOBAL_OPTION_NAMES,
    install_global_options,
    is_passthrough,
)


@pytest.fixture
def runner():
    return CliRunner()


def _mock_client(execute_results: dict):
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.close = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def mock_execute(command, args=None):
        result = execute_results.get(command, {})
        if isinstance(result, Exception):
            raise result
        return result

    mock_client.execute = AsyncMock(side_effect=mock_execute)
    return mock_client


def _tasks_client():
    return _mock_client(
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
                        "description": "long description that --brief drops",
                    }
                ],
                "total": 1,
            }
        }
    )


# ---------------------------------------------------------------------------
# Position independence on the real command tree
# ---------------------------------------------------------------------------


class TestPositionsAgree:
    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--json", "task", "list"], id="prefix"),
            pytest.param(["task", "--json", "list"], id="group"),
            pytest.param(["task", "list", "--json"], id="trailing"),
            pytest.param(["--json", "task", "list", "--json"], id="duplicate"),
            pytest.param(["--json", "task", "--json", "list", "--json"], id="every-position"),
        ],
    )
    def test_json_envelope_from_any_position(self, runner, argv):
        from src.cli.app import cli

        with patch("src.cli.tasks._get_client", return_value=_tasks_client()):
            result = runner.invoke(cli, argv)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == 1
        assert payload["data"][0]["id"] == "task-1"

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--json", "--brief", "task", "list"], id="both-prefix"),
            pytest.param(["task", "list", "--json", "--brief"], id="both-trailing"),
            pytest.param(["--json", "task", "list", "--brief"], id="split"),
            pytest.param(["--brief", "task", "list", "--json"], id="split-reversed"),
            pytest.param(["task", "--brief", "list", "--json"], id="group-and-leaf"),
        ],
    )
    def test_brief_composes_with_json_in_any_order(self, runner, argv):
        from src.cli.app import cli
        from src.cli.envelope import BRIEF_PROJECTIONS

        with patch("src.cli.tasks._get_client", return_value=_tasks_client()):
            result = runner.invoke(cli, argv)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert set(payload["data"][0]) == set(BRIEF_PROJECTIONS["task"])

    def test_a_leaf_flag_left_at_its_default_does_not_clear_the_prefix_form(self, runner):
        """``aq --json task list`` parses the leaf's ``--json`` as False."""
        from src.cli.app import cli

        with patch("src.cli.tasks._get_client", return_value=_tasks_client()):
            result = runner.invoke(cli, ["--json", "task", "list"])

        assert result.exit_code == 0, result.output
        json.loads(result.output)  # would raise on Rich table output

    def test_trailing_flag_after_a_positional_argument(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_show": {"id": "task-1", "title": "T", "status": "READY"}})
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "show", "task-1", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["id"] == "task-1"

    def test_status_accepts_the_published_trailing_form(self, runner):
        """The exact invocation from the audit: ``aq status --json``."""
        from src.cli.app import cli

        mock = _mock_client({"get_status": {"tasks": {"by_status": {"ready": 1}}, "projects": 0}})
        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        assert "no such option" not in result.output

    def test_trailing_api_url_reaches_the_client(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"list_tasks": {"tasks": [], "total": 0}})
        with patch("src.cli.tasks._get_client", return_value=mock) as get_client:
            result = runner.invoke(
                cli, ["task", "list", "--api-url", "http://example:9999", "--json"]
            )

        assert result.exit_code == 0, result.output
        assert get_client.call_args.args[0] == "http://example:9999"

    def test_explicit_prefix_api_url_wins_over_the_environment(self, runner, monkeypatch):
        """The injected copies carry no ``envvar``, so they cannot clobber it."""
        from src.cli.app import cli

        monkeypatch.setenv("AGENT_QUEUE_API_URL", "http://from-env:1111")
        mock = _mock_client({"list_tasks": {"tasks": [], "total": 0}})
        with patch("src.cli.tasks._get_client", return_value=mock) as get_client:
            result = runner.invoke(cli, ["--api-url", "http://explicit:2222", "task", "list"])

        assert result.exit_code == 0, result.output
        assert get_client.call_args.args[0] == "http://explicit:2222"


# ---------------------------------------------------------------------------
# Exclusions: passthrough argv and locally-declared options
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_aq_test_hands_json_to_pytest(self, runner):
        from src.cli.app import cli

        result = runner.invoke(
            cli, ["test", "--aq-dry-run", "tests/test_cli_global_options.py", "--json"]
        )

        assert result.exit_code == 0, result.output
        assert "--json" in result.output
        assert "schema_version" not in result.output

    def test_passthrough_commands_declare_no_global_options(self):
        from src.cli.app import cli

        for path, cmd in _walk_commands(cli):
            if not is_passthrough(cmd):
                continue
            declared = {opt for param in cmd.params for opt in param.opts}
            assert not (declared & set(GLOBAL_OPTION_NAMES)), path

    def test_doctor_keeps_its_own_json_option(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["doctor", "--help"])

        assert result.exit_code == 0, result.output
        assert "Emit the versioned JSON envelope." in result.output
        assert result.output.count("--json") == 1

    def test_a_dash_dash_boundary_is_not_consumed(self):
        """Click's end-of-options marker still wins over an injected flag."""
        seen: dict = {}

        @click.group()
        @click.pass_context
        def root(ctx):
            ctx.ensure_object(dict)

        @root.command("run")
        @click.argument("argv", nargs=-1, type=click.UNPROCESSED)
        @click.pass_context
        def run(ctx, argv):
            seen["argv"] = list(argv)
            seen["json"] = bool((ctx.obj or {}).get("json"))

        install_global_options(root)
        result = CliRunner().invoke(root, ["run", "--", "--json"])

        assert result.exit_code == 0, result.output
        assert seen == {"argv": ["--json"], "json": False}


# ---------------------------------------------------------------------------
# Whole-tree ratchet: nested groups, auto-generated commands, help
# ---------------------------------------------------------------------------


def _walk_commands(group: click.Group, prefix: str = "aq"):
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None:
            continue
        path = f"{prefix} {name}"
        yield path, cmd
        if isinstance(cmd, click.Group):
            yield from _walk_commands(cmd, path)


class TestWholeTree:
    def test_every_command_and_group_accepts_the_global_options(self):
        from src.cli.app import cli

        missing: list[str] = []
        for path, cmd in _walk_commands(cli):
            if is_passthrough(cmd):
                continue
            declared = {opt for param in cmd.params for opt in param.opts}
            for opt in GLOBAL_OPTION_NAMES:
                if opt not in declared:
                    missing.append(f"{path} {opt}")
        assert not missing, f"commands without position-independent globals: {missing[:20]}"

    def test_nested_group_help_lists_the_globals(self, runner):
        from src.cli.app import cli

        for argv in (["task", "--help"], ["task", "list", "--help"], ["session", "--help"]):
            result = runner.invoke(cli, argv)
            assert result.exit_code == 0, result.output
            assert "--json" in result.output, argv
            assert "--brief" in result.output, argv

    def test_a_group_accepts_the_flag_before_its_subcommand(self, runner):
        from src.cli.app import cli

        with patch("src.cli.tasks._get_client", return_value=_tasks_client()):
            result = runner.invoke(cli, ["task", "--json", "list"])

        assert result.exit_code == 0, result.output
        json.loads(result.output)

    def test_a_daemon_first_import_still_gets_the_globals(self):
        """Import order must not decide the grammar.

        Importing ``src.cli.daemon`` first enters ``app.py`` through the
        circular ``from .app import cli``, so ``app.py`` — install included —
        finishes before ``daemon.py`` registers ``aq start|stop|restart``.
        ``AQGroup.add_command`` is what covers those late arrivals.
        """
        import subprocess
        import sys

        code = (
            "import src.cli.daemon\n"
            "from src.cli.app import cli\n"
            "import click\n"
            "ctx = click.Context(cli)\n"
            "for name in ('start', 'stop', 'restart'):\n"
            "    cmd = cli.get_command(ctx, name)\n"
            "    opts = {o for p in cmd.params for o in p.opts}\n"
            "    assert '--json' in opts and '--brief' in opts, (name, sorted(opts))\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
        )

        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_help_all_still_renders(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["--help-all"])

        assert result.exit_code == 0, result.output
        assert "aq task list" in result.output


# ---------------------------------------------------------------------------
# install_global_options() itself
# ---------------------------------------------------------------------------


class TestInstaller:
    def _tree(self):
        @click.group()
        @click.pass_context
        def root(ctx):
            ctx.ensure_object(dict)

        @root.group("outer")
        def outer():
            """Outer."""

        @outer.command("leaf")
        @click.pass_context
        def leaf(ctx):
            click.echo(json.dumps({k: ctx.obj.get(k) for k in ("json", "brief", "api_url")}))

        return root

    def test_deeply_nested_leaf_records_on_the_root_context(self):
        root = self._tree()
        install_global_options(root)

        result = CliRunner().invoke(root, ["outer", "leaf", "--json", "--brief"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"json": True, "brief": True, "api_url": None}

    def test_running_twice_does_not_duplicate_options(self):
        root = self._tree()
        install_global_options(root)
        before = [len(cmd.params) for _, cmd in _walk_commands(root)]
        install_global_options(root)
        after = [len(cmd.params) for _, cmd in _walk_commands(root)]

        assert before == after

    def test_a_local_option_of_the_same_name_is_left_alone(self):
        @click.group()
        def root():
            """Root."""

        @root.command("local")
        @click.option("--json", "as_json", is_flag=True, help="Local meaning.")
        def local(as_json):
            click.echo(f"local={as_json}")

        install_global_options(root)
        cmd = root.get_command(click.Context(root), "local")

        json_params = [p for p in cmd.params if "--json" in p.opts]
        assert len(json_params) == 1
        assert json_params[0].expose_value is True
        assert {"--brief", "--api-url"} <= {opt for p in cmd.params for opt in p.opts}
