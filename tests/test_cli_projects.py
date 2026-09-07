"""Contract tests for the hand-crafted project CLI (api-cli plan 17)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.exceptions import CommandError


def _client(results):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _execute(command, args=None):
        value = results[command]
        if isinstance(value, Exception):
            raise value
        return value

    client.execute = AsyncMock(side_effect=_execute)
    return client


@pytest.fixture
def runner():
    return CliRunner()


def test_project_details_and_set_forward_correct_args_and_render_client_errors(runner):
    """Plan 17: exact arg forwarding, not-found exit, key coercion, JSON errors."""
    from src.cli.app import cli

    # -- details success: composes list_projects + list_tasks -------------
    client = _client({
        "list_projects": {"projects": [
            {"id": "p1", "name": "P One", "status": "ACTIVE",
             "max_concurrent_agents": 2, "credit_weight": 1.0},
        ]},
        "list_tasks": {"tasks": [
            {"status": "DEFINED"}, {"status": "DEFINED"}, {"status": "COMPLETED"},
        ]},
    })
    with patch("src.cli.projects._get_client", return_value=client):
        result = runner.invoke(cli, ["project", "details", "p1"])
    assert result.exit_code == 0, result.output
    assert "p1" in result.output and "P One" in result.output
    assert "DEFINED" in result.output
    assert client.execute.await_args_list[1].args == (
        "list_tasks", {"project_id": "p1", "include_completed": True},
    )

    # -- details unknown project: nonzero exit, no crash -------------------
    client = _client({"list_projects": {"projects": []}, "list_tasks": {"tasks": []}})
    with patch("src.cli.projects._get_client", return_value=client):
        result = runner.invoke(cli, ["project", "details", "nope"])
    assert result.exit_code == 1
    assert "Project not found" in result.output

    # -- set: key aliasing + value coercion reach the right command --------
    for argv, expected in (
        (["project", "set", "p1", "max-agents", "5"],
         ("edit_project", {"project_id": "p1", "max_concurrent_agents": 5})),
        (["project", "set", "p1", "credit-weight", "1.5"],
         ("edit_project", {"project_id": "p1", "credit_weight": 1.5})),
        (["project", "set", "p1", "channel", "123456"],
         ("set_project_channel", {"project_id": "p1", "channel_id": "123456"})),
        (["project", "set", "p1", "branch", "develop"],
         ("set_default_branch", {"project_id": "p1", "branch": "develop"})),
        (["project", "set", "p1", "budget-limit", "unlimited"],
         ("edit_project", {"project_id": "p1", "budget_limit": None})),
        (["project", "set", "p1", "default-profile", "worker-standard"],
         ("edit_project", {"project_id": "p1", "default_profile_id": "worker-standard"})),
        (["project", "set", "p1", "default-profile", "none"],
         ("edit_project", {"project_id": "p1", "default_profile_id": None})),
    ):
        client = _client({expected[0]: {"success": True}})
        with patch("src.cli.projects._get_client", return_value=client):
            result = runner.invoke(cli, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert "Updated" in result.output
        assert client.execute.await_args_list == [((expected[0], expected[1]),)]

    # -- set: unknown key rejected before any daemon call ------------------
    client = _client({})
    with patch("src.cli.projects._get_client", return_value=client):
        result = runner.invoke(cli, ["project", "set", "p1", "bogus-key", "x"])
    assert result.exit_code == 1
    assert "Unknown key" in result.output
    client.execute.assert_not_awaited()

    # -- daemon command error under --json: one parseable error envelope ---
    client = _client({
        "edit_project": CommandError("edit_project", "No project found: p1"),
    })
    with patch("src.cli.projects._get_client", return_value=client):
        result = runner.invoke(cli, ["--json", "project", "set", "p1", "name", "x"])
    assert result.exit_code == 1
    envelope = json.loads(result.output)
    assert envelope["error"]["code"] == "command_error"
    assert "No project found" in envelope["error"]["message"]
    assert envelope["data"] is None


def test_project_set_forwards_guarded_integration_configuration(runner):
    from src.cli.app import cli

    cases = (
        (
            [
                "project",
                "set",
                "p1",
                "integration-repository-id",
                "repo-1",
                "--expected-integration-generation",
                "4",
                "--reason",
                "bind exact repository",
            ],
            {
                "project_id": "p1",
                "integration_repository_id": "repo-1",
                "expected_integration_generation": 4,
                "reason": "bind exact repository",
            },
        ),
        (
            [
                "project",
                "set",
                "p1",
                "integration-policy",
                '{"version": 1}',
                "--expected-integration-generation",
                "5",
                "--reason",
                "bind reviewed policy",
            ],
            {
                "project_id": "p1",
                "hierarchical_integration_policy": {"version": 1},
                "expected_integration_generation": 5,
                "reason": "bind reviewed policy",
            },
        ),
    )
    for argv, expected in cases:
        client = _client({"edit_project": {"outcome": "configured", "generation": 5}})
        with patch("src.cli.projects._get_client", return_value=client):
            result = runner.invoke(cli, argv)
        assert result.exit_code == 0, result.output
        client.execute.assert_awaited_once_with("edit_project", expected)


def test_project_set_rejects_unguarded_or_invalid_integration_configuration(runner):
    from src.cli.app import cli

    client = _client({})
    with patch("src.cli.projects._get_client", return_value=client):
        missing_generation = runner.invoke(
            cli,
            ["project", "set", "p1", "integration-repository-id", "repo-1"],
        )
        invalid_policy = runner.invoke(
            cli,
            [
                "project",
                "set",
                "p1",
                "integration-policy",
                "not-json",
                "--expected-integration-generation",
                "1",
            ],
        )

    assert missing_generation.exit_code == 2
    assert "--expected-integration-generation" in missing_generation.output
    assert invalid_policy.exit_code == 2
    assert "valid JSON object" in invalid_policy.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("extra_argv", "extra_args"),
    [
        ([], {}),
        (
            ["--source-mode", "init", "--no-create-readme", "--create-github", "--github-owner", "acme"],
            {
                "source_mode": "init",
                "create_readme": False,
                "create_github": True,
                "github_owner": "acme",
                "github_visibility": "private",
            },
        ),
        (
            [
                "--source-mode",
                "init",
                "--create-github",
                "--github-owner",
                "acme",
                "--github-repo",
                "widgets",
                "--github-visibility",
                "public",
            ],
            {
                "source_mode": "init",
                "create_readme": True,
                "create_github": True,
                "github_owner": "acme",
                "github_repo": "widgets",
                "github_visibility": "public",
            },
        ),
        (
            ["--source-mode", "github_clone", "--github-repository", "acme/widgets"],
            {
                "source_mode": "github_clone",
                "github_repository": {"owner": "acme", "name": "widgets"},
            },
        ),
        (
            ["--source-mode", "github_clone", "--github-url", "git@github.com:acme/widgets.git"],
            {
                "source_mode": "github_clone",
                "github_url": "git@github.com:acme/widgets.git",
            },
        ),
    ],
)
def test_project_onboard_forwards_request_fields_and_prints_result(
    runner, extra_argv, extra_args
):
    from src.cli.app import cli

    response = {
        "success": True,
        "request_id": "request-123",
        "project_id": "example",
        "workspace_id": "example-primary",
        "source_type": extra_args.get("source_mode", "link"),
        "root_id": "dev",
        "relative_path": "example",
        "canonical_path": "/srv/dev/example",
        "default_branch": "main",
        "remote_url": None,
        "actions": ["project_created"],
    }
    client = _client({"onboard_project": response})
    argv = [
        "project",
        "onboard",
        "--request-id",
        "request-123",
        "--root-id",
        "dev",
        "--relative-path",
        "example",
        "--project-name",
        "Example",
        "--project-id",
        "example",
        *extra_argv,
    ]

    with patch("src.cli.projects._get_client", return_value=client):
        result = runner.invoke(cli, argv)

    assert result.exit_code == 0, result.output
    expected = {
        "request_id": "request-123",
        "source_mode": "link",
        "root_id": "dev",
        "relative_path": "example",
        "project_name": "Example",
        "project_id": "example",
        "default_branch": None,
        **extra_args,
    }
    assert client.execute.await_args_list == [(("onboard_project", expected),)]
    assert "example" in result.output
    assert "example-primary" in result.output
    assert "/srv/dev/example" in result.output
