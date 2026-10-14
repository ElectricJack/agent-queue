"""Verified waits suppress routine delivery until the holder acknowledges."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.config import AppConfig
from src.database.tables import sessions, tasks, wait_records
from src.messages.delivery import MessageDeliveryEngine
from src.waits.holding import active_wait_for
from tests.test_wait_queries import NOW, env as _env, owner, register

env = _env


async def mail(db, recipient, *, kind="task", body_kind=None):
    return await db.create_message(
        project_id="p", from_kind="system", from_id="patrol", to_kind=kind,
        to_id=recipient, body="Routine patrol", body_kind=body_kind,
    )


async def test_nine_waiters_hold_routine_mail_through_outcome_delivery(env):
    holders = [env.scope]
    for n in range(1, 9):
        holders.append(await owner(env.db, f"owner-{n}", f"s-{n}", f"a-{n}"))
    routine = []
    for scope in holders:
        await register(env, scope=scope)
        routine.append(await mail(env.db, scope.owner_id, body_kind="report_request"))
        routine.append(await mail(env.db, scope.session_id, kind="session"))
        routine.append(await mail(env.db, f"wait-{scope.session_id}", kind="session",
                                  body_kind="conversation_input"))
    lens = SimpleNamespace(activity=AsyncMock(return_value="idle"), nudge=AsyncMock(),
                           ensure_started=AsyncMock())
    engine = MessageDeliveryEngine(env.db, lens, AppConfig())
    for _ in range(3):
        assert (await engine.run_delivery_pass())["delivered"] == 0
    lens.activity.assert_not_called()
    lens.nudge.assert_not_called()
    for message in routine:
        assert not await env.db.mark_delivered(message.id)
        assert not await env.db.mark_read(message.id)
        assert not await env.db.claim_message_notification(message.id, now=NOW, retry_delay=0)
        row = await env.db.get_message(message.id)
        assert row.notification_attempts == 0
        assert row.archived_at is None and row.delivered_at is None
    # A committed and delivered outcome is still not acknowledgement.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(
            outcome="satisfied", resolved_at=NOW + 1, wake_delivered_at=NOW + 2,
        ))
    assert not await env.db.get_pending_messages("task", "owner")
    assert await active_wait_for(env.db, await env.db.get_session("s")) is not None
    # The acknowledgement owner is the only writer of this terminal boundary.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(cleared_at=NOW + 3))
    assert await active_wait_for(env.db, await env.db.get_session("s")) is None
    assert len(await env.db.get_pending_messages("task", "owner")) == 1


@pytest.mark.parametrize("kind", ["wait_outcome", "wait_suspend", "wait_decision"])
async def test_wake_messages_cannot_be_starved_by_held_mail_limit(env, kind):
    await register(env)
    for _ in range(5):
        await mail(env.db, "s", kind="session")
    wake = await mail(env.db, "s", kind="session", body_kind=kind)
    assert [row.id for row in await env.db.get_pending_messages("session", "s", limit=1)] == [wake.id]
    assert await env.db.mark_delivered(wake.id)


@pytest.mark.parametrize("changed", ["epoch", "instance", "stop", "pause", "agent", "phase"])
async def test_stale_or_stopped_holder_never_gets_wait_exemption(env, changed):
    await register(env)
    row = await env.db.get_session("s")
    assert await active_wait_for(env.db, row) is not None
    async with env.db._engine.begin() as conn:
        if changed == "epoch":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        elif changed == "pause":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="PAUSED"))
        else:
            values = {"instance": {"instance_token": "replaced"},
                      "stop": {"desired_state": "stopped"},
                      "agent": {"agent_id": None}, "phase": {"claim_phase": None}}[changed]
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(**values))
    assert await active_wait_for(env.db, row) is None


async def test_registration_race_audit_grants_no_exemption(env):
    original = env.registry.observe

    async def satisfied(conn, cond):
        from src.waits.conditions import Observation
        return Observation("raced", "satisfied", "task:source:COMPLETED")

    env.registry.observe = satisfied
    receipt = await register(env)
    env.registry.observe = original
    assert receipt["code"] == "wait.already_satisfied"
    assert await active_wait_for(env.db, await env.db.get_session("s")) is None
