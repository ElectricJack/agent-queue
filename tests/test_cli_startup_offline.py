"""Offline and lazy-startup contracts for the ``aq`` command tree."""

from __future__ import annotations

import os
from pathlib import Path
import site
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pg_backend():
    """These tests prove DB isolation; never allocate a test database."""


def _install_fixture_entry_point(path: Path) -> None:
    """Create import metadata for an installed plugin without invoking pip."""
    (path / "fixture_plugin.py").write_text(
        """
import click
from src.plugins.base import Plugin


class FixturePlugin(Plugin):
    default_config = {"value": "default"}

    async def initialize(self, ctx):
        pass

    async def shutdown(self, ctx):
        pass

    def cli_group(self):
        plugin = self

        @click.group("fixture-plugin")
        def group():
            \"\"\"Commands supplied by the fixture plugin.\"\"\"

        @group.command()
        def show():
            \"\"\"Show the configured fixture value.\"\"\"
            click.echo(plugin.config["value"])

        return group
""".lstrip(),
        encoding="utf-8",
    )
    dist_info = path / "fixture_plugin-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-plugin\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        '[aq.plugins]\nfixture-plugin = fixture_plugin:FixturePlugin\n',
        encoding="utf-8",
    )


def _install_database_tripwire(path: Path) -> Path:
    """Fail and leave a marker if CLI startup reaches DB initialization."""
    marker = path / "database-initialized"
    (path / "sitecustomize.py").write_text(
        """
import os
from pathlib import Path

import src.database.adapters.postgresql as postgres
import src.database.engine as engine


async def forbidden(*args, **kwargs):
    Path(os.environ["AQ_DB_TRIPWIRE"]).write_text("called", encoding="utf-8")
    raise AssertionError("CLI discovery reached database initialization")


postgres.PostgreSQLDatabaseAdapter.initialize = forbidden
postgres.run_schema_setup = forbidden
postgres.run_startup_data_migrations = forbidden
engine.run_schema_setup = forbidden
engine.run_startup_data_migrations = forbidden
""".lstrip(),
        encoding="utf-8",
    )
    return marker


def _offline_env(tmp_path: Path, *, worker: bool, database_url: str | None) -> tuple[dict, Path]:
    _install_fixture_entry_point(tmp_path)
    marker = _install_database_tripwire(tmp_path)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home-without-config")
    env["AQ_API_URL"] = "http://127.0.0.1:1"
    env["AQ_DB_TRIPWIRE"] = str(marker)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), str(ROOT), site.getusersitepackages(), env.get("PYTHONPATH", "")]
    )
    for key in ("AQ_DB_SCOPE", "AQ_DATABASE_URL", "AGENT_QUEUE_DB"):
        env.pop(key, None)
    if worker:
        env["AQ_DB_SCOPE"] = "worker"
        env["AQ_DATABASE_URL"] = "aq-worker-no-direct-db://"
        env["AGENT_QUEUE_DB"] = "aq-worker-no-direct-db://"
    elif database_url is not None:
        env["AQ_DATABASE_URL"] = database_url
        env["AGENT_QUEUE_DB"] = database_url
    return env, marker


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--help"], "fixture-plugin"),
        (["--help-all"], "Show the configured fixture value"),
        (["--version"], "version 0.1.0"),
        (["schema"], "task_status"),
        (["test", "--aq-help"], "box-wide test semaphore"),
    ],
)
@pytest.mark.parametrize("worker", [False, True], ids=["unreachable-db", "worker-scope"])
def test_discovery_commands_are_offline_and_never_initialize_database(
    tmp_path: Path, args: list[str], expected: str, worker: bool
) -> None:
    database_url = "postgresql://invalid:invalid@127.0.0.1:1/missing"
    env, marker = _offline_env(tmp_path, worker=worker, database_url=database_url)

    result = subprocess.run(
        [sys.executable, "-m", "src.cli.app", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize(
    ("worker", "database_url", "warning"),
    [
        (False, None, "no database URL"),
        (False, "postgresql://invalid:invalid@127.0.0.1:1/missing", "could not load"),
        (True, None, "direct database access is not available inside a worker session"),
    ],
    ids=["missing-config", "unreachable-db", "worker-scope"],
)
def test_plugin_command_config_failure_is_bounded_visible_and_uses_defaults(
    tmp_path: Path, worker: bool, database_url: str | None, warning: str
) -> None:
    env, marker = _offline_env(tmp_path, worker=worker, database_url=database_url)

    result = subprocess.run(
        [sys.executable, "-m", "src.cli.app", "fixture-plugin", "show"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "default"
    assert warning in result.stderr
    assert "Using plugin defaults" in result.stderr
    assert not marker.exists()


def test_plugin_config_is_loaded_only_when_its_group_is_invoked(monkeypatch) -> None:
    from src.cli import app
    from src.plugins.base import Plugin

    plugin_name = "fixture-lazy-config"

    class FixturePlugin(Plugin):
        default_config = {"value": "default"}

        async def initialize(self, ctx):
            pass

        async def shutdown(self, ctx):
            pass

        def cli_group(self):
            import click

            plugin = self

            @click.group()
            def group():
                pass

            @group.command()
            def show():
                click.echo(plugin.config["value"])

            return group

    entry_point = SimpleNamespace(name=plugin_name, load=lambda: FixturePlugin)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry_point])

    with patch.object(app, "_load_plugin_config_from_db", return_value={"value": "saved"}) as read:
        app._load_plugin_cli_groups()
        help_result = CliRunner().invoke(app.cli, [plugin_name, "--help"])
        assert help_result.exit_code == 0, help_result.output
        read.assert_not_called()

        command_result = CliRunner().invoke(app.cli, [plugin_name, "show"])
        assert command_result.exit_code == 0, command_result.output
        assert command_result.output.strip() == "saved"
        read.assert_called_once_with(plugin_name)


def test_broken_plugin_entry_point_remains_discoverable_and_actionable(monkeypatch) -> None:
    from src.cli import app

    plugin_name = "fixture-broken-plugin"

    def fail_load():
        raise ImportError("fixture dependency is missing")

    entry_point = SimpleNamespace(name=plugin_name, load=fail_load)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry_point])
    app._load_plugin_cli_groups()

    help_result = CliRunner().invoke(app.cli, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert plugin_name in help_result.output

    command_result = CliRunner().invoke(app.cli, [plugin_name])
    assert command_result.exit_code == 1
    assert "fixture dependency is missing" in command_result.output
    assert "Reinstall the plugin" in command_result.output


class _Connection:
    def __init__(self, raw: str):
        self.raw = raw
        self.statement = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=lambda: self.raw)


class _ReadOnlyEngine:
    def __init__(self, raw: str):
        self.connection = _Connection(raw)
        self.disposed = False

    def connect(self):
        return self.connection

    async def dispose(self):
        self.disposed = True


async def test_plugin_config_reader_exposes_only_a_select_path(monkeypatch) -> None:
    from src.cli.client import PluginConfigReader

    engine = _ReadOnlyEngine('{"enabled": true}')
    monkeypatch.setattr(
        "src.database.engine.create_postgres_engine", lambda *args, **kwargs: engine
    )

    async with PluginConfigReader("postgresql://user:pass@db/plugin") as reader:
        assert await reader.get_config("fixture") == {"enabled": True}

    assert engine.disposed is True
    assert engine.connection.statement.is_select is True
