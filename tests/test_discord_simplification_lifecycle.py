"""End-to-end acceptance for the simplified Discord lifecycle (spec §11).

These tests deliberately cross component boundaries.  The command handler and
durable PostgreSQL state are real; Discord, the clock, and the stopped-process
probe are fakes.  Nothing can reach a gateway, an LLM provider, or the
operator's database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import update

from src.commands.handler import CommandHandler
from src.config import (
    AppConfig,
    DatabaseConfig,
    DiscordConfig,
    DiscordDigestConfig,
    DiscordEscalationConfig,
)
from src.database import Database
from src.database.tables import task_session_attempts, tasks
from src.digest import DigestScheduleService, schedule_for
from src.discord.escalation_intake import DiscordEscalationIntake
from src.discord.slash_commands import RETIRED_SLASH_COMMANDS, unregister_retired_commands
from src.escalations import (
    EscalationDeliveryService,
    SinkTransport,
    TransportAmbiguous,
    TransportRetryable,
)
from src.event_bus import EventBus
from src.messaging.factory import create_messaging_adapter
from src.messaging.null_adapter import NullMessagingAdapter
from src.models import AgentProfile, Project, SessionRecord, Task, TaskCompletion, TaskStatus
from src.orchestrator import Orchestrator
from src.runtimes import default_registry
from tests.db_fixtures import lease_dsn

CHANNEL = "424242424242424242"
MOVED_CHANNEL = "434343434343434343"
HUMAN = "111111111111111111"
STRANGER = "999999999999999999"
BASE_URL = "https://queue.example.test"
BASE = 1_800_000.0
HOUR = 3600.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def discord_config(
    *,
    channel_id: str = CHANNEL,
    digest_enabled: bool = True,
    escalation_enabled: bool = True,
    interval_minutes: int = 60,
    catchup_hours: int = 24,
) -> DiscordConfig:
    return DiscordConfig(
        bot_token="fake-token",
        guild_id="1",
        channel_id=channel_id,
        authorized_users=[HUMAN],
        digest=DiscordDigestConfig(
            enabled=digest_enabled,
            interval_minutes=interval_minutes,
            catchup_hours=catchup_hours,
        ),
        escalation=DiscordEscalationConfig(
            enabled=escalation_enabled,
            mention_user_ids=[HUMAN],
        ),
    )


@pytest.fixture
async def env(tmp_path):
    dsn = lease_dsn("discord-simplification-lifecycle")
    db = Database(dsn)
    await db.initialize()
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_project(Project(id="other", name="Other Project"))
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=[
                "escalation_create",
                "escalation_get",
                "escalation_list",
                "escalation_update",
                "escalation_apply_reply",
                "task_recover",
            ],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    config = AppConfig(
        discord=discord_config(),
        database=DatabaseConfig(url=dsn),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    stopped_probe = AsyncMock(return_value=True)
    orch.session_providers = SimpleNamespace(
        create=lambda *_args, **_kwargs: SimpleNamespace(confirm_stopped=stopped_probe)
    )
    yield SimpleNamespace(
        db=db,
        config=config,
        orch=orch,
        handler=CommandHandler(orch, config),
        stopped_probe=stopped_probe,
    )
    await db.close()


def supervisor_scope(session_id: str, token: str) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "session_instance_token": token,
        "task_id": None,
        "project_id": "p",
        "elevated": True,
    }


async def add_supervisor(env, suffix: str, *, state: str = "running") -> tuple[str, str]:
    session_id = f"supervisor-{suffix}"
    token = f"supervisor-token-{suffix}"
    await env.db.create_session(
        SessionRecord(
            id=session_id,
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state=state,
            desired_state=state,
            epoch="test",
            instance_token=token,
            work_dir="/never-used",
            started_at=BASE,
        )
    )
    return session_id, token


async def add_blocked_attempt(env, suffix: str = "one") -> tuple[str, dict]:
    task_id = f"blocked-{suffix}"
    session_id = f"worker-{suffix}"
    await env.db.create_task(
        Task(
            id=task_id,
            project_id="p",
            title=f"Blocked work {suffix}",
            description="Preserve this work and its evidence.",
            status=TaskStatus.BLOCKED,
            profile_id="worker",
            intelligence_class="deep-high",
            branch_name=f"aq/{task_id}",
            created_at=BASE,
            updated_at=BASE,
        )
    )
    # ``create_task`` owns normal wall-clock timestamps.  Pin this synthetic
    # task back onto the fake-clock timeline so the stopped attempt is newer
    # than its task, matching a real recovery incident.
    await env.db.update_task(task_id, created_at=BASE, updated_at=BASE)
    await env.db.create_session(
        SessionRecord(
            id=session_id,
            task_id=task_id,
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name=session_id,
            lifecycle="task",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token=f"{session_id}-token",
            work_dir="/never-used",
            started_at=BASE + 100,
            last_activity=BASE + 200,
        )
    )
    await env.db.update_session(
        session_id,
        state="stopped",
        desired_state="stopped",
        ended_at=BASE + 300,
        end_reason="stuck_timeout",
    )
    await env.db.set_task_meta(task_id, "needs_attention", "stuck_timeout")
    await env.db.queue_task_recovery_notifications()
    incident = await env.db.get_task_meta(task_id, "supervisor_recovery_incident")
    assert incident is not None
    return task_id, incident


async def create_recovery_escalation(env, task_id: str, recovery: dict, scope: dict) -> dict:
    args = {
        "project_id": "p",
        "task_id": task_id,
        "source_kind": "task_recovery",
        "source_identity": recovery["id"],
        "incident_key": f"task-recovery:{recovery['id']}",
        "summary": "The worker attempt stopped while work remained blocked",
        "investigation": "The supervisor inspected the stopped attempt and recovery guards",
        "decision_requested": "Retry the task or keep it blocked?",
        "choices": ["retry", "hold"],
        "severity": "high",
        "_scope": scope,
    }
    created = await env.handler.execute("escalation_create", args)
    replay = await env.handler.execute("escalation_create", args)
    assert created["success"] is True and created["created"] is True
    assert replay["created"] is False
    assert replay["escalation"]["id"] == created["escalation"]["id"]
    return created["escalation"]


def gateway_message(
    thread_id: str,
    *,
    message_id: str = "discord-reply-1",
    author_id: str = HUMAN,
    content: str = "Retry after preserving the evidence.",
    parent_id: str | None = CHANNEL,
):
    return SimpleNamespace(
        id=message_id,
        channel=SimpleNamespace(id=thread_id, parent_id=parent_id),
        author=SimpleNamespace(id=author_id, bot=False),
        content=content,
    )


async def post_escalation(env, escalation_id: str, sink: SinkTransport, clock: Clock):
    service = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="daemon-before-restart",
        base_url=BASE_URL,
        clock=clock,
    )
    report = await service.tick()
    assert report.sent == 1
    root = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation_id)
        if row["kind"] == "root"
    )
    assert root["status"] == "sent"
    return service, root


async def test_blocked_attempt_reply_recovers_after_supervisor_and_daemon_restart(env):
    """The complete blocked -> human -> supervisor -> recovered loop is durable."""
    old_supervisor, old_token = await add_supervisor(env, "old")
    task_id, recovery = await add_blocked_attempt(env)
    escalation = await create_recovery_escalation(
        env,
        task_id,
        recovery,
        supervisor_scope(old_supervisor, old_token),
    )

    clock = Clock(BASE + 1000)
    sink = SinkTransport()
    _, root = await post_escalation(env, escalation["id"], sink, clock)
    thread_id = str(root["thread_id"])
    assert sink.calls.count("post_root") == 1
    assert sink.calls.count("ensure_thread") == 1

    # A new daemon has no in-memory binding, but the confirmed durable IDs
    # prevent duplicate root/thread creation after a gateway reconnect.
    restarted = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="daemon-after-restart",
        base_url=BASE_URL,
        clock=clock,
    )
    await restarted.tick()
    assert sink.calls.count("post_root") == 1
    assert sink.calls.count("ensure_thread") == 1

    # The owning supervisor disappears before the human replies.  The reply
    # is nevertheless committed and addressed to the logical owner exactly
    # once, including across duplicate gateway delivery.
    await env.db.update_session(old_supervisor, state="stopped", desired_state="stopped")
    intake = DiscordEscalationIntake(env.handler, env.config, reconcile=restarted.reconcile)
    message = gateway_message(thread_id)
    assert await intake.handle(message, bot_user_id=777)
    assert await intake.handle(message, bot_user_id=777)
    inbound = [
        row
        for row in await env.db.list_escalation_messages(escalation["id"])
        if row["direction"] == "inbound"
    ]
    assert len(inbound) == 1
    queued = await env.db.get_message(inbound[0]["supervisor_message_id"])
    assert queued.to_id == "supervisor-p" and queued.delivered_at is None

    # A replacement process takes over the same logical supervisor identity
    # and applies the verified reply through the guarded recovery service.
    new_supervisor, new_token = await add_supervisor(env, "replacement")
    current = await env.db.get_escalation(escalation["id"])
    result = await env.handler.execute(
        "escalation_apply_reply",
        {
            "escalation_id": escalation["id"],
            "reply_id": inbound[0]["id"],
            "expected_revision": current["revision"],
            "idempotency_key": "apply-discord-recovery",
            "action_kind": "task_recover",
            "target_id": task_id,
            "decision": "retry",
            "_scope": supervisor_scope(new_supervisor, new_token),
        },
    )
    assert result["success"] is True
    assert result["escalation"]["state"] == "resolved"
    assert (await env.db.get_task(task_id)).status is TaskStatus.READY
    env.stopped_probe.assert_awaited_once()

    clock.advance(120)
    await restarted.tick()
    deliveries = await env.db.list_escalation_deliveries(escalation["id"])
    assert len([row for row in deliveries if row["kind"] == "root"]) == 1
    assert len([row for row in deliveries if row["kind"] == "ack"]) == 1
    assert len([row for row in deliveries if row["kind"] == "resolution"]) == 1
    assert thread_id in sink.archived
    assert "Resolved" in sink.messages[str(root["root_message_id"])].content


async def test_failed_recovery_stays_open_and_reports_in_the_original_thread(env):
    supervisor_id, token = await add_supervisor(env, "failure")
    task_id, recovery = await add_blocked_attempt(env, "failure")
    scope = supervisor_scope(supervisor_id, token)
    escalation = await create_recovery_escalation(env, task_id, recovery, scope)
    clock = Clock(BASE + 1000)
    sink = SinkTransport()
    service, root = await post_escalation(env, escalation["id"], sink, clock)
    thread_id = str(root["thread_id"])

    intake = DiscordEscalationIntake(env.handler, env.config, reconcile=service.reconcile)
    assert await intake.handle(gateway_message(thread_id), bot_user_id=777)
    reply = next(
        row
        for row in await env.db.list_escalation_messages(escalation["id"])
        if row["direction"] == "inbound"
    )

    # The task changed after the human saw the incident.  Recovery must fail
    # closed rather than treating an old answer as permission for new state.
    await env.db.pause_task(task_id)
    current = await env.db.get_escalation(escalation["id"])
    result = await env.handler.execute(
        "escalation_apply_reply",
        {
            "escalation_id": escalation["id"],
            "reply_id": reply["id"],
            "expected_revision": current["revision"],
            "idempotency_key": "failed-discord-recovery",
            "action_kind": "task_recover",
            "target_id": task_id,
            "decision": "retry",
            "_scope": scope,
        },
    )
    assert result["error_code"] == "action_failed"
    assert result["escalation"]["state"] == "reply_received"
    assert (await env.db.get_task(task_id)).status is TaskStatus.PAUSED

    clock.advance(120)
    await service.tick()
    assert sink.calls.count("post_root") == 1
    assert thread_id not in sink.archived
    thread_messages = [row.content for row in sink.messages.values() if row.thread_id == thread_id]
    assert any("recovery action failed" in body.lower() for body in thread_messages)
    assert not any("resolved" in body.lower() for body in thread_messages)


async def test_partial_root_and_thread_creation_reconcile_without_blind_reposts(env):
    """Each confirmed half survives retry, ambiguity, and daemon replacement."""
    supervisor_id, token = await add_supervisor(env, "partial")
    task_id, recovery = await add_blocked_attempt(env, "partial")
    escalation = await create_recovery_escalation(
        env,
        task_id,
        recovery,
        supervisor_scope(supervisor_id, token),
    )
    clock = Clock(BASE + 1000)
    sink = SinkTransport()
    sink.faults.append(("ensure_thread", TransportRetryable("gateway unavailable")))

    first = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="partial-root-daemon",
        base_url=BASE_URL,
        clock=clock,
    )
    assert (await first.tick()).retried == 1
    root = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation["id"])
        if row["kind"] == "root"
    )
    assert root["status"] == "retry"
    assert root["root_message_id"] and root["thread_id"] is None

    # The replacement daemon creates only the missing thread.  Its opener
    # lands, but the fake gateway loses the response after accepting it.
    original_post_thread = sink.post_thread_message

    async def ambiguous_opener(*, thread_id: str, content: str):
        sink.calls.append("post_thread_message")
        sink.record(thread_id, content, thread_id=thread_id)
        raise TransportAmbiguous("response lost after the opener landed")

    sink.post_thread_message = ambiguous_opener  # type: ignore[method-assign]
    clock.advance(120)
    second = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="partial-thread-daemon",
        base_url=BASE_URL,
        clock=clock,
    )
    assert (await second.tick()).retried == 1
    partial = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation["id"])
        if row["kind"] == "root"
    )
    assert partial["status"] == "retry"
    assert partial["root_message_id"] == root["root_message_id"]
    assert partial["thread_id"]

    # A third process finds the opener's durable marker in the bound thread;
    # it never posts a second root, creates a second thread, or repeats text.
    sink.post_thread_message = original_post_thread  # type: ignore[method-assign]
    clock.advance(120)
    third = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="partial-reconcile-daemon",
        base_url=BASE_URL,
        clock=clock,
    )
    assert (await third.tick()).sent == 1
    final = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation["id"])
        if row["kind"] == "root"
    )
    assert final["status"] == "sent"
    assert sink.calls.count("post_root") == 1
    assert sink.calls.count("ensure_thread") == 2
    assert sink.calls.count("post_thread_message") == 1
    assert len(sink.threads) == 1
    assert len([message for message in sink.messages.values() if message.thread_id]) == 1


async def test_transport_outage_and_rate_guard_leave_scheduling_and_event_bus_live(env):
    """External failure is bounded and observable without blocking core work."""
    supervisor_id, token = await add_supervisor(env, "outage")
    task_id, recovery = await add_blocked_attempt(env, "outage")
    escalation = await create_recovery_escalation(
        env,
        task_id,
        recovery,
        supervisor_scope(supervisor_id, token),
    )
    await env.db.create_task(
        Task(
            id="scheduler-still-runs",
            project_id="p",
            title="Scheduler still runs",
            description="",
            profile_id="worker",
        )
    )

    delivery_events: list[dict] = []
    ready_events: list[dict] = []
    env.orch.bus.subscribe(
        "escalation.delivery_status.v1", lambda payload: delivery_events.append(payload)
    )
    env.orch.bus.subscribe("task.ready", lambda payload: ready_events.append(payload))
    env.orch.register_settlement_listener()

    clock = Clock(BASE + 1000)
    sink = SinkTransport()
    sink.faults.extend(
        [
            ("post_root", TransportRetryable("Discord gateway unavailable")),
            ("post_root", TransportRetryable("Discord gateway unavailable")),
        ]
    )
    outage = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="outage-daemon",
        base_url=BASE_URL,
        clock=clock,
        on_status=env.handler.emit_escalation_delivery_status,
        max_attempts=2,
    )

    assert (await outage.tick()).retried == 1
    retry = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation["id"])
        if row["kind"] == "root"
    )
    assert retry["status"] == "retry" and retry["attempt_count"] == 1
    assert retry["next_attempt_at"] == clock.now + 15
    assert "gateway unavailable" in retry["last_error"]

    # The same real database and EventBus can still promote independent work
    # while Discord is unavailable; the retry status itself is visible there.
    await env.orch._check_defined_tasks()
    assert (await env.db.get_task("scheduler-still-runs")).status is TaskStatus.READY
    assert [event["task_id"] for event in ready_events] == ["scheduler-still-runs"]
    assert [event["status"] for event in delivery_events] == ["retry"]

    clock.advance(15)
    assert (await outage.tick()).unknown == 1
    abandoned = next(
        row
        for row in await env.db.list_escalation_deliveries(escalation["id"])
        if row["kind"] == "root"
    )
    assert abandoned["status"] == "unknown" and abandoned["attempt_count"] == 2
    assert [event["status"] for event in delivery_events] == ["retry", "unknown"]

    # The invalid-request guard spends no transport call and schedules one
    # bounded backoff.  A tick before that deadline cannot busy-loop it.
    rate_task, rate_recovery = await add_blocked_attempt(env, "rate-limit")
    rate_escalation = await create_recovery_escalation(
        env,
        rate_task,
        rate_recovery,
        supervisor_scope(supervisor_id, token),
    )
    allowed = {"value": False}
    guarded = EscalationDeliveryService(
        env.db,
        sink,
        config=env.config,
        lease_owner="rate-guard-daemon",
        base_url=BASE_URL,
        clock=clock,
        rate_guard=lambda: allowed["value"],
        on_status=env.handler.emit_escalation_delivery_status,
    )
    calls_before_guard = len(sink.calls)
    assert (await guarded.tick()).retried == 1
    held = next(
        row
        for row in await env.db.list_escalation_deliveries(rate_escalation["id"])
        if row["kind"] == "root"
    )
    assert held["status"] == "retry" and held["next_attempt_at"] == clock.now + 15
    assert "rate guard" in held["last_error"]
    assert len(sink.calls) == calls_before_guard
    assert (await guarded.tick()).retried == 0
    assert (
        next(
            row
            for row in await env.db.list_escalation_deliveries(rate_escalation["id"])
            if row["kind"] == "root"
        )["attempt_count"]
        == 1
    )

    allowed["value"] = True
    clock.advance(15)
    assert (await guarded.tick()).sent == 1
    delivered = next(
        row
        for row in await env.db.list_escalation_deliveries(rate_escalation["id"])
        if row["kind"] == "root"
    )
    assert delivered["status"] == "sent" and delivered["attempt_count"] == 2


async def test_authorized_late_reply_cannot_reopen_a_resolved_archived_task(env):
    """A durable thread tombstone records history but creates no new work."""
    supervisor_id, token = await add_supervisor(env, "late-archive")
    task_id, recovery = await add_blocked_attempt(env, "late-archive")
    escalation = await create_recovery_escalation(
        env,
        task_id,
        recovery,
        supervisor_scope(supervisor_id, token),
    )
    clock = Clock(BASE + 1000)
    sink = SinkTransport()
    service, root = await post_escalation(env, escalation["id"], sink, clock)
    thread_id = str(root["thread_id"])

    # Resolve the incident from an earlier verified decision, then close and
    # archive its task while retaining the incident's transport tombstone.
    accepted = await env.db.accept_escalation_reply(
        escalation["id"],
        transport="dashboard",
        external_message_id="decision-before-archive",
        verified_actor="human:dashboard:operator",
        text="Resolve without retrying the worker.",
        received_at=clock.now,
    )
    await service.reconcile(escalation["id"])
    await service.pump()
    resolving = await env.db.transition_escalation(
        escalation["id"],
        expected_revision=accepted["escalation"]["revision"],
        new_state="resolving",
        now=clock.advance(1),
    )
    resolved = await env.db.transition_escalation(
        escalation["id"],
        expected_revision=resolving["revision"],
        new_state="resolved",
        terminal_outcome="Closed before the late Discord reply.",
        terminal_evidence={"kind": "test-resolution"},
        now=clock.advance(1),
    )
    await service.reconcile(escalation["id"])
    await service.pump()
    assert thread_id in sink.archived

    await env.db.transition_task(
        task_id,
        TaskStatus.COMPLETED,
        context="resolved_before_late_reply",
        force=True,
    )
    assert await env.db.archive_task(task_id)
    assert await env.db.get_task(task_id) is None
    assert (await env.db.get_archived_task(task_id))["status"] == "COMPLETED"
    pending_before = await env.db.get_pending_messages("session", "supervisor-p")

    intake = DiscordEscalationIntake(env.handler, env.config, reconcile=service.reconcile)
    assert await intake.handle(
        gateway_message(
            thread_id,
            message_id="discord-late-after-archive",
            content="Retry this closed task anyway.",
        ),
        bot_user_id=777,
    )
    await service.pump()

    after = await env.db.get_escalation(escalation["id"])
    assert after["state"] == "resolved" and after["revision"] == resolved["revision"]
    assert await env.db.get_task(task_id) is None
    assert (await env.db.get_archived_task(task_id))["status"] == "COMPLETED"
    assert await env.db.get_pending_messages("session", "supervisor-p") == pending_before
    late = next(
        message
        for message in await env.db.list_escalation_messages(escalation["id"])
        if message["external_message_id"] == "discord-late-after-archive"
    )
    assert late["supervisor_message_id"] is None
    env.stopped_probe.assert_not_awaited()
    guidance = [
        message
        for message in sink.messages.values()
        if "has not reopened the work" in message.content
    ]
    assert len(guidance) == 1 and guidance[0].thread_id == thread_id
    assert thread_id in sink.archived


async def add_digest_task(db: Database, task_id: str, *, status: str = "READY") -> None:
    await db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status=status))


async def add_live_attempt(db: Database, task_id: str, *, started_at: float) -> tuple[str, str]:
    session_id = f"session-{task_id}"
    await db.create_session(
        SessionRecord(
            id=session_id,
            project_id="p",
            task_id=task_id,
            profile_id="worker",
            harness="fake",
            provider="fake",
            name=session_id,
            lifecycle="pool",
            state="running",
            desired_state="running",
            work_dir="/never-used",
            epoch="test",
            instance_token=f"{session_id}-token",
            started_at=started_at,
            last_activity=started_at,
        )
    )
    attempts = await db.list_task_session_attempts(task_id)
    assert len(attempts) == 1
    return session_id, str(attempts[0]["id"])


async def test_digest_is_silent_then_tracks_live_and_late_work_across_restart(env):
    await add_digest_task(env.db, "queued-only")
    clock = Clock(BASE + HOUR + 30)
    sink = SinkTransport()
    service = DigestScheduleService(
        env.db,
        sink,
        config=env.config,
        lease_owner="digest-before-restart",
        base_url=BASE_URL,
        clock=clock,
    )
    schedule = schedule_for(env.config.discord)
    service._anchors[(schedule.destination, schedule.generation)] = BASE

    idle = await service.tick()
    assert idle.suppressed == 1 and idle.sent == 0
    assert sink.messages == {}

    await add_digest_task(env.db, "long-running", status="IN_PROGRESS")
    session_id, attempt_id = await add_live_attempt(
        env.db, "long-running", started_at=BASE + HOUR + 100
    )
    await env.db.update_session(session_id, last_activity=BASE + 2 * HOUR)
    clock.advance(HOUR)
    started = await service.tick()
    assert started.sent == 1
    assert "long-running" in next(iter(sink.messages.values())).content

    # Reconstructing the worker inside the same window neither re-evaluates
    # nor re-sends it.
    restarted = DigestScheduleService(
        env.db,
        sink,
        config=env.config,
        lease_owner="digest-after-restart",
        base_url=BASE_URL,
        clock=clock,
    )
    clock.advance(60)
    assert (await restarted.tick()).evaluated == 0
    assert len(sink.messages) == 1

    # In the next hour the same healthy attempt gets only an active-count
    # update; its start highlight is not replayed as progress.
    await env.db.update_session(session_id, last_activity=BASE + 3 * HOUR)
    clock.advance(HOUR)
    active = await restarted.tick()
    assert active.sent == 1
    assert len(sink.messages) == 2
    latest = list(sink.messages.values())[-1].content
    assert "1 active" in latest
    assert "started" not in latest.lower()

    # A completion row written after its natural window was evaluated is
    # recovered by the next window's lookback exactly once.
    late_at = BASE + 3 * HOUR - 300
    await env.db.update_session(
        session_id,
        state="stopped",
        desired_state="stopped",
        ended_at=late_at,
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(task_session_attempts)
            .where(task_session_attempts.c.id == attempt_id)
            .values(ended_at=late_at, state="stopped")
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "long-running").values(status="COMPLETED")
        )
    await env.db.save_task_completion(
        TaskCompletion(
            id="completion-late",
            task_id="long-running",
            outcome="pass",
            summary="finished after the window collector had already run",
            completed_at=late_at,
        )
    )
    clock.advance(HOUR)
    late = await restarted.tick()
    assert late.sent == 1
    assert "finished after the window" in list(sink.messages.values())[-1].content
    clock.advance(60)
    assert (await restarted.tick()).evaluated == 0
    assert len(sink.messages) == 3

    windows = await env.db.list_digest_windows(destination=f"discord:{CHANNEL}")
    assert [row["send_status"] for row in reversed(windows)] == [
        "suppressed",
        "sent",
        "sent",
        "sent",
    ]


async def test_digest_config_change_and_long_outage_produce_one_bounded_catchup(env):
    clock = Clock(BASE + HOUR + 30)
    sink = SinkTransport()
    service = DigestScheduleService(
        env.db,
        sink,
        config=env.config,
        lease_owner="digest-config",
        base_url=BASE_URL,
        clock=clock,
    )
    initial = schedule_for(env.config.discord)
    service._anchors[(initial.destination, initial.generation)] = BASE
    assert (await service.tick()).suppressed == 1

    moved = discord_config(channel_id=MOVED_CHANNEL, interval_minutes=30, catchup_hours=24)
    env.config.discord = moved
    # A new destination/generation anchors at the change boundary and does
    # not replay the old channel's suppressed or active history.
    clock.advance(30 * 60)
    assert (await service.tick()).evaluated == 0
    assert sink.messages == {}

    await add_digest_task(env.db, "after-config", status="COMPLETED")
    await env.db.save_task_completion(
        TaskCompletion(
            id="completion-after-config",
            task_id="after-config",
            outcome="pass",
            summary="completed after the destination change",
            completed_at=clock.now + 60,
        )
    )
    clock.advance(30 * 60)
    assert (await service.tick()).sent == 1
    assert {message.where for message in sink.messages.values()} == {MOVED_CHANNEL}

    # Ninety missed hours coalesce into one recent 24-hour window and one
    # message.  The old activity is not dumped again.
    previous_count = len(sink.messages)
    clock.advance(90 * HOUR)
    await add_digest_task(env.db, "during-outage", status="COMPLETED")
    await env.db.save_task_completion(
        TaskCompletion(
            id="completion-during-outage",
            task_id="during-outage",
            outcome="pass",
            summary="completed during the bounded catch-up horizon",
            completed_at=clock.now - HOUR,
        )
    )
    catchup = await service.tick()
    assert catchup.catchup == 1 and catchup.sent == 1
    assert len(sink.messages) == previous_count + 1
    assert "catch-up" in list(sink.messages.values())[-1].content
    rows = await env.db.list_digest_windows(destination=f"discord:{MOVED_CHANNEL}")
    catchup_rows = [row for row in rows if row["is_catchup"]]
    assert len(catchup_rows) == 1
    assert catchup_rows[0]["window_end"] - catchup_rows[0]["window_start"] == 24 * HOUR


async def test_discord_disabled_keeps_scheduler_and_dashboard_escalation_commands(tmp_path):
    dsn = lease_dsn("discord-disabled-lifecycle")
    config = AppConfig(
        messaging_platform="none",
        discord=discord_config(digest_enabled=False, escalation_enabled=False),
        database=DatabaseConfig(url=dsn),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config, runtimes=None)
    orch._runtimes = default_registry(config=config)
    await orch.initialize()
    try:
        await orch.db.create_project(Project(id="p", name="Project P"))
        handler = CommandHandler(orch, config)
        orch.set_command_handler(handler)
        adapter = create_messaging_adapter(config, orch)
        assert isinstance(adapter, NullMessagingAdapter)
        await orch.run_one_cycle()

        created = await handler.execute(
            "escalation_create",
            {
                "project_id": "p",
                "source_kind": "core",
                "source_identity": "disabled-discord",
                "incident_key": "disabled-discord",
                "summary": "External messaging is unavailable",
                "investigation": "The dashboard and core are still reachable",
                "decision_requested": "Continue in the dashboard?",
                "severity": "medium",
            },
        )
        escalation_id = created["escalation"]["id"]
        listed = await handler.execute("escalation_list", {"project_id": "p"})
        assert [row["id"] for row in listed["escalations"]] == [escalation_id]
        replied = await handler.execute(
            "escalation_reply",
            {
                "escalation_id": escalation_id,
                "text": "Continue through the dashboard.",
                "external_message_id": "dashboard-disabled-discord",
            },
        )
        assert replied["supervisor_enqueued"] is True

        sink = SinkTransport()
        delivery = EscalationDeliveryService(
            orch.db,
            sink,
            config=config,
            lease_owner="disabled-discord",
            clock=Clock(BASE),
        )
        digest = DigestScheduleService(
            orch.db,
            sink,
            config=config,
            lease_owner="disabled-discord",
            clock=Clock(BASE),
        )
        assert (await delivery.tick()).skipped == "discord.escalation.enabled is false"
        assert (await digest.tick()).skipped == "discord.digest.enabled is false"
        assert sink.calls == []
    finally:
        await orch.shutdown()


async def test_retired_discord_inputs_cannot_mutate_tasks_gates_or_workers(env):
    await env.db.create_task(
        Task(id="protected", project_id="p", title="Protected", description="original")
    )
    gate_id, _ = await env.db.create_gate(
        "p", "human", "Approval", question="Proceed?", await_id="protected-gate"
    )
    intake = DiscordEscalationIntake(env.handler, env.config)

    # Channel chatter, arbitrary mentions, and an old/unknown task thread all
    # remain routing-inert even when sent by an authorized user.
    assert not await intake.handle(
        gateway_message(CHANNEL, parent_id=None, content="@everyone reopen protected"),
        bot_user_id=777,
    )
    assert not await intake.handle(
        gateway_message("515151515151515151", content="approve and retry"),
        bot_user_id=777,
    )
    assert not await intake.handle(
        gateway_message("515151515151515151", author_id=STRANGER),
        bot_user_id=777,
    )
    task = await env.db.get_task("protected")
    assert task.status is TaskStatus.DEFINED and task.description == "original"
    assert (await env.db.get_gate(gate_id))["status"] == "open"
    assert await env.db.get_pending_messages("session", "supervisor-p") == []

    class FakeTree:
        def __init__(self) -> None:
            self.commands = {name: object() for name in (*RETIRED_SLASH_COMMANDS, "plugin")}

        def remove_command(self, name: str):
            return self.commands.pop(name, None)

    tree = FakeTree()
    assert set(unregister_retired_commands(tree)) == RETIRED_SLASH_COMMANDS
    assert set(tree.commands) == {"plugin"}

    # Persistent callback modules were removed at cutover; their historical
    # messages are made inert by view removal instead of retaining mutation
    # code in a hidden compatibility path.
    assert not Path("src/discord/agent_questions.py").exists()
    assert not Path("src/discord/gate_view.py").exists()
