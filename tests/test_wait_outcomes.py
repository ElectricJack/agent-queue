"""Real transactional dispatch and mail acknowledgement; lifecycle hooks are explicit."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.config import AppConfig
from src.database.tables import (
    continuations, messages, sessions, tasks, wait_deadlines, wait_edges, wait_measurements,
    wait_source_events,
)
from src.messages.delivery import MessageDeliveryEngine
from src.models import TaskStatus
from src.waits.conditions import SleepDecision, WaitError
from src.waits.dispatcher import WakeDispatcher
from src.waits.holding import active_wait_for
from src.waits.listener import ListenerUnavailable
from src.waits.mail import acknowledge_in_place
from src.waits.source_locking import lock_database_source
from tests.test_wait_dispatch import runtime
from tests.test_wait_holding import mail
from tests.test_wait_queries import NOW, env as _env, records, register

env = _env


def dispatcher(env, **kwargs):
    return WakeDispatcher(env.registry, clock=lambda: NOW + 5,
                          lock_source=lock_database_source,
                          recompute_priority=kwargs.pop("recompute_priority", AsyncMock()), **kwargs)


async def waiting(env, **kwargs):
    receipt = await register(env, **kwargs)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="WAITING"))
    return receipt


async def test_source_commit_one_wake_held_mail_and_atomic_ack(env, monkeypatch):
    prior = await mail(env.db, "owner")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(messages).where(messages.c.id == prior.id).values(
            delivered_at=NOW - 1,
        ))
    receipt = await waiting(env)
    routine = await mail(env.db, "owner")
    disp = dispatcher(env)
    service = runtime(env, dispatcher=disp)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    wait = (await records(env))[0]
    assert wait["outcome"] == "satisfied" and wait["cleared_at"] is None
    assert wait["decision_inputs"]["registration_receipt"] == receipt
    assert all(row["cancelled_at"] is not None for row in await records(env, wait_deadlines))
    assert all(row["active"] is False for row in await records(env, wait_edges))
    assert len(await records(env, wait_measurements)) == 1
    disp.recompute_priority.assert_awaited_once()
    envelope = next(row for row in await records(env, messages) if row["body_kind"] == "wait_outcome")
    body = json.loads(envelope["body"])
    assert body["held_messages"] == [routine.id]
    assert body["result_ref"].startswith("task:source@0:")
    lens = SimpleNamespace(activity=AsyncMock(return_value="idle"),
                           nudge=AsyncMock(return_value=True), ensure_started=AsyncMock())
    engine = MessageDeliveryEngine(env.db, lens, AppConfig())
    monkeypatch.setattr("src.messages.delivery.time.time", lambda: NOW + 6)
    assert await engine.run_delivery_pass()
    assert lens.nudge.await_count == 1
    # A successful nudge is not repeated, even after the normal retry backoff.
    monkeypatch.setattr("src.messages.delivery.time.time", lambda: NOW + 1000)
    await engine.run_delivery_pass()
    assert lens.nudge.await_count == 1
    assert await active_wait_for(env.db, await env.db.get_session("s"))
    assert (await env.db.get_message(routine.id)).delivered_at is None
    # A later source event cannot create a second outcome/measurement/message.
    await env.db.wait_source_commit(source_key="task:source", generation=0, revision="later",
                                   outcome_ref="not-the-result", payload={})
    assert await service.tick()
    assert len(await records(env, wait_measurements)) == 1
    late = await mail(env.db, "owner")
    async with env.db._engine.begin() as conn:
        ack = await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope,
                                         now=NOW + 1001)
    assert {row["id"] for row in ack["held_messages"]} == {routine.id, late.id}
    assert await active_wait_for(env.db, await env.db.get_session("s")) is None
    assert (await env.db.get_task("owner")).status == TaskStatus.IN_PROGRESS
    assert (await env.db.get_message(late.id)).delivered_at == NOW + 1001
    assert (await env.db.get_message(prior.id)).delivered_at == NOW - 1
    assert await env.db.message_reply_window_start(prior.id) == NOW + 1001
    assert await env.db.get_task_meta("owner", "stall_last_action") == str(NOW + 1001)
    async with env.db._engine.begin() as conn:
        assert await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope,
                                          now=NOW + 1002) == ack


async def test_missing_priority_owner_rolls_back_outcome_and_leased_cursor(env):
    await waiting(env)
    disp = dispatcher(env, recompute_priority=None)
    service = runtime(env, dispatcher=disp)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    with pytest.raises(ListenerUnavailable, match="priority"):
        await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert not await records(env, wait_measurements)
    assert not await records(env, messages)
    assert all(row["active"] for row in await records(env, wait_edges))
    assert all(row["dispatched_at"] is None for row in await records(env, wait_source_events))
    disp.recompute_priority = AsyncMock()
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"


async def test_false_payload_and_stale_holder_do_not_wake(env):
    await waiting(env)
    service = runtime(env, dispatcher=dispatcher(env))
    await env.db.wait_source_commit(source_key="task:source", generation=0, revision="lie",
                                   outcome_ref="false", payload={"status": "COMPLETED"})
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert not await records(env, messages)
    assert (await records(env))[0]["outcome"] == "pending"


async def test_outcome_and_held_mail_ack_rollback_together(env):
    receipt = await waiting(env)
    msg = await mail(env.db, "owner")
    service = runtime(env, dispatcher=dispatcher(env))
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    with pytest.raises(RuntimeError, match="abort"):
        async with env.db._engine.begin() as conn:
            await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope, now=NOW + 6)
            raise RuntimeError("abort")
    assert (await env.db.get_message(msg.id)).delivered_at is None
    assert await active_wait_for(env.db, await env.db.get_session("s"))
    assert (await env.db.get_task("owner")).status == TaskStatus.WAITING


async def test_pending_wait_cannot_be_acknowledged(env):
    receipt = await waiting(env)
    async with env.db._engine.begin() as conn:
        with pytest.raises(WaitError, match="no committed outcome"):
            await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope, now=NOW)


@pytest.mark.parametrize("replacement", ["claim", "process"])
async def test_committed_wake_never_nudges_replacement_holder(env, replacement):
    await waiting(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await runtime(env, dispatcher=dispatcher(env)).tick()
    async with env.db._engine.begin() as conn:
        if replacement == "claim":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        else:
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(
                instance_token="replacement",
            ))
    lens = SimpleNamespace(activity=AsyncMock(return_value="idle"), nudge=AsyncMock(),
                           ensure_started=AsyncMock())
    await MessageDeliveryEngine(env.db, lens, AppConfig()).run_delivery_pass()
    lens.nudge.assert_not_called()
    assert all(row["notification_attempts"] == 0 for row in await records(env, messages))


async def test_new_registration_requires_acknowledgement(env):
    await waiting(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await runtime(env, dispatcher=dispatcher(env)).tick()
    with pytest.raises(WaitError, match="already has an execution wait"):
        await register(env, {"kind": "timer", "due_at": NOW + 30}, idempotency_key="next")


async def test_heartbeat_is_successful_without_inventing_activity(env):
    await waiting(env)
    handler = runtime(env).orch._command_handler
    before = await env.db.get_session("s")
    agent = await env.db.get_agent("a")
    result = await handler.execute("task_heartbeat", {
        "task_id": "owner", "session_id": "s", "claim_epoch": 1,
    })
    assert result == {"success": True, "ignored": "wait_active"}
    assert (await env.db.get_session("s")).last_activity == before.last_activity
    assert (await env.db.get_agent("a")).last_heartbeat == agent.last_heartbeat


@pytest.mark.parametrize("proven_stop", [True, False])
async def test_parked_continuation_queues_once_without_message(env, proven_stop):
    receipt = await register(env, decide_sleep=lambda *args: SleepDecision("hibernate", "test"))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            status="HIBERNATED", claim_epoch=2, assigned_agent_id=None,
        ))
        if proven_stop:
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(
                state="stopped", desired_state="stopped", end_reason="hibernated",
            ))
        await conn.execute(insert(continuations).values(
            id="continuation", task_id="owner", wait_id=receipt["wait_id"],
            suspension_generation=1, facts={}, branch="aq/owner", prior_status="WAITING",
            held_cursor_seq=0, resume_state="sleeping", stopped_at=NOW + 1,
        ))
    service = runtime(env, dispatcher=dispatcher(env))
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    if not proven_stop:
        assert (await env.db.get_task("owner")).status == TaskStatus.HIBERNATED
        assert (await records(env, continuations))[0]["resume_state"] == "sleeping"
        assert not await records(env, wait_measurements)
        assert not await records(env, messages)
        return
    assert (await env.db.get_task("owner")).status == TaskStatus.RESUME_PENDING
    assert (await records(env, continuations))[0]["resume_state"] == "queued"
    assert not await records(env, messages)
    assert len(await records(env, wait_measurements)) == 1
    assert await service.tick()
    assert (await records(env, continuations))[0]["version"] == 2
