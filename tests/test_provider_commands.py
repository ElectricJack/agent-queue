"""Provider availability commands (``docs/specs/provider-failover.md`` D6, D19, D20).

``provider_status`` / ``provider_history`` / ``provider_recheck`` /
``provider_set_state`` are the operator surface of the availability service,
and ``provider_availability_notify`` is its contracted, idempotent
state-change notice.  The service owns every write; these tests drive it with
real evidence against a real database and check what the commands report and
refuse.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import (
    ProviderAvailabilityNotifyArgs,
    ProviderAvailabilityNotifyValue,
    set_handler_provider,
)
from src.commands.provider_commands import parse_duration, parse_timestamp
from src.models import AgentProfile, Project, Task, TaskStatus
from src.providers.availability import AUTH_PROBE, STARTUP_DIALOG


@pytest.fixture
async def handler(command_handler_factory):
    return await command_handler_factory()


@pytest.fixture
def service(handler):
    svc = handler.orchestrator.provider_availability
    # Never shell out to a real ``codex login status`` from a test.
    svc._probe_impl = AsyncMock(return_value="cannot_tell")
    return svc


async def _trip_codex_unauthenticated(service) -> None:
    """The 2026-09-20 incident: two launches die on Codex's login dialog."""
    for _ in range(2):
        await service.record(
            "codex", STARTUP_DIALOG, "auth", detail={"dialog": "login-required"}, project_id="p1"
        )
    await service.wait_for_probes()


# -- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("90s", 90), ("30m", 1800), ("4h", 14400), ("2d", 172800), ("1h30m", 5400), (600, 600)],
)
def test_parse_duration(raw, seconds) -> None:
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["", "soon", "4x", "-5", 0, True])
def test_parse_duration_refuses_anything_else(raw) -> None:
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_parse_timestamp_accepts_epoch_and_iso() -> None:
    assert parse_timestamp(1_800_000_000) == 1_800_000_000
    assert parse_timestamp("1800000000") == 1_800_000_000
    iso = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert parse_timestamp("2026-09-20T12:00:00Z") == iso.timestamp()
    with pytest.raises(ValueError):
        parse_timestamp("next tuesday")


# -- provider_status ---------------------------------------------------------------


