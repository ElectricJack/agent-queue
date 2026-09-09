"""Payload and exit-contract conformance for the generated CLI surface."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from src.cli.auto_commands import NullableParam, StructuredParam
from src.cli.inventory import _walk_leaves


@pytest.fixture(autouse=True)
def _pg_backend():
    """Transport is mocked; no command may reach a database."""


def _generated_cases():
    from src.cli.app import cli

    return [
        pytest.param(path, command, id=path.removeprefix("aq ").replace(" ", "/"))
        for path, command in _walk_leaves(cli)
        if getattr(command, "_aq_registration", None) == "generated"
    ]


def _text_value(param_type: click.ParamType) -> str:
    if isinstance(param_type, StructuredParam):
        return "[]" if param_type.kind == "array" else "{}"
    if isinstance(param_type, NullableParam):
        return _text_value(param_type.inner)
    if isinstance(param_type, click.Choice):
        return str(next(choice for choice in param_type.choices if choice != "null"))
    if param_type.name == "integer":
        return "1"
    if param_type.name == "float":
        return "1.5"
    return "value"


def _required_argv(command: click.Command) -> list[str]:
    argv: list[str] = []
    for param in command.params:
        if not param.required:
            continue
        assert isinstance(param, click.Option), "generated commands use schema-backed options"
        if param.type.name == "boolean":
            argv.append(param.opts[0])
        else:
            argv.extend([param.opts[0], _text_value(param.type)])
    return argv


def _client(result: dict | Exception):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        client.execute.return_value = result
    return client


# Broad enough for every registered formatter; the test is about transport,
# not Rich rendering, but registered formatters still receive the response.
SUCCESS_PAYLOAD = {
    "id": "x",
    "task_id": "x",
    "project_id": "p",
    "name": "x",
    "title": "x",
    "status": "READY",
    "state": "idle",
    "priority": 1,
    "description": "x",
    "tasks": [],
    "projects": [],
    "agents": [],
    "workspaces": [],
    "results": [],
    "items": [],
    "total": 0,
    "depends_on": [],
    "blocks": [],
    "profiles": [],
    "formulas": [],
    "servers": [],
    "pools": [],
    "samples": [],
    "classes": [],
    "triggers": [],
}


@pytest.mark.parametrize("path,command", _generated_cases())
def test_every_generated_leaf_dispatches_required_and_omits_optional(path, command):
    """The historical help-only audit now executes every generated callback."""
    client = _client(SUCCESS_PAYLOAD)
    argv = _required_argv(command)
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(command, argv, obj={})
    assert result.exit_code == 0, f"{path}: {result.output}\n{result.exception!r}"
    client.execute.assert_awaited_once()
    backend, payload = client.execute.await_args.args
    assert backend == getattr(command, "_aq_backend_command")
    required = {param.name for param in command.params if param.required}
    optional = {param.name for param in command.params if not param.required}
    assert required <= set(payload)
    assert optional.isdisjoint(payload)


def test_required_structured_optional_and_explicit_null_reach_the_transport():
    from src.cli.app import cli

    client = _client(SUCCESS_PAYLOAD)
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "system",
                "update-config",
                "--section",
                "swarm",
                "--data",
                '{"enabled": true}',
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert client.execute.await_args.args == (
        "update_config",
        {"section": "swarm", "data": {"enabled": True}, "dry_run": True},
    )

    client = _client(SUCCESS_PAYLOAD)
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            ["pool", "scale", "--project-id", "p", "--profile-id", "worker", "--max", "null"],
        )
    assert result.exit_code == 0, result.output
    assert client.execute.await_args.args[1]["max"] is None


def test_required_and_malformed_structured_values_are_usage_errors_without_dispatch():
    from src.cli.app import cli

    client = _client(SUCCESS_PAYLOAD)
    with patch("src.cli.app._get_client", return_value=client):
        missing = CliRunner().invoke(cli, ["memory", "save"])
        malformed = CliRunner().invoke(
            cli,
            [
                "system",
                "delivery-promote",
                "--operation-key",
                "op",
                "--source-task-id",
                "t1",
                "--source-head",
                "abc",
                "--source-base",
                "def",
                "--expected-target",
                "ghi",
                "--fence",
                "{bad",
            ],
        )
    assert missing.exit_code == 2
    assert "Missing option '--content'" in missing.output
    assert malformed.exit_code == 2
    assert "not valid JSON" in malformed.output
    client.execute.assert_not_awaited()


def test_generated_command_error_is_json_and_uses_the_documented_exit_code():
    from src.cli.app import cli
    from src.cli.exceptions import ScopeDeniedError

    client = _client(ScopeDeniedError("pool_status", "session cannot inspect pools"))
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "pool", "status"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "out_of_scope"
    assert payload["data"] is None


def test_aq_test_reserves_aq_help_and_passes_pytest_help_through(monkeypatch, tmp_path):
    from src.cli.app import cli

    monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "missing.yaml"))
    wrapper_help = CliRunner().invoke(cli, ["test", "--aq-help"])
    assert wrapper_help.exit_code == 0
    assert "Run pytest under the box-wide test semaphore" in wrapper_help.output

    pytest_help = CliRunner().invoke(cli, ["test", "--aq-dry-run", "--help"])
    assert pytest_help.exit_code == 0
    assert "pytest" in pytest_help.output
    assert pytest_help.output.rstrip().endswith("--help")

    short_help = CliRunner().invoke(cli, ["test", "--aq-dry-run", "-h"])
    assert short_help.exit_code == 0
    assert short_help.output.rstrip().endswith("-h")
