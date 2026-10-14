"""Ensure the operator integration controls are registered in the CLI."""

from src.cli.app import cli
from click.testing import CliRunner


def test_integration_group_exposes_train_controls():
    integration = cli.commands["integration"]

    assert {
        "seal-now",
        "pause-batch",
        "resume-batch",
        "abort-batch",
        "refresh-epic",
        "record-root-noop",
        "off",
    } <= integration.commands.keys()


def test_manual_apply_requires_preview_digest_before_contacting_daemon():
    result = CliRunner().invoke(cli, ["integration", "off", "p", "--reason", "manual", "--apply"])
    assert result.exit_code == 2
    assert "--expected-plan-digest" in result.output
