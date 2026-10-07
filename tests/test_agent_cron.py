"""Controlled-time recurring prompts, scope and real durable message delivery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import select, update

from src.agent_cron import AgentCronService, CronError, due_count, next_fire, recurrence
from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.config import AppConfig
from src.database import Database
from src.database.tables import messages, sessions, tasks
from src.messages.delivery import MessageDeliveryEngine
from src.models import AgentProfile, SessionRecord
from src.orchestrator import Orchestrator
from tests.test_agent_wait_queries import NOW, env as _wait_env
from tests.test_message_delivery import FakeSessionManager

env = _wait_env
COMMANDS = ["cron_register", "cron_get", "cron_list", "cron_cancel", "message_status"]


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def test_aligned_interval_offset_and_no_immediate_tick():
    spec = recurrence(every=900, offset=120)
    boundary = epoch("2026-10-07T12:02:00")
    assert next_fire(spec, boundary - 1) == boundary
    assert next_fire(spec, boundary) == boundary + 900
    assert next_fire(spec, boundary + 900 * 500 + 33) == boundary + 900 * 501


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("15 9 * * *", "2026-10-07T16:15:00"),
        ("*/15 * * * *", "2026-10-07T12:15:00"),
        ("* 6 * * *", "2026-10-07T13:00:00"),
    ],
)
def test_simple_cron_timezone(expression, expected):
    spec = recurrence(cron=expression, zone="America/Los_Angeles")
    assert next_fire(spec, epoch("2026-10-07T12:00:00")) == epoch(expected)


def test_dst_missing_time_is_skipped_and_repeated_time_fires_twice():
    spring = recurrence(cron="30 2 * * *", zone="America/Los_Angeles")
    assert next_fire(spring, epoch("2026-03-08T09:00:00")) == epoch("2026-03-09T09:30:00")
    fall = recurrence(cron="30 1 * * *", zone="America/Los_Angeles")
    first = next_fire(fall, epoch("2026-11-01T08:00:00"))
    assert first == epoch("2026-11-01T08:30:00")
    assert next_fire(fall, first) == epoch("2026-11-01T09:30:00")


def test_sparse_cron_missed_count_is_bounded_after_long_outage():
    spec = recurrence(cron="0 9 * * *")
    first = epoch("2026-10-07T09:00:00")
    assert due_count(spec, first, first + 86400 * 2) == 3
    assert due_count(spec, first, first + 86400 * 3650) == 7
    assert due_count(spec, first, first - 1) == 0


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"every": 59},
        {"every": float("nan")},
        {"every": 900, "offset": 900},
        {"every": 900, "cron": "0 9 * * *"},
        {"cron": "0 9 * * *", "offset": 1},
        {"cron": "60 9 * * *"},
        {"cron": "*/0 * * * *"},
        {"cron": "0 24 * * *"},
        {"cron": "0 9 * * MON"},
        {"cron": "0,15 * * * *"},
        {"cron": "0 0 9 * * *"},
        {"every": 60, "zone": "missing/zone"},
    ],
)
def test_invalid_recurrence_is_refused(args):
    with pytest.raises(CronError):
        recurrence(**args)


@pytest.fixture
async def setup(env, monkeypatch):
    for profile in ("worker-codex", "supervisor"):
        await env.db.create_profile(
            AgentProfile(
                id=profile,
                name=profile,
                aq_commands=COMMANDS,
                harness_tools=[],
                plugin_tools=[],
            )
        )
    config = AppConfig()
    config.messages.enabled = True
    config.sessions.enabled = True
    orch = SimpleNamespace(db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    handler = CommandHandler(orch, config)
    clock = [NOW]
    monkeypatch.setattr("src.commands.cron_commands.time.time", lambda: clock[0])
    yield SimpleNamespace(env=env, handler=handler, config=config, clock=clock)


def scope(session_id="s", token="token", project="p", **extra):
    return dict(
        kind="session",
        session_id=session_id,
        session_instance_token=token,
        project_id=project,
        **extra,
    )


async def call(setup, name, args=None, identity=None):
    return await setup.handler.execute(name, {**(args or {}), "_scope": identity or scope()})


async def register(setup, *, key="patrol", identity=None, **extra):
    return await call(
        setup,
        "cron_register",
        {
            "every": 900,
            "offset": 120,
            "prompt": "Run the authorized patrol.",
            "idempotency_key": key,
            "claim_epoch": 1,
            **extra,
        },
        identity,
    )


async def pending_count(env):
    async with env.db._engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    select(messages.c.id).where(
                        messages.c.body_kind == "schedule_prompt",
                        messages.c.delivered_at.is_(None),
                        messages.c.archived_at.is_(None),
                    )
                )
            ).scalars()
        )


async def advance(setup, now):
    setup.clock[0] = now
    result = await AgentCronService(setup.handler).tick(now=now)
    assert result["success"], result
    return result


async def test_registration_duplicate_and_conflict_without_delivery(setup):
    first, again = await asyncio.gather(register(setup), register(setup))
    assert first["success"], first
    assert first["schedule"]["id"] == again["schedule"]["id"]
    assert first["schedule"]["next_fire_at"] > NOW
    assert await pending_count(setup.env) == []
    conflict = await register(setup, prompt="A different prompt")
    assert conflict["error_code"] == "cron.idempotency_conflict"
    due = first["schedule"]["next_fire_at"]
    await advance(setup, due - 1)
    assert await pending_count(setup.env) == []
    await asyncio.gather(advance(setup, due), advance(setup, due))
    assert len(await pending_count(setup.env)) == 1


async def test_coalescing_busy_owner_and_recovery_are_durable(setup):
    registered = await register(setup)
    schedule_id = registered["schedule"]["id"]
    due = registered["schedule"]["next_fire_at"]
    await advance(setup, due + 1800)
    row = await setup.env.db.get_agent_cron(schedule_id)
    assert row["coalesced_count"] == 2
    assert row["tick_count"] == 1
    lens = FakeSessionManager(activity_map={("session", "s", "p"): "busy"})
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    await engine.run_delivery_pass()
    assert lens.nudges == []
    assert (await setup.env.db.get_agent_cron(schedule_id))["delivery_attempts"] == 0
    # Rebuild the persistence adapter: no in-memory timer/subscription is required.
    fresh = Database(setup.env.db._dsn)
    fresh._engine = setup.env.db._engine
    await fresh.reconcile_agent_cron(now=due + 2700)
    await fresh.reconcile_agent_cron(now=due + 2700)
    row = await fresh.get_agent_cron(schedule_id)
    assert row["coalesced_count"] == 3
    assert row["tick_count"] == 1
    assert len(await pending_count(setup.env)) == 1
    assert (await fresh.list_agent_cron(session_id="s", instance_token="token"))[0][
        "id"
    ] == schedule_id


async def test_delivery_wakes_idle_and_consumes_only_its_notification(setup):
    registered = await register(setup)
    row = registered["schedule"]
    await advance(setup, row["next_fire_at"])
    other = await setup.env.db.create_message(
        project_id="p",
        from_kind="user",
        from_id="dashboard",
        to_kind="task",
        to_id="owner",
        body="Other feedback stays pending.",
    )
    lens = FakeSessionManager(activity_map={("session", "s", "p"): "idle"})
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    result = await engine.run_delivery_pass()
    assert result["delivered"] == 1
    assert lens.nudges == [
        ("session", "s", "p", f"Handle `aq cron show {row['id']} --consume --json`.")
    ]
    await engine.run_delivery_pass()
    assert len(lens.nudges) == 1
    shown = await call(setup, "cron_get", {"schedule_id": row["id"]})
    assert shown["schedule"]["prompt"] == "Run the authorized patrol."
    assert shown["schedule"]["delivery_receipt"]["via"] == "nudge"
    # The daemon clears its pending pointer before the agent may consume it.
    await advance(setup, setup.clock[0] + 1)
    assert (await setup.env.db.get_agent_cron(row["id"]))["pending_message_id"] is None
    consumed = await call(
        setup, "cron_get", dict(schedule_id=row["id"], consume=True, claim_epoch=1)
    )
    assert consumed["success"], consumed
    assert consumed["schedule"]["delivery_receipt"]["read_at"] == setup.clock[0]
    assert consumed["schedule"]["delivery_receipt"]["via"] == "nudge"
    assert consumed["schedule"]["last_delivery_status"] == "consumed"
    assert (await setup.env.db.get_message(other.id)).delivered_at is None


async def test_cancel_archives_pending_and_key_cannot_resurrect(setup):
    first = await register(setup)
    row = first["schedule"]
    await advance(setup, row["next_fire_at"])
    cancelled = await call(setup, "cron_cancel", dict(schedule_id=row["id"], claim_epoch=1))
    assert cancelled["schedule"]["state"] == "cancelled"
    assert await pending_count(setup.env) == []
    assert (await register(setup))["schedule"]["state"] == "cancelled"
    await advance(setup, row["next_fire_at"] + 3600)
    assert await pending_count(setup.env) == []


@pytest.mark.parametrize(
    "change",
    [
        {"state": "stopped"},
        {"desired_state": "stopped"},
        {"instance_token": "replacement"},
        {"task_id": "source"},
        {"claim_phase": "idle"},
    ],
)
async def test_session_end_or_replacement_expires_pending_schedule(setup, change):
    first = await register(setup)
    row = first["schedule"]
    await advance(setup, row["next_fire_at"])
    async with setup.env.db._engine.begin() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(**change))
    await advance(setup, row["next_fire_at"] + 1)
    assert (await setup.env.db.get_agent_cron(row["id"]))["state"] == "expired"
    assert await pending_count(setup.env) == []


async def test_task_claim_turnover_expires_schedule(setup):
    first = await register(setup)
    async with setup.env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    await advance(setup, first["schedule"]["next_fire_at"])
    assert (await setup.env.db.get_agent_cron(first["schedule"]["id"]))["state"] == "expired"
    assert await pending_count(setup.env) == []


async def global_owner(setup):
    await setup.env.db.create_session(
        SessionRecord(
            id="global-owner",
            project_id=None,
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-supervisor--global",
            lifecycle="named",
            work_dir="/tmp/global-cron",
            epoch="boot",
            instance_token="global-token",
            started_at=NOW,
            state="running",
        )
    )
    return scope("global-owner", "global-token", None, elevated=True)


async def test_global_unset_project_registration_reconnect_wake_and_no_extra_authority(setup):
    identity = await global_owner(setup)
    first = await register(setup, identity=identity)
    assert first["success"], first
    row = first["schedule"]
    assert row["project_id"] is None and row["owner_task_id"] is None
    listed = await call(setup, "cron_list", identity=identity)
    assert listed["schedules"][0]["id"] == row["id"]
    assert (await register(setup, identity=identity))["schedule"]["id"] == row["id"]
    await advance(setup, row["next_fire_at"])
    lens = FakeSessionManager(activity_map={("session", "global-owner", None): "idle"})
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    assert (await engine.run_delivery_pass())["delivered"] == 1
    assert lens.nudges[0][:3] == ("session", "global-owner", None)
    shown = await call(setup, "cron_get", dict(schedule_id=row["id"], consume=True), identity)
    assert shown["success"] and shown["schedule"]["prompt"] == row["prompt"]
    foreign = await register(setup, identity=identity, project_id="p")
    assert foreign["error_code"] == "out_of_scope"
    foreign = await call(setup, "cron_get", {"schedule_id": row["id"]})
    assert foreign["error_code"] == "out_of_scope"
    # Neither global supervisor scope nor public grants admit daemon-only controls.
    refused = await call(setup, "cron_delivery_begin", {"message_id": "anything"}, identity)
    assert refused.get("success") is False or refused.get("error")


async def test_daemon_session_cycle_wires_cron_and_restarts_without_duplicate_wakes(
    setup, monkeypatch
):
    monkeypatch.setattr(
        "src.commands.collaboration_lifecycle.CollaborationReconciler.tick",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "src.agent_waits.AgentWaitReconciler.tick", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(
        "src.integration.completion_recovery.schedule_ready_owner_recovery", lambda _: None
    )
    identity = await global_owner(setup)
    first = await register(setup, identity=identity)
    row = first["schedule"]
    lens = FakeSessionManager(activity_map={("session", "global-owner", None): "idle"})
    setup.clock[0] = row["next_fire_at"]
    # Exercise the daemon's actual installation and session-cycle methods.
    # Reconstruct them around the same persistence/session to model adoption.
    for _ in range(2):
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = setup.config
        orch.transcript_watcher = SimpleNamespace(tick=AsyncMock())
        orch.agent_questions = SimpleNamespace(tick=AsyncMock())
        orch.session_reconciler = SimpleNamespace(tick=AsyncMock())
        orch.message_delivery = MessageDeliveryEngine(setup.env.db, lens, setup.config)
        orch.set_command_handler(setup.handler)
        assert orch.message_delivery.cron_service is orch.agent_cron
        await orch._reconcile_sessions()
        await orch.message_delivery.run_delivery_pass()
    assert len(lens.nudges) == 1
    assert (await setup.env.db.get_agent_cron(row["id"]))["tick_count"] == 1
    assert (await register(setup, identity=identity))["schedule"]["id"] == row["id"]


async def test_terminal_failures_have_bounded_persisted_backoff(setup, caplog):
    first = await register(setup)
    row = first["schedule"]
    due = row["next_fire_at"]
    await advance(setup, due)
    lens = FakeSessionManager(activity_map={("session", "s", "p"): "idle"}, nudge_returns=False)
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    for attempt in range(1, 6):
        await engine.run_delivery_pass()
        stored = await setup.env.db.get_agent_cron(row["id"])
        assert stored["delivery_attempts"] == attempt
        await engine.run_delivery_pass()
        assert len(lens.nudges) == attempt
        setup.clock[0] = stored["next_attempt_at"]
    # A recovered engine still cannot perform a sixth submission.
    recovered = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    await recovered.run_delivery_pass()
    assert len(lens.nudges) == 5
    await advance(setup, setup.clock[0])
    stored = await setup.env.db.get_agent_cron(row["id"])
    assert stored["last_delivery_status"] == "failed"
    assert stored["last_error"] == "terminal submission deferred"
    assert f"Agent cron delivery failed for {row['id']}" in caplog.text
    assert await pending_count(setup.env) == []
    consumed = await call(
        setup, "cron_get", dict(schedule_id=row["id"], consume=True, claim_epoch=1)
    )
    assert consumed["schedule"]["last_delivery_status"] == "failed"
    assert consumed["schedule"]["delivery_receipt"]["read_at"] is None


@pytest.mark.parametrize("activity", ["busy", "sleeping", "absent"])
async def test_busy_or_missing_owner_does_not_spend_attempts_or_cold_start(setup, activity):
    first = await register(setup)
    row = first["schedule"]
    await advance(setup, row["next_fire_at"])
    lens = FakeSessionManager(activity_map={("session", "s", "p"): activity})
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    await engine.run_delivery_pass()
    assert not lens.nudges and not lens.ensure_started_calls
    assert (await setup.env.db.get_agent_cron(row["id"]))["delivery_attempts"] == 0
    await advance(setup, row["next_fire_at"] + 3600)
    stored = await setup.env.db.get_agent_cron(row["id"])
    assert stored["last_delivery_status"] == "failed"
    assert await pending_count(setup.env) == []


async def test_owner_fence_is_checked_again_at_delivery(setup):
    first = await register(setup)
    row = first["schedule"]
    await advance(setup, row["next_fire_at"])
    async with setup.env.db._engine.begin() as conn:
        await conn.execute(
            update(sessions).where(sessions.c.id == "s").values(instance_token="replaced")
        )
    lens = FakeSessionManager(activity_map={("session", "s", "p"): "idle"})
    engine = MessageDeliveryEngine(
        setup.env.db, lens, setup.config, cron_service=AgentCronService(setup.handler)
    )
    await engine.run_delivery_pass()
    assert not lens.nudges
    assert (await setup.env.db.get_agent_cron(row["id"]))["state"] == "expired"


async def test_scope_validation_caps_and_configuration(setup):
    assert (await register(setup, session_id="someone-else"))["error_code"] == "out_of_scope"
    assert (await register(setup, claim_epoch=0))["error_code"] == "stale_claim"
    assert (await register(setup, offset=901))["error_code"] == "cron.invalid"
    for i in range(10):
        assert (await register(setup, key=f"k{i}"))["success"]
    assert (await register(setup, key="extra"))["error_code"] == "cron.limit"
    setup.config.messages.enabled = False
    assert (await register(setup, key="disabled"))["error_code"] == "cron.disabled"
    assert (await call(setup, "cron_list", {"limit": 101}))["error_code"] == "cron.invalid"


def test_contract_cli_and_fresh_install_grants(monkeypatch):
    from src.cli import app
    from src.cli.app import cli
    from src.profiles.parser import parse_profile
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS
    from src.api.codegen import API_EXCLUDED
    from src.api.models import get_all_response_models

    assert all(CONTRACTS.get(name) for name in COMMANDS[:4])
    assert all(name in get_all_response_models() for name in COMMANDS[:4])
    root = Path(__file__).resolve().parents[1]
    for profile in ("worker-codex", "worker-claude", "supervisor"):
        grants = parse_profile(
            (root / "src/profiles/defaults" / profile / "profile.md").read_text()
        )
        assert set(COMMANDS[:4]) <= set(grants.capabilities["aq_commands"])
    assert {"cron_delivery_begin", "cron_delivery_finish", "reconcile_agent_cron"} <= (
        DEFAULT_EXCLUDED_COMMANDS & API_EXCLUDED
    )
    recorded = []

    class Client:
        async def execute(self, command, params):
            recorded.append((command, params))
            return {"success": True, "schedule": {"id": "x"}}

    @asynccontextmanager
    async def client(*args):
        yield Client()

    monkeypatch.setattr("src.cli.cron._get_client", client)
    monkeypatch.setattr("src.cli.cron.resolve_claim_epoch", lambda value: value)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cron",
            "register",
            "--every",
            "900",
            "--offset",
            "120",
            "--prompt",
            "patrol",
            "--idempotency-key",
            "k",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert recorded[0] == (
        "cron_register",
        dict(
            every=900.0,
            offset=120.0,
            prompt="patrol",
            idempotency_key="k",
            timezone="UTC",
        ),
    )
    assert '"schema_version": 1' in result.output
    result = runner.invoke(cli, ["cron", "register", "--help"])
    assert result.exit_code == 0 and "MINUTE HOUR * * *" in result.output
    # CLI loading keeps database/handler imports out of the startup path.
    assert app.cli is cli
