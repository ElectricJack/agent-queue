"""Long-horizon notices: durable receipts, cancellation races and scoped commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from click.testing import CliRunner
from sqlalchemy import func, select, update

from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig
from src.database import Database
from src.database.tables import messages, project_schedules, wait_source_events
from src.models import AgentProfile, Project, SessionRecord
from src.project_schedules import ProjectScheduleService, ScheduleError, utc_instant
from src.waits.adapters.base import Adapter
from src.waits.adapters.timer import TimerAdapter
from src.waits.conditions import WaitError
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0
DUE = NOW + 30 * 86400
COMMANDS = ["schedule_register", "schedule_get", "schedule_list", "schedule_cancel"]


def iso(instant):
    return datetime.fromtimestamp(instant, timezone.utc).isoformat()


@pytest.fixture
async def env(monkeypatch):
    dsn = lease_dsn("project-schedules")
    db = Database(dsn)
    await db.initialize()
    for project_id in ("p", "q"):
        await db.create_project(Project(id=project_id, name=project_id))
    for profile_id in ("supervisor", "worker-codex"):
        await db.create_profile(AgentProfile(
            id=profile_id, name=profile_id, aq_commands=COMMANDS,
            harness_tools=[], plugin_tools=[],
        ))
    await db.create_session(SessionRecord(
        id="sup", project_id="p", profile_id="supervisor", harness="codex",
        provider="fake", name="n-supervisor--p", lifecycle="named", work_dir="/tmp/sup",
        epoch="boot", instance_token="sup-token", started_at=NOW, state="running",
    ))
    config = AppConfig()
    # Schedules must not depend on waits/messages feature flags or a held task.
    config.messages.enabled = False
    orch = SimpleNamespace(db=db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    handler = CommandHandler(orch, config)
    clock = [NOW]
    monkeypatch.setattr("src.commands.schedule_commands.time.time", lambda: clock[0])
    value = SimpleNamespace(db=db, dsn=dsn, orch=orch, handler=handler, clock=clock)
    yield value
    await value.db.close()


async def register(env, *, key="observation", due_at=None, body="Observe compatibility exit."):
    return await env.handler.execute("schedule_register", {
        "project_id": "p", "due_at": due_at or iso(DUE), "body": body,
        "idempotency_key": key,
    })


async def tick(env, now=DUE):
    return await ProjectScheduleService(env.handler, clock=lambda: now).tick()


async def notices(env):
    async with env.db._engine.connect() as conn:
        return (await conn.execute(select(messages).where(
            messages.c.body_kind == "project_schedule",
        ))).mappings().all()


async def test_migration_is_idempotent_and_preserves_receipts(env):
    migration = import_module("migrations.versions.a00000000123_project_schedules")

    def rebuild(bind):
        project_schedules.drop(bind)
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
        inspector = sa.inspect(bind)
        assert {c["name"] for c in inspector.get_columns("project_schedules")} == set(
            project_schedules.c.keys(),
        )
        assert {c["name"] for c in inspector.get_check_constraints("project_schedules")} == {
            c.name for c in project_schedules.constraints if isinstance(c, sa.CheckConstraint)
        }
        assert {c["name"] for c in inspector.get_unique_constraints("project_schedules")} == {
            c.name for c in project_schedules.constraints if isinstance(c, sa.UniqueConstraint)
        }
        assert {i["name"] for i in inspector.get_indexes("project_schedules")
                if not i.get("duplicates_constraint")} == {i.name for i in project_schedules.indexes}

    async with env.db._engine.begin() as conn:
        await conn.run_sync(rebuild)
    await register(env)

    def refuse_downgrade(bind):
        with Operations.context(MigrationContext.configure(bind)):
            with pytest.raises(RuntimeError, match="Cannot discard durable schedules"):
                migration.downgrade()

    async with env.db._engine.begin() as conn:
        await conn.run_sync(refuse_downgrade)
    assert (await tick(env))["fired"] == 1


async def test_thirty_day_notice_survives_restart_and_fires_once(env):
    result = await register(env)
    row = result["schedule"]
    assert row["state"] == "pending" and row["due_at"] == DUE
    assert (await tick(env, DUE - 1))["fired"] == 0
    await env.db.close()
    env.db = Database(env.dsn)
    await env.db.initialize()
    env.orch.db = env.db
    env.handler = CommandHandler(env.orch, AppConfig())
    # New engine and service, overdue after a deployment outage.
    assert (await tick(env, DUE + 500))["fired"] == 1
    assert (await tick(env, DUE + 1000))["fired"] == 0
    queued = await notices(env)
    assert len(queued) == 1
    assert queued[0]["to_kind"] == "session"
    assert queued[0]["to_id"] == "supervisor-global"  # project supervision disabled
    assert "Observe compatibility exit." in queued[0]["body"]
    receipt = await env.db.get_project_schedule(project_id="p", schedule_id=row["id"])
    assert receipt["state"] == "fired"
    assert receipt["message_id"] == queued[0]["id"]
    assert receipt["fired_at"] == DUE + 500
    # Registration replay is stable after its instant is in the past.
    env.clock[0] = DUE + 2000
    assert (await register(env))["schedule"]["id"] == row["id"]
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(wait_source_events).where(
            wait_source_events.c.outcome_ref == f"message:{queued[0]['id']}",
        )) == 1


async def test_enabled_project_uses_logical_supervisor_mailbox(env):
    await env.db.update_project("p", supervisor_enabled=True)
    await register(env)
    await tick(env)
    notice, = await notices(env)
    assert (notice["to_kind"], notice["to_id"], notice["project_id"]) == (
        "profile", "supervisor", "p",
    )


async def test_cancelled_notice_never_fires_and_retry_does_not_rearm(env):
    row = (await register(env))["schedule"]
    args = {"project_id": "p", "schedule_id": row["id"]}
    cancelled = await env.handler.execute("schedule_cancel", args)
    assert cancelled["schedule"]["state"] == "cancelled"
    assert (await env.handler.execute("schedule_cancel", args)) == cancelled
    assert (await register(env))["schedule"]["state"] == "cancelled"
    assert (await tick(env, DUE + 100 * 86400))["fired"] == 0
    assert not await notices(env)


async def test_concurrent_register_and_scans_queue_one_message(env):
    rows = await asyncio.gather(*(register(env) for _ in range(4)))
    assert len({r["schedule"]["id"] for r in rows}) == 1
    outcomes = await asyncio.gather(tick(env), tick(env), tick(env))
    assert sum(outcome["fired"] for outcome in outcomes) == 1
    assert len(await notices(env)) == 1
    cancelled = await env.handler.execute("schedule_cancel", {
        "project_id": "p", "schedule_id": rows[0]["schedule"]["id"],
    })
    assert cancelled["schedule"]["state"] == "fired"


async def test_cancellation_winning_lock_prevents_firing(env):
    row = (await register(env))["schedule"]
    async with env.db._engine.begin() as conn:
        await conn.execute(select(project_schedules).where(
            project_schedules.c.id == row["id"],
        ).with_for_update())
        await conn.execute(update(project_schedules).where(
            project_schedules.c.id == row["id"],
        ).values(state="cancelled", cancelled_at=NOW + 10))
        assert (await tick(env))["fired"] == 0
    assert (await tick(env))["fired"] == 0
    assert not await notices(env)


async def test_failed_receipt_rolls_back_message_and_retries(env, monkeypatch):
    await register(env)
    original = env.db.create_message_in_transaction

    async def insert_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated crash before firing receipt")

    monkeypatch.setattr(env.db, "create_message_in_transaction", insert_then_fail)
    result = await tick(env)
    assert not result["success"]
    assert not await notices(env)
    monkeypatch.setattr(env.db, "create_message_in_transaction", original)
    assert (await tick(env))["fired"] == 1
    assert len(await notices(env)) == 1


async def test_normalised_key_replay_and_conflicts(env):
    row = (await register(env))["schedule"]
    offset = datetime.fromtimestamp(DUE, timezone.utc).astimezone(
        timezone(timedelta(hours=4)),
    ).isoformat()
    assert (await register(env, due_at=offset))["schedule"]["id"] == row["id"]
    for change in ({"body": "Changed"}, {"due_at": iso(DUE + 1)}):
        assert (await register(env, **change))["error_code"] == "schedule.key_conflict"


@pytest.mark.parametrize("due_at", [
    "2026-11-23T16:20:12", "later", "NaN", iso(NOW), "9999-12-31T23:59:59-12:00",
])
async def test_invalid_or_past_instant_is_refused(env, due_at):
    assert (await register(env, due_at=due_at))["error_code"] == "schedule.invalid"


async def test_visibility_pagination_and_cross_project_ids(env):
    row = (await register(env))["schedule"]
    await register(env, key="second", due_at=iso(DUE + 1))
    result = await env.handler.execute("schedule_list", {
        "project_id": "p", "state": "pending", "limit": 1, "offset": 1,
    })
    assert result["count"] == 1 and result["schedules"][0]["id"] != row["id"]
    for command in ("schedule_get", "schedule_cancel"):
        result = await env.handler.execute(command, {
            "project_id": "q", "schedule_id": row["id"],
        })
        assert result["error_code"] == "schedule.not_found"


async def test_supervisor_scope_and_worker_service_refusals(env):
    scope = dict(kind="session", session_id="sup", session_instance_token="sup-token",
                 project_id="p", elevated=True)
    for project, allowed in (("p", True), ("q", False)):
        result = await env.handler.execute("schedule_list", {
            "project_id": project, "_scope": scope,
        })
        assert bool(result.get("success")) == allowed
    scope["elevated"] = False
    result = await env.handler.execute("schedule_list", {"project_id": "p", "_scope": scope})
    assert not result.get("success")
    assert not (await env.handler.execute("reconcile_project_schedules", {"now": DUE}))["success"]
    with principal_context(ExecutionPrincipal.service("wrong-service")):
        assert not (await env.handler.execute("reconcile_project_schedules", {"now": DUE}))["success"]


async def test_short_timer_cap_is_unchanged():
    timer = TimerAdapter(clock=lambda: NOW)
    with patch.object(Adapter, "authorise", new_callable=AsyncMock) as base:
        for delta in (1, 86400):
            await timer.authorise(None, timer.normalise({"due_at": NOW + delta}), None)
        assert base.await_count == 2
    # Invalid horizons fail before the ordinary scope authorization.
    for delta in (0, 86401, 30 * 86400):
        with pytest.raises(WaitError, match="between one second and one day"):
            await timer.authorise(None, timer.normalise({"due_at": NOW + delta}), None)


def test_cli_contracts_and_internal_surface_exclusions():
    from src.api.codegen import API_EXCLUDED
    from src.cli.app import cli
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

    for action in ("register", "get", "list", "cancel"):
        assert CONTRACTS.get(f"schedule_{action}") is not None
        result = CliRunner().invoke(cli, ["schedule", action, "--help"])
        assert result.exit_code == 0, result.output
        assert "--project-id" in result.output
    assert "reconcile_project_schedules" in API_EXCLUDED
    assert "reconcile_project_schedules" in DEFAULT_EXCLUDED_COMMANDS
    assert CliRunner().invoke(cli, ["schedule", "reconcile-project-schedules"]).exit_code != 0


def test_absolute_utc_instant_has_no_horizon():
    assert utc_instant("2099-11-23T16:20:12Z") > DUE
    with pytest.raises(ScheduleError):
        utc_instant("2099-11-23T16:20:12")
