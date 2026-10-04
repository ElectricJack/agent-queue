"""Operational CLI contract for hierarchical integration trains."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.exceptions import CommandError, ScopeDeniedError


def test_onboarding_pr_read_uses_scoped_runner_and_normalizes_state(monkeypatch):
    from src.cli.integration import _gh_json
    from src.git.github_cli import GhResult, GhRunner

    async def fake_run(self, args, **kwargs):
        assert args == (
            "api", "repos/ElectricJack/agent-queue/pulls/123", "--jq", ".state"
        )
        assert kwargs["hostname"] == "github.com"
        return GhResult(0, b"open\n", "")

    monkeypatch.setattr(GhRunner, "run", fake_run)
    assert _gh_json("pr", "view", "https://github.com/ElectricJack/agent-queue/pull/123") == "OPEN"


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
        (["adopt", "p", "--task", "parent", "--head-sha", "a" * 40,
          "--settle-delivered-children", "--dry-run", "--reason", "children delivered"],
         "integration_adopt", {"project_id": "p", "task_ids": ["parent"],
          "target_ref": "refs/heads/main", "head_sha": "a" * 40,
          "accept_equivalent": False, "settle_delivered_children": True,
          "dry_run": True, "reason": "children delivered"}),
        (["settle-delivered-batch", "batch"], "integration_settle_delivered_batch",
         {"batch_id": "batch", "dry_run": True}),
        (["settle-delivered-batch", "batch", "--apply", "--candidate", "a" * 40,
          "--target-head", "b" * 40, "--snapshot", "c" * 64, "--reason", "delivered"],
         "integration_settle_delivered_batch",
         {"batch_id": "batch", "dry_run": False, "expected_candidate_sha": "a" * 40,
          "expected_target_sha": "b" * 40, "expected_snapshot_digest": "c" * 64,
          "reason": "delivered"}),
        (["engine-transfer", "repo", "--engine", "legacy"], "integration_engine_transfer",
         {"repository_id": "repo", "engine": "legacy", "expected_versions": {},
          "reason": "", "evidence": [], "dry_run": True}),
        (["engine-transfer", "repo", "--engine", "reconciler", "--apply",
          "--expected-subject", "root:7", "--reason", "reviewed cutover", "--evidence", "shadow-week"],
         "integration_engine_transfer",
         {"repository_id": "repo", "engine": "reconciler", "expected_versions": {"root": 7},
          "reason": "reviewed cutover", "evidence": ["shadow-week"], "dry_run": False}),
        (["recover-parent-head", "op", "--head", "a" * 40],
         "integration_recover_parent_head",
         {"operation_id": "op", "head_sha": "a" * 40, "dry_run": True}),
        (["recover-parent-head", "op", "--head", "a" * 40, "--apply", "--episode", "episode",
          "--generation", "2", "--stage", "12", "--fence", "19", "--reason", "reconcile"],
         "integration_recover_parent_head",
         {"operation_id": "op", "head_sha": "a" * 40, "dry_run": False,
          "expected_episode_id": "episode", "expected_generation": 2, "expected_stage": 12,
          "expected_fence_token": 19, "reason": "reconcile"}),
        (["recover-preserved-repair", "op", "--intent", "intent", "--candidate", "a" * 40],
         "integration_recover_preserved_repair",
         {"operation_id": "op", "intent_id": "intent", "candidate_sha": "a" * 40, "dry_run": True}),
        (["recover-preserved-repair", "op", "--intent", "intent", "--candidate", "a" * 40,
          "--apply", "--stage", "9", "--released-fence", "19", "--reason", "preserved"],
         "integration_recover_preserved_repair",
         {"operation_id": "op", "intent_id": "intent", "candidate_sha": "a" * 40,
          "dry_run": False, "expected_stage": 9, "expected_released_fence": 19,
          "reason": "preserved"}),
        (["migrate-provenance", "p", "--task-id", "research", "--no-artifact",
          "--reason", "research only", "--apply"], "integration_migrate_provenance",
         {"project_id": "p", "apply": True, "limit": 500, "offset": 0,
          "task_id": "research", "no_artifact": True, "reason": "research only"}),
        (["migrate-provenance", "p", "--task-id", "legacy", "--source", "a" * 40,
          "--reason", "delivered", "--apply"], "integration_migrate_provenance",
         {"project_id": "p", "apply": True, "limit": 500, "offset": 0,
          "task_id": "legacy", "source": "a" * 40, "reason": "delivered"}),
        (["status", "p"], "integration_status", {"project_id": "p"}),
        (["status", "p", "--control-only"], "integration_status",
         {"project_id": "p", "control_only": True}),
        (
            ["record-noop", "parent.1", "--expected-head-sha", "a" * 40],
            "integration_record_noop",
            {"child_task_id": "parent.1", "expected_head_sha": "a" * 40},
        ),
        (["flush", "p"], "integration_flush", {"project_id": "p"}),
        (["release-delegates", "op", "--archive-obsolete"], "integration_release_delegates",
         {"operation_id": "op", "archive_obsolete": True}),
        (["materialize-root", "legacy"], "integration_materialize_root",
         {"task_id": "legacy", "dry_run": True}),
        (["materialize-root", "legacy", "--apply", "--head", "a" * 40,
          "--reason", "recover legacy PR"], "integration_materialize_root",
         {"task_id": "legacy", "dry_run": False, "expected_head_sha": "a" * 40,
          "reason": "recover legacy PR"}),
        (["close-delivered-pr", "p", "687"], "integration_close_delivered_pr",
         {"project_id": "p", "pr_number": 687, "dry_run": True}),
        (["close-delivered-pr", "p", "687", "--apply", "--head", "a" * 40,
          "--reason", "patch-equivalent on main"], "integration_close_delivered_pr",
         {"project_id": "p", "pr_number": 687, "dry_run": False,
          "expected_head_sha": "a" * 40, "reason": "patch-equivalent on main"}),
        (["authorize-root", "chore"], "integration_authorize_root",
         {"task_id": "chore", "dry_run": True}),
        (["authorize-root", "chore", "--apply", "--head", "a" * 40,
          "--reason", "user authorized PR 711"], "integration_authorize_root",
         {"task_id": "chore", "dry_run": False, "expected_head_sha": "a" * 40,
          "reason": "user authorized PR 711"}),
        (
            ["sweep", "p", "--recover-child", "child"],
            "integration_development_sweep",
            {"project_id": "p", "retry": False, "recover_child": "child"},
        ),
        (
            ["settle-parked", "p", "op-1", "--dismiss", "--reason", "stale park"],
            "integration_settle_parked",
            {"project_id": "p", "operation_id": "op-1", "dismiss": True, "reason": "stale park"},
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
            ["reserve-owner", "--task-id", "task-1"],
            "integration_reserve_owner",
            {"task_id": "task-1"},
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
            ["clear-stale-request", "p"],
            "integration_clear_stale_request",
            {"project_id": "p", "dry_run": True},
        ),
        (
            ["redrive-root", "noble-harbor-74"],
            "integration_redrive_root",
            {"task_id": "noble-harbor-74", "dry_run": True},
        ),
        (
            [
                "redrive-root", "noble-harbor-74", "--apply",
                "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "--reason", "leaf root closed with no PR",
            ],
            "integration_redrive_root",
            {
                "task_id": "noble-harbor-74", "dry_run": False,
                "expected_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reason": "leaf root closed with no PR",
            },
        ),
        (
            ["redrive-child", "sharp-impact.1"],
            "integration_redrive_child",
            {"task_id": "sharp-impact.1", "dry_run": True},
        ),
        (
            [
                "redrive-child", "sharp-impact.1", "--apply",
                "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "--reason", "child never assembled",
            ],
            "integration_redrive_child",
            {
                "task_id": "sharp-impact.1", "dry_run": False,
                "expected_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reason": "child never assembled",
            },
        ),
        (
            ["reopen-collection", "calm-grove-25"],
            "integration_reopen_collection",
            {"task_id": "calm-grove-25", "dry_run": True},
        ),
        (
            [
                "reopen-collection", "calm-grove-25", "--apply",
                "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "--reason", "collection cancelled",
            ],
            "integration_reopen_collection",
            {
                "task_id": "calm-grove-25", "dry_run": False,
                "expected_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reason": "collection cancelled",
            },
        ),
        (
            [
                "clear-stale-request", "p", "--apply",
                "--request-id", "integration-sweep:p:53", "--reason", "aborted batch",
            ],
            "integration_clear_stale_request",
            {
                "project_id": "p", "dry_run": False,
                "expected_request_id": "integration-sweep:p:53", "reason": "aborted batch",
            },
        ),
        (
            ["rebind-reused-identity", "--task-id", "t"],
            "integration_rebind_reused_identity",
            {"task_id": "t", "dry_run": True},
        ),
        (
            ["rebind-repair", "--task-id", "repair-t", "--dry-run"],
            "integration_rebind_repair",
            {"task_id": "repair-t", "dry_run": True},
        ),
        (
            ["rebind-repair", "--task-id", "repair-t", "--apply", "--head", "a" * 40],
            "integration_rebind_repair",
            {"task_id": "repair-t", "dry_run": False, "expected_head_sha": "a" * 40},
        ),
        (
            ["rebind-detached-repair", "op-1"],
            "integration_rebind_detached_repair",
            {"operation_id": "op-1", "dry_run": True},
        ),
        (
            [
                "rebind-detached-repair", "op-1", "--apply", "--stage", "7",
                "--remote-head", "a" * 40, "--reason", "frozen head unpublished",
            ],
            "integration_rebind_detached_repair",
            {
                "operation_id": "op-1", "dry_run": False, "expected_stage": 7,
                "expected_remote_head_sha": "a" * 40, "reason": "frozen head unpublished",
            },
        ),
        (
            [
                "rebind-reused-identity", "--task-id", "t", "--apply",
                "--origin-id", "o1", "--origin-id", "o2",
                "--discard-tip", "a" * 40, "--reason", "reused identity",
            ],
            "integration_rebind_reused_identity",
            {
                "task_id": "t", "dry_run": False, "expected_origin_ids": ["o1", "o2"],
                "discard_tips": ["a" * 40], "reason": "reused identity",
            },
        ),
        (
            ["bind-legacy-repositories", "p"],
            "integration_bind_legacy_repositories",
            {"project_id": "p", "dry_run": True, "reason": None},
        ),
        (
            ["bind-legacy-repositories", "p", "--apply", "--reason", "delivered"],
            "integration_bind_legacy_repositories",
            {"project_id": "p", "dry_run": False, "reason": "delivered"},
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
        "warnings": [{"code": "audit_workflow_missing", "detail": "warns", "ref": "repo"}],
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
        "projection_kind": None,
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
        # Non-blocking App-mode warnings stay visible in the brief projection.
        "warnings": response["warnings"],
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
        "integration_gate_answer",
        "integration_authorize_root",
        "integration_policy_activate",
        "integration_hold",
        "integration_status",
        "integration_explain",
        "integration_flush",
        "integration_enable",
        "integration_waive_history",
        "integration_resume",
        "integration_abort",
        "integration_retry_cleanup",
        "integration_bind_legacy_repositories",
        "integration_clear_stale_request",
        "integration_redrive_root",
        "integration_redrive_child",
        "integration_reopen_collection",
        "integration_rebind_reused_identity",
        "integration_rebind_repair",
        "integration_rebind_detached_repair",
        "integration_recover_preserved_repair",
        "integration_resolve_candidate_member",
    }
    assert expected <= HANDCRAFTED_COVERAGE

    result = CliRunner().invoke(cli, ["integration", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("gate", "authorize", "policy", "hold", "status", "explain", "flush", "legacy"):
        assert command in result.output
    # Pre-consolidation controls are listed only under ``legacy``.
    assert "waive-history" not in result.output
    legacy = CliRunner().invoke(cli, ["integration", "legacy", "--help"])
    assert legacy.exit_code == 0, legacy.output
    for command in (
        "enable",
        "waive-history",
        "resume",
        "abort",
        "retry-cleanup",
        "bind-legacy-repositories",
        "clear-stale-request",
        "redrive-root",
        "redrive-child",
        "reopen-collection",
        "rebind-reused-identity",
        "rebind-repair",
        "rebind-detached-repair",
        "release-owner",
        "recover-candidate-member",
    ):
        assert command in legacy.output
    assert "probe" not in result.output


def test_delivered_batch_apply_requires_preview_fences_before_transport():
    from src.cli.app import cli

    client = _client({"outcome": "settled"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "settle-delivered-batch", "batch", "--apply"])
    assert result.exit_code == 2
    assert "--candidate, --target-head, --snapshot and --reason" in result.output
    client.execute.assert_not_called()


def test_rebind_repair_apply_requires_previewed_head_before_transport():
    from src.cli.app import cli

    client = _client({"outcome": "rebound"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["integration", "rebind-repair", "--task-id", "repair-t", "--apply"]
        )

    assert result.exit_code == 2
    assert "--apply requires --head from dry-run" in result.output
    client.execute.assert_not_called()


def test_rebind_detached_repair_apply_requires_previewed_identity_before_transport():
    from src.cli.app import cli

    client = _client({"outcome": "rebound"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, [
            "integration", "rebind-detached-repair", "op-1", "--apply",
            "--remote-head", "a" * 40, "--reason", "frozen head unpublished",
        ])

    assert result.exit_code == 2
    assert "--apply requires --stage, --remote-head and --reason" in result.output
    client.execute.assert_not_called()


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


@pytest.mark.parametrize(
    "argv",
    (
        ["clear-stale-request", "p", "--apply", "--reason", "aborted batch"],
        ["clear-stale-request", "p", "--apply", "--request-id", "integration-sweep:p:53"],
    ),
)
def test_clear_stale_request_apply_needs_the_request_and_a_reason(argv):
    from src.cli.app import cli

    client = _client({"outcome": "cleared"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv])

    assert result.exit_code != 0
    assert "--apply requires --request-id and --reason" in result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "argv",
    (
        ["redrive-root", "r1", "--apply", "--reason", "stuck"],
        ["redrive-root", "r1", "--apply", "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        ["redrive-child", "c1", "--apply", "--reason", "stuck"],
        ["redrive-child", "c1", "--apply", "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        ["reopen-collection", "p1", "--apply", "--reason", "stuck"],
        ["reopen-collection", "p1", "--apply", "--head", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    ),
)
def test_redrive_root_apply_needs_the_head_and_a_reason(argv):
    from src.cli.app import cli

    client = _client({"outcome": "opened"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv])

    assert result.exit_code != 0
    assert "--apply requires --head and --reason" in result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "argv",
    (
        ["rebind-reused-identity", "--task-id", "t", "--apply", "--reason", "reused"],
        ["rebind-reused-identity", "--task-id", "t", "--apply", "--origin-id", "o1"],
    ),
)
def test_rebind_reused_identity_apply_needs_the_origins_and_a_reason(argv):
    from src.cli.app import cli

    client = _client({"outcome": "rebound"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv])

    assert result.exit_code != 0
    assert "--apply requires --origin-id and --reason" in result.output
    client.execute.assert_not_awaited()