async def test_status_reports_state_reason_since_and_recovery(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    result = await handler.execute("provider_status", {})
    assert result["success"] is True
    assert result["mode"] == "enforce"
    rows = {row["provider"]: row for row in result["providers"]}
    codex = rows["codex"]
    assert codex["state"] == "unauthenticated"
    assert codex["half"] == "unavailable"
    assert codex["vendor"] == "openai"
    assert "login dialog" in codex["reason"]
    assert codex["since"] <= time.time()
    assert codex["until"] is None  # only a human fixes a login
    assert "codex login" in codex["remediation"]
    assert codex["override"] is None
    assert codex["held"] == 0
    # Not verbose: no evidence ring, no transitions.
    assert "evidence" not in codex
    assert codex["transitions"] == []


async def test_status_accepts_a_vendor_alias_and_verbose_adds_evidence(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    result = await handler.execute("provider_status", {"provider": "openai", "verbose": True})
    assert result["success"] is True
    [codex] = result["providers"]
    assert codex["provider"] == "codex"
    kinds = [entry["kind"] for entry in codex["evidence"]]
    assert kinds.count(STARTUP_DIALOG) == 2
    assert codex["transitions"][0]["to_state"] == "unauthenticated"


async def test_status_refuses_an_unknown_provider(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    result = await handler.execute("provider_status", {"provider": "nosuch"})
    assert result["success"] is False
    assert "unknown provider" in result["error"]


async def test_status_includes_the_newest_account_wide_usage_reading(handler, service) -> None:
    now = time.time()
    await handler.db.record_provider_usage(
        [
            {
                "provider": "codex",
                "window": "primary",
                "scope": "",
                "used_percent": 42.0,
                "resets_at": now + 3600,
                "observed_at": now,
                "source": "transcript",
            }
        ]
    )
    await service.record("codex", AUTH_PROBE, "authenticated")
    result = await handler.execute("provider_status", {"provider": "codex"})
    usage = result["providers"][0]["usage"]
    assert usage["window"] == "primary"
    assert usage["used_percent"] == 42.0


async def test_status_counts_held_tasks_for_an_unavailable_provider(handler, service) -> None:
    service.affected = AsyncMock(return_value={"roles": [], "held": [{"task_id": "t1"}] * 3})
    await _trip_codex_unauthenticated(service)
    result = await handler.execute("provider_status", {"provider": "codex"})
    assert result["providers"][0]["held"] == 3


async def test_status_held_count_is_exactly_what_the_derived_hold_holds(handler, service) -> None:
    """``held`` counts every queued status and follows the derived default (D13, D18).

    It once listed only ``READY`` tasks and resolved an unrouted task through
    the raw project default, so the dashboard's banner and the notices
    disagreed with ``aq task explain``: a ``DEFINED`` task was held but not
    counted, and an unrouted task whose default has a rung on an available
    provider was counted but not held.
    """
    db = handler.db
    for profile_id, harness, cls in (
        ("sh-codex", "codex", "standard-high"),
        ("sh-claude", "claude", "standard-high"),
        ("astra-codex", "codex", "astra-high"),  # no other provider runs astra
    ):
        await db.create_profile(
            AgentProfile(id=profile_id, name=profile_id, harness=harness, default_class=cls)
        )
    # An unrouted task in "rung" follows sh-codex's equivalent rung on claude;
    # one in "astra" has nowhere to go.
    await db.create_project(Project(id="rung", name="rung", default_profile_id="sh-codex"))
    await db.create_project(Project(id="astra", name="astra", default_profile_id="astra-codex"))
    tasks = [
        ("ready", "rung", "sh-codex", TaskStatus.READY),
        ("defined", "rung", "sh-codex", TaskStatus.DEFINED),
        ("blocked", "rung", "sh-codex", TaskStatus.BLOCKED),
        ("paused", "rung", "sh-codex", TaskStatus.PAUSED),
        ("running", "rung", "sh-codex", TaskStatus.IN_PROGRESS),  # not queued
        ("on-claude", "rung", "sh-claude", TaskStatus.READY),  # a launchable provider
        ("unrouted-rung", "rung", None, TaskStatus.READY),  # follows the default's rung
        ("unrouted-astra", "astra", None, TaskStatus.DEFINED),  # held
    ]
    for task_id, project_id, profile_id, status in tasks:
        await db.create_task(
            Task(
                id=task_id,
                project_id=project_id,
                title=task_id,
                description="",
                status=status,
                profile_id=profile_id,
            )
        )
    await _trip_codex_unauthenticated(service)
    assert service.suppresses("codex")

    holds = {}
    for task_id, *_ in tasks:
        hold = await service.hold_for(await db.get_task(task_id))
        if hold is not None:
            holds[task_id] = hold
    assert set(holds) == {"ready", "defined", "blocked", "paused", "unrouted-astra"}
    assert {hold["provider"] for hold in holds.values()} == {"codex"}

    affected = await service.affected("codex")
    assert {entry["task_id"] for entry in affected["held"]} == set(holds)
    assert {entry["task_id"]: entry["profile_id"] for entry in affected["held"]} == {
        task_id: hold["profile_id"] for task_id, hold in holds.items()
    }
    result = await handler.execute("provider_status", {"provider": "codex"})
    assert result["providers"][0]["held"] == len(holds) == 5


# -- provider_history --------------------------------------------------------------


async def test_history_lists_transitions_newest_first(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    result = await handler.execute("provider_history", {"provider": "codex"})
    assert result["success"] is True
    states = [row["to_state"] for row in result["transitions"]]
    assert states[0] == "unauthenticated"
    assert "degraded" in states
    ats = [row["at"] for row in result["transitions"]]
    assert ats == sorted(ats, reverse=True)


# -- provider_set_state ------------------------------------------------------------


async def test_disabled_override_defaults_to_the_configured_ttl(handler, service) -> None:
    await service.record("codex", AUTH_PROBE, "authenticated")
    before = time.time()
    result = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "disabled", "reason": "rotating the account"},
    )
    assert result["success"] is True, result
    assert result["state"] == "disabled"
    override = result["status"]["override"]
    ttl = handler.config.provider_failover.override.default_ttl_seconds
    assert before + ttl - 5 <= override["until"] <= time.time() + ttl + 5
    assert override["by"] == "human:local-operator"
    assert override["reason"] == "rotating the account"
    assert result["transition"]["to_state"] == "disabled"
    history = await handler.execute("provider_history", {"provider": "codex"})
    assert history["transitions"][0]["actor"] == "human:local-operator"


async def test_for_and_until_set_the_expiry(handler, service) -> None:
    await service.record("codex", AUTH_PROBE, "authenticated")
    result = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "disabled", "reason": "x", "for": "30m"},
    )
    assert result["status"]["override"]["until"] == pytest.approx(time.time() + 1800, abs=10)
    target = time.time() + 7200
    result = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "disabled", "reason": "x", "until": str(target)},
    )
    assert result["status"]["override"]["until"] == pytest.approx(target)


