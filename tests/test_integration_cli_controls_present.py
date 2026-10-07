"""Ensure the operator integration controls are registered in the CLI."""

from src.cli.app import cli


def test_integration_group_exposes_train_controls():
    integration = cli.commands["integration"]

    assert {
        "seal-now",
        "pause-batch",
        "resume-batch",
        "abort-batch",
        "refresh-epic",
    } <= integration.commands.keys()
