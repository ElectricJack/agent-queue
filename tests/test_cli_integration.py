"""Operational CLI contract for hierarchical integration trains."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.exceptions import CommandError, ScopeDeniedError


def _client(result):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        client.execute.return_value = result
    return client


@pytest.mark.parametrize(
    ("argv", "command", "args"),
    [
        (["status", "p"], "integration_status", {"project_id": "p"}),
        (
            ["record-noop", "parent.1", "--expected-head-sha", "a" * 40],
            "integration_record_noop",
            {"child_task_id": "parent.1", "expected_head_sha": "a" * 40},
        ),
        (["flush", "p"], "integration_flush", {"project_id": "p"}),
        (
            ["sweep", "p", "--recover-child", "child"],
            "integration_development_sweep",
            {"project_id": "p", "retry": False, "recover_child": "child"},
        ),
        (
            [
                "resolve-candidate-member",
                "--resolved-head-sha",
                "a" * 40,
                "--resolved-tree-sha",
                "b" * 40,
                "--repair-commit-sha",
                "c" * 40,
                "--repair-commit-sha",
                "a" * 40,
                "--claim-epoch",
                "7",
            ],
            "integration_resolve_candidate_member",
            {
                "resolved_head_sha": "a" * 40,
                "resolved_tree_sha": "b" * 40,
                "repair_commit_shas": ["c" * 40, "a" * 40],
                "claim_epoch": 7,
            },
        ),
        (
            [
                "enable",
                "p",
                "--mode",
                "train",
                "--interval-seconds",
                "600",
                "--expected-generation",
                "7",
                "--reason",
                "roll out",
                "--waiver-id",
                "waiver-1",
            ],
            "integration_enable",
            {
                "project_id": "p",
                "mode": "train",
                "interval_seconds": 600,
                "expected_generation": 7,
                "reason": "roll out",
                "waiver_id": "waiver-1",
            },
        ),
        (
            [
                "waive-history",
                "p",
                "--reason",
                "accepted history",
                "--blocker-digest",
                "sha256:" + "a" * 64,
            ],
            "integration_waive_history",
            {
                "project_id": "p",
                "reason": "accepted history",
                "blocker_digest": "sha256:" + "a" * 64,
            },
        ),
        (["resume", "op-1"], "integration_resume", {"operation_id": "op-1"}),
        (
            ["abort", "op-1", "--reason", "operator decision"],
            "integration_abort",
            {"operation_id": "op-1", "reason": "operator decision"},
        ),
        (
            ["retry-cleanup", "batch-1"],
            "integration_retry_cleanup",
            {"batch_id": "batch-1"},
        ),
        (
            ["release-owner", "--task-id", "task-1", "--dry-run"],
            "integration_release_owner",
            {"task_id": "task-1", "dry_run": True},
        ),
        (
            ["release-stale-owners", "--project-id", "p"],
            "integration_release_stale_owners",
            {"project_id": "p", "dry_run": False},
        ),
        (
            ["release-stale-owners", "--project-id", "p", "--dry-run", "--older-than", "2d"],
            "integration_release_stale_owners",
            {"project_id": "p", "dry_run": True, "older_than": "2d"},
        ),
        (
            ["adopt-legacy-deliveries", "--project-id", "p", "--dry-run"],
            "integration_adopt_legacy_deliveries",
            {"project_id": "p", "dry_run": True},
        ),
        (
            [
                "adopt-legacy-deliveries", "--project-id", "p",
                "--accept", "c1", "--accept", "c2", "--reason", "no-code task",
            ],
            "integration_adopt_legacy_deliveries",
            {"project_id": "p", "dry_run": False, "accept": ["c1", "c2"], "reason": "no-code task"},
        ),
        (
            [
                "adopt-legacy-deliveries", "--project-id", "p",
                "--supersede", "c1", "--by", "abcdef1234", "--retire", "c2",
                "--reason", "re-delivered; abandoned",
            ],
            "integration_adopt_legacy_deliveries",
            {
                "project_id": "p",
                "dry_run": False,
                "supersede": {"c1": "abcdef1234"},
                "retire": ["c2"],
                "reason": "re-delivered; abandoned",
            },
        ),
        (
            ["recover-candidate-member", "frozen-resolution"],
            "integration_recover_candidate_member",
            {"reservation_id": "frozen-resolution"},
        ),
        (
            ["develop", "p", "--command", "aq test tests/x.py", "--reason", "dev"],
            "integration_develop",
            {
                "project_id": "p",
                "reason": "dev",
                "policy": {
                    "validation": "focused",
                    "commands": ["aq test tests/x.py"],
                    "interval_seconds": 300,
                },
            },
        ),
        (
            [
                "develop", "p", "--command", "aq test tests/x.py", "--reason", "slow box",
                "--timeout-seconds", "900", "--slot-wait-seconds", "1200",
            ],
            "integration_develop",
            {
                "project_id": "p",
                "reason": "slow box",
                "policy": {
                    "validation": "focused",
                    "commands": ["aq test tests/x.py"],
                    "interval_seconds": 300,
                    "timeout_seconds": 900,
                    "slot_wait_seconds": 1200,
                },
            },
        ),
    ],
)
def test_integration_commands_use_generic_execute_and_json_envelope(argv, command, args):
    from src.cli.app import cli

    response = {"outcome": "status", "project_id": "p", "generation": 7}
    client = _client(response)
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "integration", *argv])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"schema_version": 1, "data": response}
    client.execute.assert_awaited_once_with(command, args)


def test_integration_status_brief_keeps_operator_fences_and_drops_deep_detail():
    from src.cli.app import cli

    response = {
        "outcome": "status",
        "project_id": "p",
        "effective_mode": "train",
        "desired_mode": "train",
        "generation": 9,
        "draining": False,
        "ready": False,
        "blockers": [{"code": "human_hold", "detail": "needs operator", "ref": "op"}],
        "blocker_digest": "sha256:" + "b" * 64,
        "schedule": {"next_due_at": 123.0},
        "members": [{"task_id": "t"}],
    }
    client = _client(response)
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["--json", "--brief", "integration", "status", "p"]
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data == {
        "outcome": "status",
        "project_id": "p",
        "operation_id": None,
        "batch_id": None,
        "effective_mode": "train",
        "desired_mode": "train",
        "generation": 9,
        "draining": False,
        "ready": False,
        "blockers": response["blockers"],
        "blocker_digest": response["blocker_digest"],
        "state": None,
        "stage": None,
        "count": None,
    }
    assert "schedule" not in data
    assert "members" not in data


@pytest.mark.parametrize(
    ("error", "exit_code", "code"),
    [
        (
            CommandError(
                "integration_enable",
                "stale generation",
                {"generation": 12, "blockers": [{"code": "stale"}]},
            ),
            1,
            "command_error",
        ),
        (ScopeDeniedError("integration_enable", "LOCAL authority required"), 4, "out_of_scope"),
    ],
)
def test_integration_errors_keep_structured_details_and_authorization_exit(error, exit_code, code):
    from src.cli.app import cli

    client = _client(error)
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "integration",
                "enable",
                "p",
                "--mode",
                "train",
                "--expected-generation",
                "11",
                "--reason",
                "roll out",
            ],
        )

    assert result.exit_code == exit_code, result.output
    error_payload = json.loads(result.output)["error"]
    assert error_payload["code"] == code
    if isinstance(error, ScopeDeniedError):
        assert "LOCAL authority" in error_payload["message"]
    else:
        assert error_payload["details"] == error.details


def test_integration_cli_is_handcrafted_and_has_no_deferred_probe_command():
    from src.cli.app import cli
    from src.cli.auto_commands import HANDCRAFTED_COVERAGE

    expected = {
        "integration_status",
        "integration_flush",
        "integration_enable",
        "integration_waive_history",
        "integration_resume",
        "integration_abort",
        "integration_retry_cleanup",
        "integration_resolve_candidate_member",
    }
    assert expected <= HANDCRAFTED_COVERAGE

    result = CliRunner().invoke(cli, ["integration", "--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "status",
        "flush",
        "enable",
        "waive-history",
        "resume",
        "abort",
        "retry-cleanup",
        "release-owner",
        "resolve-candidate-member",
        "recover-candidate-member",
    ):
        assert command in result.output
    assert "probe" not in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "train", "--interval-seconds", "0"],
        ["--mode", "train", "--interval-seconds", "-1"],
        ["--mode", "observe", "--interval-seconds", "60"],
    ],
)
def test_integration_enable_rejects_invalid_interval_before_transport(argv):
    from src.cli.app import cli

    client = _client({"outcome": "enabled"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "integration",
                "enable",
                "p",
                *argv,
                "--expected-generation",
                "0",
                "--reason",
                "cadence",
            ],
        )

    assert result.exit_code == 2, result.output
    assert "interval" in result.output.lower()
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--supersede", "c1", "--reason", "r"], "--supersede and --by go together"),
        (["--by", "abcdef1234", "--reason", "r"], "--supersede and --by go together"),
        (["--retire", "c1"], "require --reason"),
    ],
)
def test_adopt_legacy_deliveries_rejects_incomplete_decisions_before_transport(argv, message):
    from src.cli.app import cli

    client = _client({"outcome": "adopted"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["integration", "adopt-legacy-deliveries", "--project-id", "p", *argv]
        )

    assert result.exit_code == 2, result.output
    assert message in result.output
    client.execute.assert_not_awaited()


def test_operator_guide_uses_only_real_operational_commands_and_options():
    from src.cli.app import cli

    guide = (
        Path(__file__).parents[1] / "docs/guides/hierarchical-integration-trains.md"
    ).read_text()
    required = (
        "aq integration status PROJECT_ID",
        "aq integration flush PROJECT_ID",
        "aq integration enable PROJECT_ID --mode observe --expected-generation GENERATION --reason REASON",
        "aq integration enable PROJECT_ID --mode train --interval-seconds SECONDS --expected-generation GENERATION --reason REASON",
        "aq integration waive-history PROJECT_ID --reason REASON --blocker-digest BLOCKER_DIGEST",
        "aq integration resume OPERATION_ID",
        "aq integration abort OPERATION_ID --reason REASON",
        "aq integration retry-cleanup BATCH_ID",
        "aq integration resolve-candidate-member",
        "aq project set PROJECT_ID integration-repository-id REPOSITORY_ID --expected-integration-generation GENERATION --reason REASON",
        "aq project set PROJECT_ID integration-policy POLICY_JSON --expected-integration-generation GENERATION --reason REASON",
    )
    for command in required:
        assert command in guide
    assert "aq integration probe" not in guide

    for leaf in (
        "status",
        "flush",
        "enable",
        "waive-history",
        "resume",
        "abort",
        "retry-cleanup",
        "resolve-candidate-member",
        "recover-candidate-member",
    ):
        result = CliRunner().invoke(cli, ["integration", leaf, "--help"])
        assert result.exit_code == 0, (leaf, result.output)
