"""Operational deadlines never wake workers; only fenced supervisor decisions do."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.database.tables import (
    continuations, escalation_deliveries, escalations, events, messages, sessions,
    task_comments, task_context, tasks, wait_escalations, wait_records,
)
from src.database.queries.continuation_queries import wait_outcome_envelope
from src.event_schemas import validate_event
from src.messages.delivery import MessageDeliveryEngine
from src.config import AppConfig
from src.models import TaskStatus
from src.waits.holding import active_wait_for
from src.waits.conditions import Condition
from src.waits.mail import acknowledge_in_place
from tests.test_wait_dispatch import runtime
from tests.test_wait_holding import mail
from tests.test_wait_queries import NOW, env as _env, records, register
from tests.test_wait_outcomes import waiting

env = _env


def service_for(env, now):
    service = runtime(env, clock=lambda: now[0])
    del service.orch.wait_dispatcher
    policy = AppConfig().wait_policy
    policy.v2_enabled = True
    service.orch.config.wait_policy = policy
    return service


async def escalated(env, *, hibernate=False):
    now = [NOW + 60]
    service = service_for(env, now)
    receipt = await waiting(env, escalate_after_seconds=60)
    if hibernate:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(sleep_decision="hibernate"))
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
                status="HIBERNATED", assigned_agent_id=None, claim_epoch=2,
            ))
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(
                state="stopped", desired_state="stopped", end_reason="hibernated",
            ))
            await conn.execute(insert(continuations).values(
                id="continuation", wait_id=receipt["wait_id"], task_id="owner",
                suspension_generation=1, summary="Continue the implementation", facts={},
                branch="aq/owner", saved_head="a" * 40, prior_status="IN_PROGRESS",
                held_cursor_seq=0, resume_state="sleeping", created_at=NOW, stopped_at=NOW + 1,
            ))
    assert await service.lease()
    args = {"wait_id": receipt["wait_id"], "deadline_type": "escalation", "generation": 1}
    result = await service.command("wait_deadline_fire", args)
    assert result["success"], result
    return receipt, service, now, args


async def decide(service, receipt, decision, **extra):
    return await service.orch._command_handler.execute("wait_decide", {
        "wait_id": receipt["wait_id"], "decision": decision, "note": "Supervisor reason",
        **extra,
    })


async def test_expiry_one_supervisor_incident_no_worker_or_human_delivery(env):
    receipt, service, now, args = await escalated(env)
    result = await service.command("wait_deadline_fire", args)
    assert result["error_code"] == "wait.deadline_stale"
    wait = (await records(env))[0]
    assert wait["outcome"] == "pending" and wait["created_at"] == NOW
    assert wait["decision_inputs"]["registration_receipt"] == receipt
    notices = await records(env, messages)
    assert len(notices) == 1 and notices[0]["body_kind"] == "wait_escalation"
    assert notices[0]["to_id"] == "supervisor-p"
    assert len(await records(env, wait_escalations)) == 1
    assert not await records(env, escalation_deliveries)
    assert (await env.db.get_task("owner")).status is TaskStatus.WAITING


async def test_extend_preserves_age_receipt_ceiling_and_allows_next_incident(env):
    receipt, service, now, args = await escalated(env)
    before = (await records(env))[0]
    result = await decide(service, receipt, "extend", extend_seconds=120, expected_generation=1)
    assert result["success"], result
    wait = (await records(env))[0]
    for key in ("created_at", "ceiling_anchor_at", "decision_inputs", "outcome", "blocking"):
        assert wait[key] == before[key]
    assert wait["escalate_at"] == now[0] + 120
    assert len(await records(env, messages)) == 1
    first = (await records(env, escalations))[0]
    assert first["state"] == "cancelled" and first["terminal_outcome"] == "extend"
    now[0] += 120
    assert await service.lease()
    assert (await service.command("wait_deadline_fire", {**args, "generation": 2}))["success"]
    incidents = await records(env, wait_escalations)
    assert len(incidents) == 2
    second = next(row for row in incidents if row["generation"] == 2)
    assert second["original_age_seconds"] == 180
    assert second["prior_escalation_id"] == next(row["id"] for row in incidents
                                                if row["generation"] == 1)
    assert len(await records(env, escalations)) == 2
    assert not (await decide(service, receipt, "wake", expected_generation=1))["success"]
    assert (await records(env))[0]["outcome"] == "pending"
    assert (await decide(service, receipt, "wake", expected_generation=2))["success"]
    assert (await records(env))[0]["outcome"] == "cancelled"
    resolved = {row["id"]: row for row in await records(env, escalations)}
    assert resolved[first["id"]]["terminal_outcome"] == "extend"
    assert all(row["state"] == "cancelled" for row in resolved.values())


async def test_same_deadline_generation_concurrently_creates_one_incident(env):
    now = [NOW + 60]
    service = service_for(env, now)
    receipt = await waiting(env, escalate_after_seconds=60)
    assert await service.lease()
    args = {"wait_id": receipt["wait_id"], "deadline_type": "escalation", "generation": 1}
    results = await asyncio.gather(*(service.command("wait_deadline_fire", args) for _ in range(2)))
    assert sum(result["success"] for result in results) == 1, results
    assert next(result for result in results if not result["success"])["error_code"] == (
        "wait.deadline_stale"
    )
    assert len(await records(env, escalations)) == len(await records(env, wait_escalations)) == 1
    notices = await records(env, messages)
    assert len(notices) == 1 and notices[0]["body_kind"] == "wait_escalation"
    assert not await records(env, escalation_deliveries)
    wait = (await records(env))[0]
    assert wait["outcome"] == "pending" and wait["created_at"] == NOW
    assert wait["decision_inputs"]["registration_receipt"] == receipt


async def test_wake_is_cancelled_one_envelope_held_mail_and_exemption_until_ack(env, monkeypatch):
    receipt, service, now, _ = await escalated(env)
    routine = await mail(env.db, "owner")
    result = await decide(service, receipt, "wake", expected_generation=1)
    assert result["success"] and result["outcome"] == "cancelled", result
    wait = (await records(env))[0]
    assert wait["outcome"] == "cancelled" and wait["cleared_at"] is None
    assert wait["decision_inputs"]["registration_receipt"] == receipt
    envelope = next(row for row in await records(env, messages) if row["body_kind"] == "wait_decision")
    body = json.loads(envelope["body"])
    assert body["outcome"] == "cancelled" and body["decision"]["decision"] == "wake"
    assert body["decision"]["note"] == "Supervisor reason"
    assert body["held_messages"] == [routine.id]
    assert await active_wait_for(env.db, await env.db.get_session("s"))
    lens = type("Lens", (), {"activity": AsyncMock(return_value="idle"),
                              "nudge": AsyncMock(return_value=True),
                              "ensure_started": AsyncMock()})()
    engine = MessageDeliveryEngine(env.db, lens, AppConfig())
    # The supervisor notice may be retried; count only the worker's wake.
    monkeypatch.setattr("src.messages.delivery.time.time", lambda: now[0])
    await engine.run_delivery_pass()
    now[0] += 1000
    await engine.run_delivery_pass()
    assert len([call for call in lens.nudge.await_args_list
                if call.kwargs.get("target_id") == "s"]) == 1
    assert (await env.db.get_message(routine.id)).delivered_at is None
    async with env.db._engine.begin() as conn:
        await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope, now=now[0])
    assert not await active_wait_for(env.db, await env.db.get_session("s"))
    assert (await env.db.get_message(routine.id)).delivered_at == now[0]
    assert not (await decide(service, receipt, "wake"))["success"]


@pytest.mark.parametrize("hibernate", [False, True])
async def test_cancel_holds_paused_without_worker_wake_or_resume(env, hibernate):
    receipt, service, _, _ = await escalated(env, hibernate=hibernate)
    routine = await mail(env.db, "owner")
    result = await decide(service, receipt, "cancel")
    assert result["success"], result
    assert (await env.db.get_task("owner")).status is TaskStatus.PAUSED
    assert (await env.db.get_session("s")).desired_state == "stopped"
    assert (await env.db.get_message(routine.id)).delivered_at is None
    assert not [row for row in await records(env, messages)
                if row["body_kind"] in {"wait_decision", "wait_outcome"}]
    assert all(row["resume_state"] == "cancelled" for row in await records(env, continuations))


async def test_parked_wake_queues_one_intent_without_launch(env):
    receipt, service, _, _ = await escalated(env, hibernate=True)
    assert (await decide(service, receipt, "wake"))["success"]
    assert (await env.db.get_task("owner")).status is TaskStatus.RESUME_PENDING
    assert (await records(env, continuations))[0]["resume_state"] == "queued"
    assert len(await records(env, sessions)) == 1
    assert not [row for row in await records(env, messages) if row["body_kind"] == "wait_decision"]


@pytest.mark.parametrize("fence", ["process", "claim", "pause", "source"])
async def test_decision_rechecks_owner_and_source_fences(env, fence):
    receipt, service, _, _ = await escalated(env)
    async with env.db._engine.begin() as conn:
        if fence == "process":
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(instance_token="new"))
        elif fence == "claim":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        elif fence == "pause":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="PAUSED"))
        else:
            await conn.execute(update(tasks).where(tasks.c.id == "source").values(status="COMPLETED"))
    assert not (await decide(service, receipt, "wake"))["success"]
    assert (await records(env))[0]["outcome"] == "pending"
    assert len(await records(env, messages)) == 1
    assert (await records(env, wait_escalations))[0]["decided_at"] is None


async def test_source_satisfaction_with_escalation_is_one_atomic_commit(env):
    receipt, service, _, _ = await escalated(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"
    incident = (await records(env, escalations))[0]
    assert incident["state"] == "cancelled" and incident["terminal_outcome"] == "source_satisfied"
    assert incident["terminal_evidence"]["source_seq"]
    assert not (await decide(service, receipt, "wake"))["success"]


@pytest.mark.parametrize("hibernate", [False, True])
async def test_human_conversion_uses_verified_thread_and_no_operational_wake(env, hibernate):
    receipt, service, _, _ = await escalated(env, hibernate=hibernate)
    result = await decide(service, receipt, "escalate_to_human")
    assert result["success"], result
    wait = (await records(env))[0]
    assert wait["condition_kind"] == "human" and wait["sleep_decision"] == "hibernate"
    assert wait["outcome"] == "pending" and wait["escalate_at"] is None
    assert wait["decision_inputs"]["registration_receipt"] == receipt
    assert len(await records(env, escalation_deliveries)) == 1
    assert not [row for row in await records(env, messages) if row["body_kind"] == "wait_decision"]
    incident = (await records(env, escalations))[0]
    await env.db.accept_escalation_reply(incident["id"], transport="test", external_message_id="reply",
                                        verified_actor="user:jack", text="Proceed", received_at=NOW+61)
    cond = Condition.from_record(wait["condition"])
    async with env.db._engine.connect() as conn:
        assert (await service.registry.observe(conn, cond)).state == "satisfied"
    assert (await records(env))[0]["outcome"] == "pending"
    if hibernate:
        assert await service.tick()
        assert (await records(env))[0]["outcome"] == "satisfied"
        assert (await records(env, continuations))[0]["resume_state"] == "queued"
        assert (await env.db.get_task("owner")).status is TaskStatus.RESUME_PENDING
        assert (await records(env, escalations))[0]["terminal_outcome"] == "source_satisfied"


async def complete_and_ack(env, service, receipt, now):
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    async with env.db._engine.begin() as conn:
        await acknowledge_in_place(conn, wait_id=receipt["wait_id"], scope=env.scope, now=now[0])
    await env.db.transition_task("source", TaskStatus.DEFINED)
    now[0] += 1


async def test_third_identical_registration_without_progress_hibernates_and_escalates(env):
    now = [NOW + 1]
    service = service_for(env, now)
    for number in range(2):
        receipt = await waiting(env, idempotency_key=f"loop-{number}", now=now[0])
        await complete_and_ack(env, service, receipt, now)
        async with env.db._engine.begin() as conn:
            await conn.execute(insert(events).values(
                event_type="task.updated", project_id="p", task_id="owner",
                timestamp=now[0], payload=json.dumps({"status": "IN_PROGRESS"}),
            ))
    receipt = await register(env, idempotency_key="loop-third", now=now[0])
    assert receipt["decision"]["reason"] == "wait.loop_detected"
    assert receipt["decision"]["mode"] == "hibernate"
    assert receipt["next_step"] == "checkpoint_then_end_turn" and receipt["ceiling_at"] is None
    assert len(await records(env, wait_escalations)) == 1
    incident = (await records(env, escalations))[0]
    assert "loop_registrations" in incident["investigation"]
    assert len([row for row in await records(env, events)
                if row["event_type"] == "continuation.loop_detected"]) == 1
    for event in await records(env, events):
        if event["event_type"].startswith(("wait.", "continuation.", "escalation.")):
            assert not validate_event(event["event_type"], json.loads(event["payload"]))
    assert await register(env, idempotency_key="loop-third", now=now[0]) == receipt


@pytest.mark.parametrize("change", ["comment", "comment_same_timestamp", "task_set",
                                  "supervisor_wake", "window"])
async def test_owner_progress_or_supervisor_wake_resets_loop_window(env, change):
    now = [NOW + 1]
    service = service_for(env, now)
    if change == "comment_same_timestamp":
        async with env.db._engine.begin() as conn:
            await conn.execute(insert(task_comments).values(
                id="z-previous-comment", task_id="owner", body="Previous progress",
                author_kind="agent", author_id="s", created_at=NOW,
            ))
    receipt = await waiting(env, idempotency_key="loop-first", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    if change == "comment":
        await env.db.add_task_comment("owner", "Progress", author_kind="agent", author_id="s")
    elif change == "comment_same_timestamp":
        async with env.db._engine.begin() as conn:
            await conn.execute(insert(task_comments).values(
                id="a-new-comment", task_id="owner", body="New progress",
                author_kind="agent", author_id="s", created_at=NOW,
            ))
    elif change == "task_set":
        await env.db.update_task("owner", description="Changed task")
    elif change == "supervisor_wake":
        async with env.db._engine.begin() as conn:
            wait = (await records(env))[0]
            await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
                decision_inputs={**wait["decision_inputs"], "supervisor_decision": {"decision": "wake"}},
            ))
    else:
        now[0] += 3601
    receipt = await waiting(env, idempotency_key="loop-second", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    receipt = await register(env, idempotency_key="loop-third", now=now[0])
    assert receipt["decision"]["mode"] == "in_place"
    assert not await records(env, wait_escalations)


async def test_reroute_releases_only_stopped_writer_with_readable_handoff(env, monkeypatch):
    receipt, service, _, _ = await escalated(env, hibernate=True)
    monkeypatch.setattr(service.orch._command_handler, "_validate_routing_class", lambda _: None)
    result = await decide(service, receipt, "reroute", to="engineering")
    assert result["success"], result
    task = await env.db.get_task("owner")
    assert task.status is TaskStatus.READY and task.class_hint == "engineering"
    assert task.route_source == "unrouted" and task.profile_id is None
    from src.handoffs import latest_note
    handoff = latest_note(await records(env, task_context))
    assert handoff[1]["agent"]["detail"] == "Continue the implementation"
    assert (await records(env, continuations))[0]["resume_state"] == "cancelled"
    assert not [row for row in await records(env, messages) if row["body_kind"] == "wait_decision"]


async def test_reroute_live_writer_is_refused_without_partial_hold(env):
    receipt, service, _, _ = await escalated(env)
    assert not (await decide(service, receipt, "reroute", to="engineering"))["success"]
    assert (await env.db.get_task("owner")).status is TaskStatus.WAITING
    assert (await env.db.get_session("s")).desired_state == "running"
    assert (await records(env))[0]["outcome"] == "pending"


async def test_parked_supervisor_wake_preserves_decision_for_resume_prime(env):
    receipt, service, _, _ = await escalated(env, hibernate=True)
    assert (await decide(service, receipt, "wake"))["success"]
    wait = (await records(env))[0]
    continuation = (await records(env, continuations))[0]
    envelope = wait_outcome_envelope(wait, continuation)
    assert envelope["outcome"] == "cancelled"
    assert envelope["decision"]["decision"] == "wake"
    assert envelope["decision"]["note"] == "Supervisor reason"
    assert wait["decision_inputs"]["registration_receipt"] == receipt


async def test_decision_rolls_back_hold_and_incident_when_graph_retraction_fails(env):
    receipt, service, _, _ = await escalated(env)
    service._dispatcher.recompute_priority = AsyncMock(side_effect=RuntimeError("retraction failed"))
    assert not (await decide(service, receipt, "cancel")).get("success")
    assert (await env.db.get_task("owner")).status is TaskStatus.WAITING
    assert (await env.db.get_session("s")).desired_state == "running"
    assert (await records(env))[0]["outcome"] == "pending"
    assert (await records(env, escalations))[0]["state"] == "needs_human"
    assert (await records(env, wait_escalations))[0]["decided_at"] is None


@pytest.mark.parametrize("changed_head", [False, True, None])
async def test_loop_detection_uses_branch_head_and_refuses_unknown_git_evidence(env, monkeypatch,
                                                                              changed_head):
    head = ["a" * 40]
    async def read_head(*_):
        return head[0]
    monkeypatch.setattr("src.git.manager.GitManager.arev_parse", read_head)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(branch_name="aq/owner"))
    now = [NOW + 1]
    service = service_for(env, now)
    receipt = await waiting(env, idempotency_key="branch-first", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    if changed_head is not False:
        head[0] = "b" * 40 if changed_head else None
    receipt = await waiting(env, idempotency_key="branch-second", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    receipt = await register(env, idempotency_key="branch-third", now=now[0])
    assert (receipt["decision"]["mode"] == "hibernate") is (changed_head is False)


async def test_same_value_task_edit_counts_but_lifecycle_events_do_not(env):
    now = [NOW + 1]
    service = service_for(env, now)
    receipt = await waiting(env, idempotency_key="edit-first", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    task = await env.db.get_task("owner")
    result = await service.orch._command_handler.execute("edit_task", {
        "task_id": "owner", "description": task.description,
    })
    assert result["updated"] == "owner", result
    receipt = await waiting(env, idempotency_key="edit-second", now=now[0])
    await complete_and_ack(env, service, receipt, now)
    receipt = await register(env, idempotency_key="edit-third", now=now[0])
    assert receipt["decision"]["mode"] == "in_place"
