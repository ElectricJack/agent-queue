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
        "rerequest-check",
        "refresh-epic",
        "reverify-epic",
        "record-root-noop",
        "off",
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


def test_manual_apply_requires_preview_digest_before_contacting_daemon():
    result = CliRunner().invoke(cli, ["integration", "off", "p", "--reason", "manual", "--apply"])
    assert result.exit_code == 2
    assert "--expected-plan-digest" in result.output


@pytest.mark.parametrize("applying", [False, True])
def test_check_rerequest_cli_forwards_named_check_and_candidate(monkeypatch, applying):
    calls = []
    module = importlib.import_module("src.cli.integration")
    monkeypatch.setattr(module, "_execute", lambda ctx, name, args: calls.append((name, args)))
    args = ["integration", "rerequest-check", "batch", "--check", "Tests (postgres-integration)"]
    if applying:
        args.extend(["--apply", "--candidate-sha", "a" * 40, "--reason", "Phantom job"])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert calls == [("integration_rerequest_check", {
        "batch_id": "batch", "check_name": "Tests (postgres-integration)",
        "expected_candidate_sha": "a" * 40 if applying else None,
        "reason": "Phantom job" if applying else "", "dry_run": not applying,
    })]


def test_check_rerequest_apply_requires_previewed_head():
    result = CliRunner().invoke(cli, [
        "integration", "rerequest-check", "batch", "--check", "Tests", "--apply",
    ])
    assert result.exit_code == 2
    assert "--apply requires --candidate-sha and --reason" in result.output
