"""Provider allocation CLI payloads and the mixed-fleet operator view."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli


@pytest.fixture(autouse=True)
def _pg_backend():
    """These transport/rendering tests do not allocate a database."""


def _invoke(argv, payload):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if isinstance(payload, Exception):
        client.execute.side_effect = payload
    else:
        client.execute.return_value = payload
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["pool", "provider", *argv], terminal_width=240)
    return result, client


@pytest.fixture
def status():
    from src.api.models.provider import ProviderAllocationStatusResponse

    groups = []
    for provider, vendor, lifecycle, high in (
        ("codex", "openai", "pool", 2),
        ("claude", "anthropic", "task", None),
    ):
        groups.append(
            {
                "provider": provider,
                "vendor": vendor,
                "supply": {"busy": 1, "idle": 1},
                "ceiling": {"min_active": 0, "max_active": high or 0},
                "profiles": [
                    {
                        "profile_id": f"rung-{provider}",
                        "harness": provider,
                        "intelligence_class": "standard-high",
                        "lifecycle": lifecycle,
                        "min_active": 0,
                        "max_active": high,
                        "supply": {"ready": 3, "busy": 1},
                        "projects": [{"project_id": "alpha", "ready": 3, "busy": 1}],
                        "sessions": [
                            {
                                "session_id": f"session-{provider}",
                                "project_id": "alpha",
                                "lifecycle": lifecycle,
                                "state": "running",
                                "activity": "busy",
                                "task_id": f"task-{provider}",
                                "task_title": "Fix routing [carefully]",
                            },
                            {
                                "session_id": f"idle-{provider}",
                                "project_id": "alpha",
                                "lifecycle": lifecycle,
                                "state": "running",
                                "activity": "idle",
                                "idle_seconds": 42,
                            },
                        ],
                        "pinned": {"count": 1, "task_ids": [f"pin-{provider}"]},
                        "preferred": {"count": 0},
                        "hidden": {"sessions": 2},
                    }
                ],
                "manual_agents": [
                    {
                        "agent_id": f"manual-{provider}",
                        "name": "Custom worker",
                        "profile_id": f"rung-{provider}",
                        "state": "idle",
                        "enabled": True,
                        "model": "custom-model",
                        "has_overrides": True,
                        "effective_harness": provider,
                        "effective_class": "standard-high",
                    }
                ],
            }
        )
    return ProviderAllocationStatusResponse.model_validate(
        {
            "now": 100,
            "global_max_active": 8,
            "providers": groups,
            "redacted": True,
            "projects": [{"project_id": "alpha", "preferred_provider": "codex"}],
            "diagnostics": [{"kind": "profile", "id": "supervisor", "reason": "role"}],
        }
    ).model_dump()


@pytest.fixture
def preview():
    from src.api.models.provider import ProviderAllocationPreviewResponse

    before = {"lifecycle": "pool", "min_active": 0, "max_active": 2}
    after = {"lifecycle": "task", "min_active": None, "max_active": None}
    return ProviderAllocationPreviewResponse.model_validate(
        {
            "provider": "codex",
            "vendor": "openai",
            "required_scope": "operator",
            "request": {"provider": "codex", "participation": "task"},
            "preview_token": "reviewed-token",
            "selected": ["rung-codex"],
            "profiles": [
                {
                    "profile_id": "rung-codex",
                    "selected": True,
                    "changed": True,
                    "before": before,
                    "after": after,
                }
            ],
            "ceiling": {"before": {"max_active": 2}, "after": {"max_active": 0}},
            "project_limits": [
                {
                    "project_id": "alpha",
                    "profile_id": "rung-codex",
                    "lifecycle_before": "pool",
                    "lifecycle_after": "task",
                    "effective_max_before": 2,
                    "effective_max_after": None,
                }
            ],
            "sessions": [
                {
                    "session_id": "busy-one",
                    "project_id": "alpha",
                    "profile_id": "rung-codex",
                    "lifecycle": "pool",
                    "state": "running",
                    "activity": "busy",
                    "task_id": "task-one",
                    "action": "stop_after_task",
                }
            ],
            "busy": {"session_ids": ["busy-one"], "task_ids": ["task-one"]},
            "pinned": [
                {
                    "task_id": "pinned-one",
                    "profile_id": "rung-codex",
                    "status": "READY",
                    "waits": True,
                }
            ],
            "manual_agents": [
                {
                    "agent_id": "manual-one",
                    "profile_id": "rung-codex",
                    "provider": "codex",
                    "push_before": False,
                    "push_after": True,
                }
            ],
            "warnings": [
                {
                    "code": "pinned_ready_wait",
                    "blocking": True,
                    "message": "Pinned work will wait",
                    "subjects": ["pinned-one"],
                }
            ],
            "blocked": True,
        }
    ).model_dump()


def test_status_renders_mixed_fleet_and_allocation_context(status):
    result, client = _invoke(["status", "--project-id", "alpha", "--provider", "openai"], status)
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "provider_allocation_status", {"project_id": "alpha", "provider": "openai"}
    )
    for text in (
        "codex",
        "openai",
        "claude",
        "anthropic",
        "rung-codex",
        "rung-claude",
        "pool",
        "task",
        "alpha",
        "session-codex",
        "idle-codex",
        "42",
        "task-codex",
        "pin-codex",
        "manual-codex",
        "custom-model",
        "supervisor",
        "ceiling",
        "redacted",
        "Fix routing [carefully]",
    ):
        assert text in result.output


@pytest.mark.parametrize("command", ["status", "preview", "apply"])
def test_json_keeps_the_complete_typed_response(command, status, preview):
    payload = (
        status
        if command == "status"
        else preview
        if command == "preview"
        else {
            "success": True,
            "status": "applied",
            "request_id": "allocation-one",
            "profiles": [],
            "ceiling": preview["ceiling"],
            "session_actions": [],
        }
    )
    args = {
        "status": [],
        "preview": ["--provider", "codex", "--lifecycle", "task"],
        "apply": ["--preview-token", "reviewed-token"],
    }[command]
    result, _ = _invoke([command, *args, "--json"], payload)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"schema_version": 1, "data": payload}


def test_preview_translates_flags_and_renders_reviewed_impact(preview):
    result, client = _invoke(
        [
            "preview",
            "--provider",
            "openai",
            "--profiles",
            "rung-one, rung-two",
            "--lifecycle",
            "pool",
            "--min",
            "0",
            "--max",
            "unbounded",
            "--receive-new-work",
            "alpha",
            "--drain",
            "idle-now",
            "--allow-pinned-wait",
        ],
        preview,
    )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "provider_allocation_preview",
        {
            "provider": "openai",
            "profile_ids": ["rung-one", "rung-two"],
            "participation": "pool",
            "bounds": {"min": 0, "max": None},
            "receive_new_work": {"project_id": "alpha", "mode": "prefer"},
            "drain": "idle-now",
            "allow_pinned_wait": True,
        },
    )
    for text in (
        "Before",
        "After",
        "ceiling",
        "alpha",
        "busy-one",
        "task-one",
        "stop_after_task",
        "pinned-one",
        "manual-one",
        "Pinned work will wait",
        "BLOCKED",
        "reviewed-token",
    ):
        assert text in result.output


def test_preview_omits_absent_bounds_and_clears_preference(preview):
    result, client = _invoke(
        [
            "preview",
            "--provider",
            "codex",
            "--clear-new-work",
            "alpha",
        ],
        preview,
    )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "provider_allocation_preview",
        {
            "provider": "codex",
            "receive_new_work": {"project_id": "alpha", "mode": "clear"},
        },
    )


@pytest.mark.parametrize("value,expected", [("0", 0), ("3", 3), ("unbounded", None)])
def test_preview_max_is_typed_and_min_is_not_implied(value, expected, preview):
    result, client = _invoke(["preview", "--provider", "codex", "--max", value], preview)
    assert result.exit_code == 0, result.output
    assert client.execute.await_args.args[1]["bounds"] == {"max": expected}


@pytest.mark.parametrize(
    "flags",
    [
        ["--receive-new-work", "alpha", "--clear-new-work", "alpha"],
        ["--profiles", ""],
        ["--profiles", "one,,two"],
        ["--min", "-1"],
        ["--max", "-1"],
        ["--max", "lots"],
        ["--lifecycle", "named"],
        ["--min", "3", "--max", "2"],
    ],
)
def test_invalid_preview_flags_fail_before_dispatch(flags):
    result, client = _invoke(["preview", "--provider", "codex", *flags], {})
    assert result.exit_code == 2, result.output
    client.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "selector",
    [
        "--provider",
        "--profiles",
        "--profile-ids",
        "--lifecycle",
        "--participation",
        "--min",
        "--max",
        "--drain",
        "--receive-new-work",
        "--clear-new-work",
        "--bounds",
    ],
)
def test_apply_refuses_mutable_selectors(selector):
    result, client = _invoke(["apply", "--preview-token", "token", selector, "value"], {})
    assert result.exit_code == 2, result.output
    client.execute.assert_not_awaited()


def test_apply_sends_only_review_token_and_explicit_authorizations():
    result, client = _invoke(
        [
            "apply",
            "--preview-token",
            "token",
            "--authorize-busy-interrupt",
            "busy-one, busy-two",
            "--allow-pinned-wait",
        ],
        {"success": True, "status": "applied", "request_id": "allocation-one"},
    )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "provider_allocation_apply",
        {
            "preview_token": "token",
            "authorize_busy_interrupt": ["busy-one", "busy-two"],
            "allow_pinned_wait": True,
        },
    )
    assert "allocation-one" in result.output


@pytest.mark.parametrize("as_json", [False, True])
def test_stale_apply_returns_a_fresh_preview_without_reapplying(preview, as_json):
    from src.cli.exceptions import CommandError

    details = {"error_code": "preview_stale", "preview": preview}
    error = CommandError("provider_allocation_apply", "preview_stale", details=details)
    result, client = _invoke(
        [
            "apply",
            "--preview-token",
            "old-token",
            *(["--json"] if as_json else []),
        ],
        error,
    )
    assert result.exit_code == 1
    client.execute.assert_awaited_once()
    if as_json:
        assert json.loads(result.stdout)["error"]["details"] == details
    else:
        assert "reviewed-token" in result.output


def test_apply_failure_renders_compensated_rows(preview):
    from src.cli.exceptions import CommandError

    row = preview["profiles"][0]
    details = {
        "status": "rolled_back",
        "request_id": "allocation-failed",
        "profiles": [
            {**row, "status": "rolled_back", "compensated": True},
        ],
    }
    result, _ = _invoke(
        ["apply", "--preview-token", "token"],
        CommandError(
            "provider_allocation_apply",
            "write failed",
            details=details,
        ),
    )
    assert result.exit_code == 1
    assert "allocation-failed" in result.output
    assert "rolled_back" in result.output
    assert "rung-codex" in result.output


def test_unbounded_aggregate_and_zero_global_cap_are_distinct(status):
    status["global_max_active"] = 0
    status["providers"][0]["ceiling"]["max_active"] = None
    status["providers"][0]["ceiling"]["unbounded"] = True
    result, _ = _invoke(["status"], status)
    assert result.exit_code == 0
    assert "Global pool ceiling: 0" in result.output
    assert "max unbounded" in result.output
