"""Contract tests for the hand-crafted system-config CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


def _client(results):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=lambda command, args: results[command])
    return client


@pytest.fixture
def runner():
    return CliRunner()


def test_config_set_nested_scalar_dry_run_does_not_write_and_reports_effective_change(runner):
    """Dropping dotted YAML parsing would send a different update to the daemon."""
    from src.cli.app import cli

    client = _client(
        {
            "get_config": {"config": {"scheduling": {"enabled": False}}},
            "update_config": {"dry_run": True},
        }
    )
    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(
            cli, ["system", "config", "set", "scheduling.enabled=true", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    assert "would set" in result.output
    assert client.execute.await_args_list[1].args == (
        "update_config",
        {"section": "scheduling", "data": {"enabled": True}, "dry_run": True},
    )


def test_config_set_rejects_malformed_assignment_without_touching_config(runner):
    """Malformed keys must stop before any daemon read or write."""
    from src.cli.app import cli

    client = _client({})
    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(cli, ["system", "config", "set", "not-dotted=true"])

    assert result.exit_code != 0
    assert "KEY must be dotted" in result.output
    client.execute.assert_not_awaited()


# -- aq system config git-identity -------------------------------------------

_UNSET = {
    "success": True,
    "configured": False,
    "installation": None,
    "installation_source": None,
    "fallback": {"name": "Agent Queue", "email": "agent-queue@localhost"},
    "project_id": None,
    "effective": None,
}
_ADA = {
    **_UNSET,
    "configured": True,
    "installation": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "installation_source": "manual",
}


def _saved(name, email):
    return {
        "success": True,
        "configured": True,
        "installation": {"name": name, "email": email},
        "previous": None,
        "changed": True,
        "applies_to": "Commits from sessions launched after this change.",
    }


def _git_identity(runner, client, *args):
    from src.cli.app import cli

    with patch("src.cli.system_config._get_client", return_value=client):
        return runner.invoke(cli, ["system", "config", "git-identity", *args])


def test_git_identity_name_and_email_set_the_default_through_the_daemon(runner):
    client = _client({"set_git_identity": _saved("Ada Lovelace", "ada@example.com")})

    result = _git_identity(
        runner, client, "--name", "Ada Lovelace", "--email", "ada@example.com"
    )

    assert result.exit_code == 0, result.output
    assert client.execute.await_args_list[0].args == (
        "set_git_identity",
        {"name": "Ada Lovelace", "email": "ada@example.com", "source": "manual"},
    )
    assert "Ada Lovelace <ada@example.com>" in result.output


def test_git_identity_clear_unsets_the_default(runner):
    client = _client(
        {"set_git_identity": {"success": True, "configured": False, "installation": None}}
    )

    result = _git_identity(runner, client, "--clear")

    assert result.exit_code == 0, result.output
    assert client.execute.await_args_list[0].args == ("set_git_identity", {"clear": True})
    assert "agent-queue@localhost" in result.output


def test_git_identity_show_prints_the_default_and_the_project_resolution(runner):
    client = _client(
        {
            "get_git_identity": {
                **_ADA,
                "project_id": "web",
                "effective": {
                    "name": "Ada Lovelace",
                    "email": "ada@example.com",
                    "source": "installation",
                },
            }
        }
    )

    result = _git_identity(runner, client, "--show", "--project", "web")

    assert result.exit_code == 0, result.output
    assert client.execute.await_args_list[0].args == ("get_git_identity", {"project_id": "web"})
    assert "Ada Lovelace <ada@example.com>" in result.output
    assert "Project web" in result.output


def test_git_identity_without_a_terminal_only_shows_and_never_prompts(runner, monkeypatch):
    from src.install import git_identity

    monkeypatch.setattr(
        git_identity, "discover", lambda **kwargs: pytest.fail("gh must not be probed")
    )
    client = _client({"get_git_identity": _UNSET})

    result = _git_identity(runner, client)

    assert result.exit_code == 0, result.output
    assert [call.args[0] for call in client.execute.await_args_list] == ["get_git_identity"]
    assert "not configured" in result.output


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (("--name", "Ada"), "must be given together"),
        (("--name", "Ada", "--email", "not an email"), "name@domain"),
        (("--clear", "--show"), "Use one of"),
        (("--project", "web"), "--project only applies to --show"),
    ],
)
def test_git_identity_refuses_bad_options_before_calling_the_daemon(runner, args, fragment):
    client = _client({})

    result = _git_identity(runner, client, *args)

    assert result.exit_code != 0
    assert fragment in result.output
    client.execute.assert_not_awaited()


def _interactive(monkeypatch, discovery):
    from src.cli import system_config
    from src.install import git_identity

    monkeypatch.setattr(system_config, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(git_identity, "discover", lambda **kwargs: discovery)


def _octocat():
    from src.install.git_identity import LABEL_NOREPLY, Discovery, Suggestion

    return Discovery(
        suggestions=(
            Suggestion(
                "Mona Lisa Octocat",
                "583231+octocat@users.noreply.github.com",
                f"GitHub @octocat: {LABEL_NOREPLY}",
                "gh:octocat",
            ),
        ),
        gh_status="authenticated",
    )


def test_git_identity_interactive_confirms_a_suggestion_with_its_source(runner, monkeypatch):
    _interactive(monkeypatch, _octocat())
    client = _client(
        {
            "get_git_identity": _UNSET,
            "set_git_identity": _saved(
                "Mona Lisa Octocat", "583231+octocat@users.noreply.github.com"
            ),
        }
    )

    from src.cli.app import cli

    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(cli, ["system", "config", "git-identity"], input="\n")

    assert result.exit_code == 0, result.output
    assert "keeps your email private" in result.output
    assert client.execute.await_args_list[-1].args == (
        "set_git_identity",
        {
            "name": "Mona Lisa Octocat",
            "email": "583231+octocat@users.noreply.github.com",
            "source": "gh:octocat",
        },
    )


def test_git_identity_interactive_keeps_the_current_one_on_enter(runner, monkeypatch):
    _interactive(monkeypatch, _octocat())
    client = _client({"get_git_identity": _ADA})

    from src.cli.app import cli

    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(cli, ["system", "config", "git-identity"], input="\n")

    assert result.exit_code == 0, result.output
    assert "Current: Ada Lovelace <ada@example.com> (manual)" in result.output
    assert "No change" in result.output
    assert [call.args[0] for call in client.execute.await_args_list] == ["get_git_identity"]


def test_git_identity_interactive_manual_entry_when_gh_is_missing(runner, monkeypatch):
    from src.install.git_identity import Discovery

    _interactive(monkeypatch, Discovery(gh_status="missing", gh_note="gh is not installed"))
    client = _client(
        {
            "get_git_identity": _UNSET,
            "set_git_identity": _saved("Ada Lovelace", "ada@example.com"),
        }
    )

    from src.cli.app import cli

    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(
            cli,
            ["system", "config", "git-identity"],
            input="Ada Lovelace\nada@example.com\n",
        )

    assert result.exit_code == 0, result.output
    assert "gh is not installed" in result.output
    assert client.execute.await_args_list[-1].args == (
        "set_git_identity",
        {"name": "Ada Lovelace", "email": "ada@example.com", "source": "manual"},
    )