async def test_no_expiry_is_for_disabled_only(handler, service) -> None:
    await service.record("codex", AUTH_PROBE, "authenticated")
    ok = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "disabled", "reason": "x", "no_expiry": True},
    )
    assert ok["success"] is True
    assert ok["status"]["override"]["until"] is None
    refused = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "available", "reason": "x", "no_expiry": True},
    )
    assert refused["success"] is False
    assert "always expires" in refused["error"]


async def test_available_override_always_expires_and_beats_the_evidence(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    result = await handler.execute(
        "provider_set_state",
        {"provider": "codex", "state": "available", "reason": "false positive"},
    )
    assert result["success"] is True
    assert result["state"] == "available"
    status = result["status"]
    assert status["override"]["until"] is not None
    # The evidence is still derived and shown.
    assert status["derived_state"] == "unauthenticated"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"state": "sideways", "reason": "x"}, "state must be one of"),
        ({"state": "disabled"}, "reason is required"),
        ({"state": "disabled", "reason": "x", "for": "1h", "until": "1900000000"}, "at most one"),
        ({"state": "disabled", "reason": "x", "for": "soon"}, "not a duration"),
        ({"state": "disabled", "reason": "x", "for": "30d"}, "at most"),
        ({"state": "disabled", "reason": "x", "until": "1000"}, "already have expired"),
    ],
)
async def test_set_state_validation(handler, service, args, error) -> None:
    await service.record("codex", AUTH_PROBE, "authenticated")
    result = await handler.execute("provider_set_state", {"provider": "codex", **args})
    assert result["success"] is False
    assert error in result["error"]


