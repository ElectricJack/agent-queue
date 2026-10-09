"""Operational CLI contract for hierarchical integration trains."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


def _client(result):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        client.execute.return_value = result
    return client


@pytest.mark.parametrize("apply", [False, True])
def test_provenance_namespace_migration_defaults_to_preview(tmp_path, apply):
    from src.cli.app import cli

    client = _client({"success": True, "outcome": "migrated" if apply else "preview"})
    argv = ["integration", "migrate-provenance-refs", "p", "--limit", "12",
            "--checkout", str(tmp_path)]
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, argv + (["--apply"] if apply else []))
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args == ("integration_migrate_provenance_refs", {
        "project_id": "p", "dry_run": not apply, "limit": 12, "checkout": str(tmp_path),
    })



def test_cutover_transmits_saved_plan_and_defaults_to_preview(tmp_path):
    from src.cli.app import cli

    flow = tmp_path / "flow.yaml"
    flow.write_text("promotion_flow: [{id: release, source: dev, target: main}]\n")
    baseline = {"project_id": "p", "generation": 7, "aborted_members": []}
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema_version": 1, "data": {"plan": baseline}}))
    client = _client({"success": True, "outcome": "preview"})
    argv = ["integration", "cutover", "--project", "p", "--flow", str(flow)]
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args[1]["dry_run"] is True
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, argv + ["--apply", "--expected-generation", "7",
                                                "--plan", str(plan)])
    assert result.exit_code == 0, result.output
    args = client.execute.call_args.args[1]
    assert args["dry_run"] is False and args["baseline"] == baseline


def test_cutover_apply_requires_preview_and_generation_before_transport(tmp_path):
    from src.cli.app import cli

    flow = tmp_path / "flow.yaml"
    flow.write_text("[]\n")
    client = _client({})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "cutover", "--project", "p",
            "--flow", str(flow), "--apply", "--expected-generation", "7"])
    assert result.exit_code == 2 and "--plan FILE" in result.output
    client.execute.assert_not_awaited()

def test_root_noop_previews_then_applies_exact_head_with_reason():
    from src.cli.app import cli

    client = _client({"success": True, "outcome": "preview"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "record-root-noop", "root"])
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args == ("integration_record_root_noop", {
        "task_id": "root", "dry_run": True, "reason": "",
    })
    client.reset_mock()
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "record-root-noop", "root", "--apply",
                                        "--head", "a" * 40, "--reason", "no code"])
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args[1] == {
        "task_id": "root", "dry_run": False, "expected_head_sha": "a" * 40, "reason": "no code",
    }


@pytest.mark.parametrize("extra", [[], ["--head", "a" * 40], ["--reason", "no code"]])
def test_root_noop_apply_requires_exact_head_and_reason(extra):
    from src.cli.app import cli

    client = _client({"success": True})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "record-root-noop", "root", "--apply", *extra])
    assert result.exit_code == 2
    client.execute.assert_not_called()



@pytest.mark.parametrize("leaf,identity,extra,command", [
    ("abort-batch", "batch", [], "integration_abort_batch"),
    ("retire-origin", "task", ["--origin-id", "origin"], "integration_retire_origin"),
])
def test_train_controls_default_to_preview_and_apply_with_reason(leaf, identity, extra, command):
    from src.cli.app import cli

    client = _client({"success": True, "outcome": "preview"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", leaf, identity])
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args[0] == command
    assert client.execute.call_args.args[1]["dry_run"] is True
    client.reset_mock()
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", leaf, identity, "--apply", *extra,
                                         "--reason", "delivered work"])
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args[1]["dry_run"] is False
    assert client.execute.call_args.args[1]["reason"] == "delivered work"


@pytest.mark.parametrize("argv", [
    ["abort-batch", "batch", "--apply"],
    ["retire-origin", "task", "--apply", "--reason", "delivered"],
])
def test_train_controls_require_apply_arguments_before_transport(argv):
    from src.cli.app import cli

    client = _client({})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv])
    assert result.exit_code == 2, result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize("apply", [False, True])
def test_refresh_epic_control_preview_and_apply(apply):
    from src.cli.app import cli

    client = _client({"success": True, "outcome": "preview" if not apply else "pending"})
    argv = ["integration", "refresh-epic", "--task", "epic"]
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, argv + (["--apply"] if apply else []))
    assert result.exit_code == 0, result.output
    assert client.execute.call_args.args == (
        "integration_refresh_epic", {"task_id": "epic", "dry_run": not apply})


def test_reevaluate_repair_apply_requires_exact_preview():
    from src.cli.app import cli

    client = _client({"outcome": "reevaluated"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "reevaluate-repair", "operation", "--apply"])
    assert result.exit_code == 2
    assert "--apply requires --head, --generation, --stage, --fence, --snapshot and --reason" in result.output
    client.execute.assert_not_awaited()




@pytest.mark.parametrize(
    "ci_source",
    [
        None,
        {"root": "hosted", "epic": "hosted", "promotion": "hosted", "origin": "default"},
        {"root": "local", "epic": "local", "promotion": "hosted", "origin": "policy"},
    ],
    ids=["absent", "default-hosted", "policy-local"],
)
def test_integration_status_brief_keeps_operator_fences_and_drops_deep_detail(ci_source):
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
        "promotion_flow": {"state": "misconfigured", "chain": "dev -> main"},
        "schedule": {"next_due_at": 123.0},
        "members": [{"task_id": "t"}],
        "github": {
            "scope": "daemon", "window_seconds": 60, "retry_at": 123.0,
            "api_calls_per_minute": {"GET repos/acme/widgets": 3},
            "gh_commands_per_minute": {},
        },
    }
    if ci_source is not None:
        response["ci_source"] = ci_source
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
        "github": response["github"],
        # The promotion chain and its misconfigured marking stay visible too.
        "promotion_flow": response["promotion_flow"],
        # The chosen CI runner and its origin remain visible when reported.
        "ci_source": ci_source,
        "state": None,
        "stage": None,
        "count": None,
    }
    assert "schedule" not in data
    assert "members" not in data








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






def test_operator_guide_uses_only_real_operational_commands_and_options():
    from src.cli.app import cli

    guide = (
        Path(__file__).parents[1] / "docs/guides/hierarchical-integration-trains.md"
    ).read_text()
    required = (
        "aq integration status PROJECT_ID",
        "aq integration resolve-candidate-member",
        "aq project set PROJECT_ID integration-repository-id REPOSITORY_ID --expected-integration-generation GENERATION --reason REASON",
        "aq project set PROJECT_ID integration-policy POLICY_JSON --expected-integration-generation GENERATION --reason REASON",
    )
    for command in required:
        assert command in guide
    assert "aq integration probe" not in guide

    for leaf in (
        "status",
        "resolve-candidate-member",
        "recover-candidate-member",
    ):
        result = CliRunner().invoke(cli, ["integration", leaf, "--help"])
        assert result.exit_code == 0, (leaf, result.output)


def test_no_code_receipts_are_documented_as_an_operator_control_not_a_supervisor_one():
    """`record-noop` carries no ``project_id`` but admits no session either.

    The handler authorizes a session principal only for
    ``_SUPERVISOR_REDRIVE_CAPABILITIES``, so the guide must not list
    `record-noop` among the controls a project's own supervisor may run, and
    the shipped supervisor profile must name it as an operator's action only.
    """
    from src.profiles.parser import parse_profile

    root = Path(__file__).parents[1]
    guide = (root / "docs/guides/hierarchical-integration-trains.md").read_text(
        encoding="utf-8"
    )
    paragraph = guide.split("The controls keyed by a task, operation, batch or reservation")[1]
    paragraph = paragraph.split("\n\n")[0]
    controls, _, exception = paragraph.partition("take no `project_id`")
    assert "record-noop" not in controls
    assert "local operator records no-code receipts" in exception
    assert "every* session is refused it" in exception

    section = guide.split("### No-code child receipts")[1].split("\n## ")[0]
    assert "a local\noperator records the exact no-code disposition" in section
    assert "No session can invoke it" in section

    profile_text = (root / "src/profiles/defaults/supervisor/profile.md").read_text(
        encoding="utf-8"
    )
    supervisor = parse_profile(profile_text)
    assert supervisor.errors == []
    assert "integration_record_noop" not in supervisor.capabilities["aq_commands"]
    mentions = [line for line in profile_text.splitlines() if "record-noop" in line]
    assert mentions
    for line in mentions:
        assert "operator" in line, line

    troubleshooting = (root / "docs/guides/integration-troubleshooting.md").read_text(
        encoding="utf-8"
    )
    assert (
        "a local operator records a no-code child's disposition with "
        "`aq integration record-noop`" in troubleshooting
    )




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
    ("argv", "message"),
    (
        (["development-engine-transfer", "p", "--engine", "reconciler", "--apply"],
         "--apply needs exact subject versions, a reason and cutover evidence"),
        (["development-engine-transfer", "p", "--engine", "reconciler", "--apply",
          "--expected-subject", "root:7"],
         "--apply needs exact subject versions, a reason and cutover evidence"),
        (["development-engine-transfer", "p", "--engine", "reconciler", "--apply",
          "--expected-subject", "root:7", "--reason", "reviewed cutover"],
         "--apply needs exact subject versions, a reason and cutover evidence"),
        (["development-engine-transfer", "p", "--engine", "legacy", "--apply", "--reason", "rollback"],
         "Invalid value for '--engine'"),
    ),
)
def test_development_engine_transfer_apply_needs_the_previewed_fences(argv, message):
    """Transfers require exact preview fences; the retired engine is refused."""
    from src.cli.app import cli

    client = _client({"outcome": "transferred"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv])

    assert result.exit_code != 0
    assert message in result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize("options", [[], ["--reason", "resume"], ["--expected-version", "1"],
                                     ["--expected-version", "1", "--reason", "   "]])
def test_release_held_gate_apply_requires_exact_version_and_reason(options):
    from src.cli.app import cli

    client = _client({"outcome": "released"})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "release-held-gate", "subject", "gate",
                                          "--apply", *options])
    assert result.exit_code != 0
    assert "--apply needs --expected-version and a nonblank --reason" in result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "expected", (["root"], ["root:-1"], ["root:seven"], ["root:7", "root:9"])
)
def test_every_engine_transfer_rejects_an_unusable_expected_subject(expected):
    from src.cli.app import cli

    client = _client({"outcome": "preview"})
    argv = ["integration", "development-engine-transfer", "p", "--engine", "reconciler"]
    for item in expected:
        argv += ["--expected-subject", item]
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, argv)

    assert result.exit_code != 0
    assert "SUBJECT_ID:VERSION" in result.output
    client.execute.assert_not_awaited()


def test_integration_help_excludes_retired_controls():
    from src.cli.app import cli
    result = CliRunner().invoke(cli, ["integration", "--help"])
    assert result.exit_code == 0, result.output
    commands = cli.commands["integration"].commands
    for retired in ("flush", "enable", "resume", "abort", "retry-cleanup",
                    "develop", "adopt", "onboard-train", "shadow-report",
                    "adopt-legacy-deliveries", "bind-legacy-repositories", "materialize-root"):
        assert retired not in commands
    assert {"status", "engine-transfer", "release-held-gate", "pause-batch", "resume-batch",
            "eject", "seal-now"} <= commands.keys()


@pytest.mark.parametrize("argv,command,identity", [
    (["pause-batch", "batch"], "integration_pause_batch", {"batch_id": "batch"}),
    (["resume-batch", "batch"], "integration_resume_batch", {"batch_id": "batch"}),
    (["eject", "--batch", "batch", "--task", "task"], "integration_eject",
     {"batch_id": "batch", "task_id": "task"}),
    (["seal-now", "--project", "p"], "integration_seal_now", {"project_id": "p"}),
])
@pytest.mark.parametrize("apply", [False, True])
def test_train_controls_preview_and_apply_transport(argv, command, identity, apply):
    from src.cli.app import cli

    client = _client({"success": True, "outcome": "preview"})
    extra = ["--apply"] if apply else ["--dry-run"]
    if command == "integration_eject" and apply:
        extra += ["--reason", "isolate member"]
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", *argv, *extra])
    assert result.exit_code == 0, result.output
    called, values = client.execute.call_args.args
    assert called == command
    assert all(values[key] == value for key, value in identity.items())
    assert values["dry_run"] is not apply


def test_train_eject_apply_requires_reason_before_transport():
    from src.cli.app import cli

    client = _client({})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "eject", "--batch", "b",
                                         "--task", "t", "--apply"])
    assert result.exit_code == 2
    client.execute.assert_not_awaited()


def test_train_brief_status_keeps_batch_intent_and_ejection_disposition():
    from src.cli.app import cli

    response = {"outcome": "status", "project_id": "p", "batches": [
        {"id": "old", "intent": "aborted", "member_disposition": "pending"},
        {"id": "new", "intent": "paused", "member_disposition": "paused"},
    ]}
    client = _client(response)
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "status", "p", "--json", "--brief"])
    assert result.exit_code == 0
    assert json.loads(result.output)["data"]["batches"] == response["batches"]
