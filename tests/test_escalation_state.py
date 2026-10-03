"""Supervisor delivery incidents stay internal and close on observed recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.config import DiscordConfig
from src.database import Database
from src.database.tables import messages
from src.escalations import SupervisorDeliveryWatchdog
from src.models import Project, SessionRecord
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def env():
    db = Database(lease_dsn("escalation_state"))
    await db.initialize()
    for project in ("p", "other"):
        await db.create_project(Project(id=project, name=project))
    bus = SimpleNamespace(emit=AsyncMock())
    config = SimpleNamespace(discord=DiscordConfig())
    yield SimpleNamespace(db=db, bus=bus, config=config)
    await db.close()


async def notice(env, *, body_kind="task_recovery", project="p", created_at=100, thread_id=None):
    row = await env.db.create_message(
        project_id=project,
        from_kind="system",
        from_id="test",
        to_kind="session",
        to_id=f"supervisor-{project}",
        body="Internal work notice",
        body_kind=body_kind,
        thread_id=thread_id,
    )
    async with env.db.immediate() as conn:
        await conn.execute(
            update(messages).where(messages.c.id == row.id).values(created_at=created_at)
        )
    return row


async def supervisor(env, *, started_at, desired_state="running"):
    await env.db.create_session(
        SessionRecord(
            id="supervisor-launch",
            project_id="p",
            profile_id="supervisor",
            name="n-supervisor--p",
            lifecycle="named",
            harness="fake",
            provider="fake",
            state="running",
            desired_state=desired_state,
            epoch="test",
            instance_token="test",
            work_dir="/never-used",
            started_at=started_at,
        )
    )


def watchdog(env):
    return SupervisorDeliveryWatchdog(env.db, env.bus, env.config)


async def test_delivery_notices_coalesce_by_project_and_body_kind_and_reach_inboxes(env):
    await notice(env)
    await notice(env)
    await notice(env, body_kind="agent_question")
    await notice(env, project="other")
    assert await watchdog(env).tick(1001) == 3
    assert await watchdog(env).tick(2000) == 0  # fresh watchdog after a daemon restart
    incidents = await env.db.list_escalations(source_kind="supervisor_delivery")
    assert len(incidents) == 3
    assert all(row["severity"] == "low" and row["task_id"] is None for row in incidents)
    for row in incidents:
        message = await env.db.get_message("msg-" + row["id"])
        assert message.to_kind == "session"
        assert message.to_id == row["supervisor_owner"]
        assert message.body_kind == "supervisor_delivery"
        assert row["id"] in message.body
        assert "does not approve" in message.body
    assert env.bus.emit.await_count == 3


async def test_parallel_ticks_create_one_incident_and_one_supervisor_notice(env):
    await notice(env)
    await notice(env)
    counts = await asyncio.gather(watchdog(env).tick(1001), watchdog(env).tick(1001))
    assert sum(counts) == 1
    incidents = await env.db.list_escalations(source_kind="supervisor_delivery")
    assert len(incidents) == 1
    pending = await env.db.get_pending_messages("session", "supervisor-p")
    assert sum(row.body_kind == "supervisor_delivery" for row in pending) == 1


async def test_transaction_coalesces_even_when_ticks_observe_different_oldest_notices(env):
    async def create(anchor):
        return await env.db.create_escalation(
            id=f"delivery-{anchor}",
            project_id="p",
            source_kind="supervisor_delivery",
            source_identity=f"task_recovery:{anchor}",
            incident_key=f"unavailable:{anchor}",
            supervisor_owner="supervisor-p",
            summary="Delivery unavailable",
            investigation="The delivery path is unavailable",
            decision_requested="Restore delivery",
            severity="high",
            supervisor_delivery_body_kind="task_recovery",
            now=1001,
        )

    first, second = await asyncio.gather(create("first"), create("second"))
    assert first[0]["id"] == second[0]["id"]
    assert first[1] != second[1]
    assert first[0]["severity"] == second[0]["severity"] == "low"
    pending = await env.db.get_pending_messages("session", "supervisor-p")
    assert len(pending) == 1 and pending[0].body_kind == "supervisor_delivery"


async def test_drained_notices_resolve_once_and_a_later_outage_is_a_new_incident(env):
    message = await notice(env)
    assert await watchdog(env).tick(1001) == 1
    original = (await env.db.list_escalations())[0]
    await env.db.mark_delivered(message.id)
    assert await watchdog(env).tick(1002) == 0
    resolved = await env.db.get_escalation(original["id"])
    assert resolved["state"] == "resolved"
    assert resolved["terminal_evidence"]["pending_notices"] == 0
    assert "delivered" in resolved["terminal_outcome"]
    assert env.bus.emit.await_args.args[0] == "escalation.updated.v1"
    emitted = env.bus.emit.await_count
    assert await watchdog(env).tick(1003) == 0
    assert env.bus.emit.await_count == emitted
    await notice(env, created_at=1004)
    assert await watchdog(env).tick(2000) == 1
    incidents = await env.db.list_escalations()
    assert len(incidents) == 2
    assert {row["state"] for row in incidents} == {"needs_human", "resolved"}


async def test_partial_drain_keeps_one_incident_including_younger_pending_notices(env):
    oldest = await notice(env)
    younger = await notice(env, created_at=900)
    assert await watchdog(env).tick(1001) == 1
    incident = (await env.db.list_escalations())[0]
    await env.db.mark_delivered(oldest.id)
    assert await watchdog(env).tick(1002) == 0
    assert (await env.db.get_escalation(incident["id"]))["state"] == "needs_human"
    await env.db.mark_delivered(younger.id)
    assert await watchdog(env).tick(1003) == 0
    assert (await env.db.get_escalation(incident["id"]))["state"] == "resolved"


async def test_new_supervisor_launch_resolves_without_waiting_for_queue_drain(env):
    await notice(env)
    assert await watchdog(env).tick(1001) == 1
    await supervisor(env, started_at=1002)
    assert await watchdog(env).tick(1003) == 0
    incident = (await env.db.list_escalations())[0]
    assert incident["state"] == "resolved"
    assert incident["terminal_evidence"]["supervisor_started_at"] == 1002
    assert incident["terminal_evidence"]["pending_notices"] == 1
    assert await watchdog(env).tick(3000) == 0
    assert len(await env.db.list_escalations()) == 1


@pytest.mark.parametrize("desired_state", ["running", "stopped"])
async def test_an_old_or_stopping_supervisor_launch_does_not_claim_recovery(env, desired_state):
    await notice(env)
    await supervisor(
        env, started_at=50 if desired_state == "running" else 1002, desired_state=desired_state
    )
    assert await watchdog(env).tick(1003) == 1
    assert (await env.db.list_escalations())[0]["state"] == "needs_human"


async def test_only_overdue_notices_raise_an_incident(env):
    await notice(env)
    assert await watchdog(env).tick(999) == 0
    assert await env.db.list_escalations() == []


async def test_legacy_high_severity_is_lowered_then_resolved_on_recovery(env):
    message = await notice(env)
    incident, _ = await env.db.create_escalation(
        id="legacy-delivery",
        project_id="p",
        source_kind="supervisor_delivery",
        source_identity="task_recovery:" + message.id.removeprefix("msg-"),
        incident_key="legacy",
        supervisor_owner="supervisor-p",
        summary="Delivery unavailable",
        investigation="Old high incident",
        decision_requested="Restore delivery",
        severity="high",
        now=1000,
    )
    assert await watchdog(env).tick(1001) == 0
    assert (await env.db.get_escalation(incident["id"]))["severity"] == "low"
    await env.db.mark_delivered(message.id)
    assert await watchdog(env).tick(1002) == 0
    assert (await env.db.get_escalation(incident["id"]))["state"] == "resolved"


async def test_later_escalation_reply_on_the_same_thread_can_raise_a_new_incident(env):
    first = await notice(env, body_kind="escalation_reply", thread_id="original-escalation")
    assert await watchdog(env).tick(1001) == 1
    await env.db.mark_delivered(first.id)
    assert await watchdog(env).tick(1002) == 0
    await notice(
        env, body_kind="escalation_reply", thread_id="original-escalation", created_at=1003
    )
    assert await watchdog(env).tick(2000) == 1
    assert len(await env.db.list_escalations()) == 2


async def test_closed_legacy_task_snapshot_is_not_reopened_or_reinterpreted(env):
    message = await notice(env)
    source = message.id.removeprefix("msg-")
    incident, _ = await env.db.create_escalation(
        id="legacy-closed",
        project_id="p",
        task_id="original-task",
        source_kind="supervisor_delivery",
        source_identity="task_recovery:" + source,
        incident_key="supervisor-unavailable:task_recovery:" + source,
        supervisor_owner="supervisor-p",
        summary="Delivery unavailable",
        investigation="Old task snapshot",
        decision_requested="Restore delivery",
        severity="high",
        now=900,
    )
    await env.db.resolve_escalation_on_recovery(
        incident["id"],
        expected_revision=0,
        source_kind="supervisor_delivery",
        terminal_outcome="Already recovered",
        terminal_evidence={},
        now=1000,
    )
    assert await watchdog(env).tick(1001) == 0
    current = await env.db.get_escalation(incident["id"])
    assert current["state"] == "resolved"
    assert current["task_id"] == "original-task"
    assert len(await env.db.list_escalations()) == 1