async def test_auto_clears_the_override_and_rederives(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    await handler.execute(
        "provider_set_state", {"provider": "codex", "state": "disabled", "reason": "x"}
    )
    result = await handler.execute("provider_set_state", {"provider": "codex", "state": "auto"})
    assert result["success"] is True
    assert result["status"]["override"] is None
    # "I just ran codex login": an unavailable derived state goes to probation.
    assert result["state"] == "degraded"
    assert result["status"]["reason_code"] == "recovering"


async def test_a_worker_token_is_refused(handler, service) -> None:
    from src.api.auth import RequestScope
    from src.api.scope import check_command_scope

    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")
    for command in ("provider_set_state", "provider_recheck", "provider_status"):
        assert check_command_scope(command, {}, worker) == f"out of scope: {command}"
    # The command repeats the rule for a caller that reaches it another way.
    handler._current_scope = {"kind": "session", "session_id": "s1", "elevated": False}
    try:
        refused = await handler._cmd_provider_set_state(
            {"provider": "codex", "state": "disabled", "reason": "x"}
        )
        assert refused["success"] is False
        assert refused["error"].startswith("out of scope")
        refused = await handler._cmd_provider_recheck({"provider": "codex"})
        assert refused["error"].startswith("out of scope")
    finally:
        handler._current_scope = None


# -- provider_recheck --------------------------------------------------------------


async def test_recheck_moves_a_logged_in_provider_to_probation(handler, service) -> None:
    await _trip_codex_unauthenticated(service)
    service._probe_impl = AsyncMock(return_value="authenticated")
    result = await handler.execute("provider_recheck", {"provider": "codex"})
    assert result["success"] is True
    assert result["probe"] == "authenticated"
    assert result["state"] == "degraded"
    assert result["status"]["probation"] is True
    assert result["transition"]["from_state"] == "unauthenticated"


# -- provider_availability_notify --------------------------------------------------


async def test_notify_is_idempotent_per_generation(handler, service) -> None:
    service.on_half_change = None  # drive the notice by hand
    await _trip_codex_unauthenticated(service)
    first = await handler.execute("provider_availability_notify", {"provider": "codex"})
    assert first["success"] is True
    assert first["outcome"] == "notified"
    assert len(first["message_ids"]) == 2
    again = await handler.execute("provider_availability_notify", {"provider": "codex"})
    assert again["outcome"] == "already_notified"
    inbox = await handler.db.list_messages(to_kind="user", to_id="dashboard")
    notices = [m for m in inbox if "codex" in (m.subject or "")]
    assert len(notices) == 1
    assert "codex login" in notices[0].body
    supervisor = await handler.db.list_messages(to_kind="session", to_id="supervisor-global")
    assert sum("codex" in (m.subject or "") for m in supervisor) == 1


def test_notify_is_contracted_as_an_idempotent_create() -> None:
    registration = CONTRACTS.require("provider_availability_notify")
    execution = registration.contract.execution
    assert execution.args_model is ProviderAvailabilityNotifyArgs
    assert execution.result_model is ProviderAvailabilityNotifyValue
    assert "notified" in {spec.name for spec in execution.outcomes}
    assert "rejected" in {spec.name for spec in execution.outcomes}
    assert execution.side_effect == "create"
    assert [clause.subject for clause in execution.effects] == ["message"]


async def test_notify_adapter_maps_outcomes_and_refusals() -> None:
    fake = AsyncMock()
    set_handler_provider(lambda: fake)
    try:
        registration = CONTRACTS.require("provider_availability_notify")
        args = ProviderAvailabilityNotifyArgs(provider="codex", generation=3)
        for outcome in ("notified", "already_notified", "flap_damped", "not_a_half_change"):
            fake.execute = AsyncMock(
                return_value={"success": True, "outcome": outcome, "provider": "codex"}
            )
            assert (await registration.invoke(args, None)).outcome == outcome
        name, sent = fake.execute.await_args.args
        assert name == "provider_availability_notify"
        assert sent == {"provider": "codex", "generation": 3}
        fake.execute = AsyncMock(return_value={"success": False, "error": "unknown provider"})
        assert (await registration.invoke(args, None)).outcome == "rejected"
    finally:
        set_handler_provider(None)


def test_notify_stays_off_every_external_surface() -> None:
    from src.api.codegen import API_EXCLUDED
    from src.cli.auto_commands import EXCLUDED
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

    assert "provider_availability_notify" in API_EXCLUDED
    assert "provider_availability_notify" in EXCLUDED
    assert "provider_availability_notify" in DEFAULT_EXCLUDED_COMMANDS


def test_provider_commands_land_in_the_provider_cli_group() -> None:
    from src.tools import _TOOL_CATEGORIES

    for name in ("provider_status", "provider_history", "provider_recheck", "provider_set_state"):
        assert _TOOL_CATEGORIES[name] == "provider"
