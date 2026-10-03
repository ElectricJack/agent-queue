"""CLI surface tests for the 14 K03 knowledge_* and record_* auto-generated commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Group and sub-verb registration
# ---------------------------------------------------------------------------


class TestGroupRegistration:
    """The knowledge and record CLI groups exist and carry all expected verbs."""

    def test_knowledge_group_exists(self):
        from src.cli.app import cli

        assert "knowledge" in cli.commands

    def test_knowledge_group_has_all_eight_verbs(self):
        from src.cli.app import cli

        kgroup = cli.commands["knowledge"]
        expected = {
            "create", "list", "show", "update",
            "history", "diff", "retire", "restore", "export",
            "propose", "proposal-show", "proposal-decide", "verify", "authority-grant",
            "authority-revoke", "share", "redact",
        }
        missing = expected - set(kgroup.commands.keys())
        assert not missing, f"knowledge group is missing: {missing}"

    def test_record_group_exists(self):
        from src.cli.app import cli

        assert "record" in cli.commands

    def test_record_group_has_all_six_verbs(self):
        from src.cli.app import cli

        rgroup = cli.commands["record"]
        expected = {"show", "search", "capabilities", "link-create", "link-list", "link-remove",
                    "repair"}
        missing = expected - set(rgroup.commands.keys())
        assert not missing, f"record group is missing: {missing}"


# ---------------------------------------------------------------------------
# Help output: options derived from pydantic contracts
# ---------------------------------------------------------------------------


class TestHelpOutput:
    """--help output must expose the expected required/optional options."""

    def test_knowledge_create_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["knowledge", "create", "--help"])
        assert result.exit_code == 0, result.output
        for opt in ("--project-id", "--title", "--body", "--category", "--idempotency-key"):
            assert opt in result.output, f"missing {opt}"

    def test_knowledge_show_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["knowledge", "show", "--help"])
        assert result.exit_code == 0, result.output
        for opt in ("--project-id", "--identity"):
            assert opt in result.output, f"missing {opt}"

    def test_record_show_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["record", "show", "--help"])
        assert result.exit_code == 0, result.output
        for opt in ("--project-id", "--identity"):
            assert opt in result.output, f"missing {opt}"

    def test_record_search_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["record", "search", "--help"])
        assert result.exit_code == 0, result.output
        # query is optional (default=""), but project-id is required
        assert "--project-id" in result.output

    def test_record_link_create_help(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["record", "link-create", "--help"])
        assert result.exit_code == 0, result.output
        for opt in ("--project-id", "--identity", "--idempotency-key"):
            assert opt in result.output, f"missing {opt}"


# ---------------------------------------------------------------------------
# Forwarding: args are passed through to the daemon
# ---------------------------------------------------------------------------


def _mock_client(execute_results: dict, captured_args: dict):
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)

    async def mock_execute(command, args=None):
        captured_args.update(args or {})
        return execute_results.get(command, {})

    mock.execute = AsyncMock(side_effect=mock_execute)
    return mock


class TestForwarding:
    """Auto-generated commands forward the expected command name and args."""

    def test_knowledge_create_forwards(self, runner):
        from src.cli.app import cli

        captured = {}
        mock = _mock_client({"knowledge_create": {"record_id": "r-1"}}, captured)
        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "knowledge", "create",
                    "--project-id", "proj",
                    "--title", "A finding",
                    "--body", "Body text",
                    "--category", "fact",
                    "--idempotency-key", "k-1",
                ],
            )
        assert result.exit_code == 0, result.output
        mock.execute.assert_awaited_once()
        cmd = mock.execute.await_args.args[0]
        assert cmd == "knowledge_create"
        args = mock.execute.await_args.args[1]
        assert args["project_id"] == "proj"
        assert args["title"] == "A finding"
        assert args["body"] == "Body text"
        assert args["category"] == "fact"
        assert args["idempotency_key"] == "k-1"

    def test_record_show_forwards(self, runner):
        from src.cli.app import cli

        captured = {}
        mock = _mock_client({"record_show": {"identity": "record:abc"}}, captured)
        with patch("src.cli.app._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "record", "show",
                    "--project-id", "proj",
                    "--identity", "record:abc",
                ],
            )
        assert result.exit_code == 0, result.output
        mock.execute.assert_awaited_once()
        cmd = mock.execute.await_args.args[0]
        assert cmd == "record_show"
        args = mock.execute.await_args.args[1]
        assert args["project_id"] == "proj"
        assert args["identity"] == "record:abc"


@pytest.mark.parametrize("explicit, expected", [(None, 7), (3, 3)])
def test_proposal_resolves_claim_file_epoch_without_overriding_explicit(runner, explicit, expected):
    from src.cli.app import cli

    captured = {}
    mock = _mock_client({"knowledge_propose": {"proposal_id": "p-1"}}, captured)
    args = ["knowledge", "propose", "--project-id", "proj", "--snapshot",
            '{"title":"Correction","body":"Evidence","category":"note"}',
            "--idempotency-key", "propose"]
    if explicit is not None:
        args += ["--claim-epoch", str(explicit)]
    with patch("src.cli.app._get_client", return_value=mock), patch(
        "src.cli.claim_epoch.resolve_claim_epoch", return_value=7,
    ):
        result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert captured["claim_epoch"] == expected
    assert captured["snapshot"]["title"] == "Correction"
