"""Real transactional dispatch and mail acknowledgement; lifecycle hooks are explicit."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.config import AppConfig
from src.database.tables import (
    continuations, escalations, messages, sessions, tasks, wait_deadlines, wait_edges,
    wait_escalations, wait_measurements,
    wait_source_events,
)
from src.messages.delivery import MessageDeliveryEngine
from src.models import Task, TaskStatus
from src.waits.conditions import Scope, SleepDecision, WaitError
from src.waits.dispatcher import WakeDispatcher
from src.waits.holding import active_wait_for
from src.waits.graph import recompute_from
from src.waits.listener import WaitSourceListener
from src.waits.mail import acknowledge_in_place
from src.waits.source_locking import lock_database_source
from tests.test_wait_dispatch import runtime
from tests.test_wait_holding import mail
from tests.test_wait_queries import NOW, env as _env, records, register

env = _env


def dispatcher(env, **kwargs):
    return WakeDispatcher(env.registry, clock=lambda: NOW + 5,
                          lock_source=lock_database_source,
                          recompute_priority=kwargs.pop(
                              "recompute_priority", AsyncMock(wraps=recompute_from)), **kwargs)


async def waiting(env, **kwargs):
    receipt = await register(env, **kwargs)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="WAITING"))
    return receipt


@pytest.mark.parametrize('source_epoch,outcome', [(0, 'pending'), (1, 'source_unavailable')])
async def test_live_owner_does_not_make_in_progress_source_unavailable(env, source_epoch, outcome):
    await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'source').values(
            status='IN_PROGRESS', claim_epoch=source_epoch,
        ))
    # An older committed hint must re-observe the current source generation.
    await env.db.wait_source_commit(source_key='task:source', generation=0, revision='hint',
                                   outcome_ref='untrusted', payload={})
    service = runtime(env, dispatcher=dispatcher(env))
    assert await service.tick()
    wait = (await records(env))[0]
    assert (await env.db.get_task('owner')).status is TaskStatus.IN_PROGRESS
    assert wait['outcome'] == outcome
    if source_epoch:
        assert wait['decision_inputs']['source_unavailable']['reason'] == 'source_changed'
        assert wait['cleared_at'] is None
    else:
        assert 'source_unavailable' not in wait['decision_inputs']


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
    assert await register(env) == receipt
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
    assert await register(env) == receipt
    async with env.db._engine.begin() as conn:
        assert await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope,
                                          now=NOW + 1002) == ack


async def test_missing_priority_owner_rolls_back_outcome_and_leased_cursor(env):
    await waiting(env)
    disp = dispatcher(env, recompute_priority=None)
    service = runtime(env, dispatcher=disp)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert not await records(env, wait_measurements)
    assert not await records(env, messages)
    assert all(row["active"] for row in await records(env, wait_edges))
    assert all(row["dispatched_at"] is None for row in await records(env, wait_source_events))
    assert all(row["dispatch_failures"] == 1 for row in await records(env, wait_source_events))
    disp.recompute_priority = recompute_from
    service.listener.clock = lambda: NOW + 31
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"


async def test_existing_outcome_measurement_preserves_sample_and_commits_wake(env):
    receipt = await waiting(env)
    sample = dict(
        id=f"outcome:{receipt['wait_id']}", project_id="p", wait_id=receipt["wait_id"],
        condition_kind="task", source_class="task", decision="in_place",
        waited_seconds=3, hibernated=True, evicted=False, recorded_at=NOW + 3,
        setup_seconds=7, rebuild_seconds=11,
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_measurements).values(**sample))
    service = runtime(env, dispatcher=dispatcher(env))
    await env.db.transition_task("source", TaskStatus.COMPLETED)

    assert await service.listener.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"
    measured = await records(env, wait_measurements)
    assert len(measured) == 1
    assert {key: measured[0][key] for key in sample} == sample
    assert len([row for row in await records(env, messages)
                if row["body_kind"] == "wait_outcome"]) == 1
    source_events = [row for row in await records(env, wait_source_events)
                     if row["source_key"] == "task:source"]
    assert source_events and all(row["dispatched_at"] is not None for row in source_events)
    assert await service.listener.tick()
    assert await records(env, wait_measurements) == measured


async def test_real_graph_retraction_rolls_back_then_commits_with_outcome(env):
    async def priorities():
        return {row["id"]: row["effective_priority"] for row in await records(env, tasks)}

    await env.db.update_task("owner", priority=10)
    await env.db.update_task("source", priority=100)
    await env.db.create_task(Task(id="tail", project_id="p", title="Tail", description="",
                                  priority=200))
    scheduler_receipt = await register(
        env, {"kind": "task", "task_id": "tail"},
        scope=Scope("p", "scheduler", "source", principal="service"),
        idempotency_key="source-to-tail",
    )
    receipt = await waiting(env)
    for task_id in ("source", "tail"):
        assert (await priorities())[task_id] == 10

    async def fail_after_recompute(conn, changed_keys, *, now):
        assert {"task:owner", "task:source"} <= changed_keys
        await recompute_from(conn, changed_keys, now=now)
        raise RuntimeError("after retraction")

    disp = dispatcher(env, recompute_priority=fail_after_recompute)
    service = runtime(env, dispatcher=disp)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    for task_id in ("source", "tail"):
        assert (await priorities())[task_id] == 10
    assert not await records(env, messages)
    assert all(row["outcome"] == "pending" for row in await records(env))
    assert all(row["cleared_at"] is None for row in await records(env))
    assert all(row["active"] for row in await records(env, wait_edges))
    disp.recompute_priority = recompute_from
    service.listener.clock = lambda: NOW + 31
    assert await service.tick()
    assert (await priorities())["source"] == 100
    # The completed source also retires its own scheduler wait, retracting the
    # remaining source-to-tail donation in the same outcome transaction.
    assert (await priorities())["tail"] == 200
    assert (await env.db.get_task("tail")).priority == 200
    retired = next(row for row in await records(env)
                   if row["id"] == scheduler_receipt["wait_id"])
    assert retired["outcome"] == "cancelled" and retired["cleared_at"] is not None
    assert retired["decision_inputs"]["registration_receipt"] == scheduler_receipt
    assert not any(row["active"] for row in await records(env, wait_edges))
    terminal = next(row for row in await records(env) if row["id"] == receipt["wait_id"])
    assert terminal["outcome"] == "satisfied" and terminal["cleared_at"] is None
    assert len(await records(env, messages)) == 1


@pytest.mark.parametrize("callback", [None, "refuse"])
async def test_unavailable_escalation_resolution_rolls_back_entire_outcome(env, callback):
    receipt = await waiting(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(escalations).values(
            id="incident", project_id="p", task_id="owner", source_kind="wait",
            source_identity=receipt["wait_id"], incident_key=f"wait:{receipt['wait_id']}:1",
            supervisor_owner="supervisor-p", task_title="Owner", summary="Waiting",
            investigation="Verified wait", decision_requested="Choose", severity="medium",
            created_at=NOW, updated_at=NOW,
        ))
        await conn.execute(insert(wait_escalations).values(
            id="wait-incident", wait_id=receipt["wait_id"], generation=1,
            escalation_id="incident", raised_at=NOW, original_age_seconds=0,
        ))
    resolver = AsyncMock(return_value={"success": False}) if callback else None
    service = runtime(env, dispatcher=dispatcher(env, resolve_escalation=resolver))
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert all(row["active"] for row in await records(env, wait_edges))
    assert all(row["cancelled_at"] is None for row in await records(env, wait_deadlines))
    assert all(row["dispatched_at"] is None for row in await records(env, wait_source_events))
    assert not await records(env, wait_measurements)
    assert not await records(env, messages)


@pytest.mark.parametrize("replacement", ["claim", "process"])
async def test_ack_refuses_replacement_holder_without_stamping_mail(env, replacement):
    receipt = await waiting(env)
    routine = await mail(env.db, "owner")
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await runtime(env, dispatcher=dispatcher(env)).tick()
    async with env.db._engine.begin() as conn:
        if replacement == "claim":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        else:
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(
                instance_token="replacement",
            ))
    with pytest.raises(WaitError):
        async with env.db._engine.begin() as conn:
            await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope,
                                       now=NOW + 6)
    assert (await env.db.get_message(routine.id)).delivered_at is None
    assert (await records(env))[0]["cleared_at"] is None


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


@pytest.mark.parametrize("kind", ["task", "message"])
@pytest.mark.parametrize("parked", [False, True])
async def test_held_hibernate_wait_retains_source_without_blocking_other_waits(env, kind, parked):
    condition = {"kind": "task", "task_id": "source"}
    if kind == "message":
        question = await env.db.create_message(
            project_id="p", from_kind="session", from_id="s", to_kind="session",
            to_id="supervisor-p", thread_id="held-thread", body="Question",
        )
        condition = {"kind": "message", "thread_id": "held-thread",
                     "after_seq": question.created_seq}
    receipt = await register(env, condition,
                             decide_sleep=lambda *_: SleepDecision("hibernate", "test"))
    other = await register(env, scope=Scope("p", "scheduler", "other", principal="service"),
                           idempotency_key="other")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            status="HIBERNATED" if parked else "WAITING",
            claim_epoch=2 if parked else 1, assigned_agent_id=None if parked else "a",
        ))
        if parked:
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(
                state="stopped", desired_state="stopped", end_reason="hibernated",
            ))
        await conn.execute(insert(continuations).values(
            id="held", task_id="owner", wait_id=receipt["wait_id"], suspension_generation=1,
            facts={"hold_escalation_id": "existing-hold"}, branch="aq/owner",
            prior_status="WAITING", held_cursor_seq=0, resume_state="held",
            hold_reason="push_failed", stopped_at=NOW + 1 if parked else None,
        ))
    held = (await records(env, continuations))[0]
    owner_before = await env.db.get_task("owner")
    if kind == "message":
        await env.db.create_message(
            project_id="p", from_kind="session", from_id="supervisor-p", to_kind="session",
            to_id="s", thread_id="held-thread", body="Answer",
        )
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    now = [NOW]
    service = runtime(env, clock=lambda: now[0], dispatcher=dispatcher(env))
    assert await service.listener.tick()
    waits = {row["id"]: row for row in await records(env)}
    assert waits[receipt["wait_id"]]["outcome"] == "pending"
    assert waits[other["wait_id"]]["outcome"] == "satisfied"
    assert (await records(env, continuations))[0] == held
    assert await env.db.get_task("owner") == owner_before
    retained = [row for row in await records(env, wait_source_events)
                if row["source_key"] == waits[receipt["wait_id"]]["source_key"]
                and row["dispatched_at"] is None]
    assert retained and all(row["dispatch_retry_at"] == NOW + 30 for row in retained)
    assert not [row for row in await records(env, messages) if row["body_kind"] == "wait_outcome"]
    # Leadership turnover must not erase backoff or mutate the held continuation.
    await service.listener.lease(release=True)
    service.listener = WaitSourceListener(service, clock=lambda: now[0])
    assert await service.listener.tick()
    assert (await records(env, continuations))[0] == held
    # Emulate the separate recovery owner's proved park. Reuse the retained
    # source, without publishing a replacement event or weakening its fences.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            status="HIBERNATED", claim_epoch=2, assigned_agent_id=None,
        ))
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(
            state="stopped", desired_state="stopped", end_reason="hibernated",
        ))
        await conn.execute(update(continuations).where(continuations.c.id == "held").values(
            resume_state="sleeping", hold_reason=None, stopped_at=NOW + 1,
        ))
    now[0] += 31
    assert await service.listener.tick()
    assert next(row for row in await records(env) if row["id"] == receipt["wait_id"])[
        "outcome"] == "satisfied"
    assert (await records(env, continuations))[0]["resume_state"] == "queued"


async def test_hibernate_intent_persistence_race_retains_only_affected_wait(env):
    receipt = await register(env, decide_sleep=lambda *_: SleepDecision("hibernate", "test"))
    other = await register(env, scope=Scope("p", "scheduler", "other", principal="service"),
                           idempotency_key="other")
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    service = runtime(env, dispatcher=dispatcher(env))
    assert await service.listener.tick()
    waits = {row["id"]: row for row in await records(env)}
    assert waits[receipt["wait_id"]]["outcome"] == "pending"
    assert waits[other["wait_id"]]["outcome"] == "satisfied"
    source = (await records(env, wait_source_events))[0]
    assert source["dispatched_at"] is None and source["dispatch_retry_at"] == NOW + 30


async def test_scheduler_hibernate_wait_does_not_require_an_agent_continuation(env):
    await env.db.create_task(Task(id="tail", project_id="p", title="Tail", description=""))
    scheduler = await register(
        env, {"kind": "task", "task_id": "tail"},
        scope=Scope("p", "scheduler", "source", principal="service"),
        decide_sleep=lambda *args: SleepDecision("hibernate", "scheduler"),
    )
    agent = await waiting(env)
    await env.db.transition_task("tail", TaskStatus.COMPLETED)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    service = runtime(env, dispatcher=dispatcher(env))
    assert await service.tick()
    waits = {row["id"]: row for row in await records(env)}
    assert waits[scheduler["wait_id"]]["outcome"] == "satisfied"
    assert waits[scheduler["wait_id"]]["cleared_at"] is not None
    assert waits[agent["wait_id"]]["outcome"] == "satisfied"
    assert not await records(env, continuations)
    envelopes = [row for row in await records(env, messages) if row["body_kind"] == "wait_outcome"]
    assert len(envelopes) == 1 and envelopes[0]["to_id"] == "s"
    assert await service.tick()
    assert len([row for row in await records(env, messages)
                if row["body_kind"] == "wait_outcome"]) == 1
