"""Ensure the operator integration controls are registered in the CLI."""

import importlib

import pytest
from click.testing import CliRunner

from src.cli.app import cli


def test_integration_group_exposes_train_controls():
    integration = cli.commands["integration"]

    assert {
        "seal-now",
        "pause-batch",
        "resume-batch",
        "abort-batch",
        "settle-batch",
        "refresh-epic",
        "record-root-noop",
        "supersede-completion",
    } <= integration.commands.keys()


@pytest.mark.parametrize("applying", [False, True])
def test_supersession_cli_forwards_exact_proof_and_defaults_to_preview(monkeypatch, applying):
    calls = []
    module = importlib.import_module("src.cli.integration")
    monkeypatch.setattr(module, "_execute", lambda ctx, name, args: calls.append((name, args)))
    args = ["integration", "supersede-completion", "root", "--project", "p",
            "--subject", "subject", "--expected-version", "2", "--expected-claim-epoch", "3",
            "--completion", "close-1", "--head", "a" * 40, "--human-reply", "original-reply",
            "--reason", "Original human supersession"]
    if applying:
        args.extend(["--apply", "--proof", "b" * 64])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert calls == [("integration_supersede_completion", {
        "task_id": "root", "project_id": "p", "subject_id": "subject", "expected_version": 2,
        "expected_claim_epoch": 3, "completion_id": "close-1", "expected_head_sha": "a" * 40,
        "human_reply_id": "original-reply", "reason": "Original human supersession",
        "expected_proof_sha256": "b" * 64 if applying else None, "dry_run": not applying,
    })]


def test_supersession_cli_apply_without_preview_digest_never_dispatches(monkeypatch):
    module = importlib.import_module("src.cli.integration")
    calls = []
    monkeypatch.setattr(module, "_execute", lambda *args: calls.append(args))
    result = CliRunner().invoke(cli, [
        "integration", "supersede-completion", "root", "--project", "p", "--subject", "subject",
        "--expected-version", "2", "--expected-claim-epoch", "3", "--completion", "close-1",
        "--head", "a" * 40, "--human-reply", "original-reply", "--reason", "Superseded", "--apply",
    ])
    assert result.exit_code == 2 and "--apply requires --proof" in result.output
    assert not calls
