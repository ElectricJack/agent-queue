"""Escalations are stateful: one post, edited in place, closed on evidence.

Two halves of the Discord design spec §5, both behind
``discord.escalations.stateful``:

* the **§5.2 state machine** — one channel post per incident, rewritten in
  place as the incident moves, collapsed to a one-line form when it closes;
* the **§5.5 auto-resolution rules** — an incident whose gate resolved, whose
  task finished, or that nobody has touched in a week closes itself, which is
  what stops the channel being an append-only log of every incident the daemon
  ever had.

The ``supervisor_delivery`` rules predate this phase (P0) and have no flag, so
they are tested here too: they are the other half of §5.5's table and they share
this file's database.

Nothing here touches a gateway: the delivery side runs against
:class:`~src.escalations.transport.SinkTransport` and the real durable outbox.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.config import DiscordConfig, DiscordEscalationConfig, DiscordEscalationsConfig
from src.database import Database
from src.database.queries.escalation_queries import ESCALATION_OUTCOMES
from src.database.tables import messages
from src.escalations import (
    AUTO_OBSOLETE_TASK_STATUSES,
    STALE_OBSOLETE_SECONDS,
    EscalationAutoResolver,
    EscalationDeliveryService,
    EscalationFacts,
    SinkTransport,
    SupervisorDeliveryWatchdog,
    display_state,
    is_collapsed,
    is_stale_due,
    plan_deliveries,
    state_dedup_key,
    thread_archived,
)
from src.escalations.dispatch import EDIT_COALESCE_SECONDS
from src.escalations.plan import DEFER_SECONDS, MAX_ATTEMPTS
from src.escalations.render import render_collapsed_root, render_state_root
from src.models import Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn

BASE_URL = "https://queue.example.test"
CHANNEL = "424242424242424242"


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


# ======================================================================
# §5.2 — the state machine, as a table of stored state -> display form
# ======================================================================


@pytest.mark.parametrize(
    ("stored", "stale", "expected"),
    [
        ("needs_human", False, "open"),
        ("needs_human", True, "stale"),
        # A verified human reply is §5.2's ``answered``; the supervisor picking
        # the incident up is the same sentence, because §5.2 has no third one.
        ("reply_received", False, "answered"),
        ("reply_received", True, "answered"),
        ("resolving", False, "answered"),
        ("resolved", False, "resolved"),
        ("cancelled", False, "obsolete"),
        ("stale", False, "obsolete"),
        # §5.2's stale form is a reminder for an incident that still wants a
        # human, so it can never make a closed one look open again.
        ("resolved", True, "resolved"),
        ("cancelled", True, "obsolete"),
    ],
)
def test_every_stored_state_has_exactly_one_display_form(stored, stale, expected):
    assert display_state(stored, stale=stale) == expected


def test_only_a_resolved_or_obsolete_post_collapses_and_archives():
    assert [state for state in ("open", "stale", "answered") if is_collapsed(state)] == []
    assert is_collapsed("resolved") and is_collapsed("obsolete")
    # §5.2's thread column: open until the post collapses, archived after.
    assert thread_archived("open") is False
    assert thread_archived("answered") is False
    assert thread_archived("stale") is False
    assert thread_archived("resolved") is True
    assert thread_archived("obsolete") is True


def test_a_state_edit_key_is_one_edit_per_distinct_form_not_per_revision():
    first = state_dedup_key("esc-1", 0, "answered")
    assert first == state_dedup_key("esc-1", 0, "answered")
    # Two revisions that render the same sentence are one edit...
    assert state_dedup_key("esc-1", 0, "answered") != state_dedup_key("esc-1", 0, "stale")
    # ...and a new root generation is a new post, so it gets a new key.
    assert state_dedup_key("esc-1", 0, "answered") != state_dedup_key("esc-1", 1, "answered")


def test_an_unknown_stored_state_is_an_error_rather_than_a_silent_fallback():
    with pytest.raises(ValueError, match="no display state"):
        display_state("not-a-state")
    with pytest.raises(ValueError, match="unknown display state"):
        is_collapsed("not-a-state")


def test_the_stale_timer_only_fires_for_an_untouched_incident_that_still_needs_a_human():
    now = 1_000_000.0
    assert is_stale_due("needs_human", now - 3600, now=now, reminder_minutes=30) is True
    assert is_stale_due("needs_human", now - 600, now=now, reminder_minutes=30) is False
    # 0 disables the reminder entirely, so a box that never asked never sees it.
    assert is_stale_due("needs_human", now - 10**9, now=now, reminder_minutes=0) is False
    # An answered incident is being acted on, not idling.
    assert is_stale_due("reply_received", now - 10**9, now=now, reminder_minutes=1) is False
    assert is_stale_due("resolved", now - 10**9, now=now, reminder_minutes=1) is False
    assert is_stale_due("needs_human", None, now=now, reminder_minutes=1) is False


# ======================================================================
# §5.1/§5.2 — edit-in-place, through the real outbox against the sink
# ======================================================================


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
async def world():
    db = Database(lease_dsn("escalation_stateful"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    transport = SinkTransport()
    clock = Clock()
    config = SimpleNamespace(
        discord=DiscordConfig(
            channel_id=CHANNEL,
            escalation=DiscordEscalationConfig(enabled=True),
            escalations=DiscordEscalationsConfig(stateful=True),
        )
    )

    def service(**overrides):
        return EscalationDeliveryService(
            db,
            transport,
            config=config,
            lease_owner="daemon-a",
            base_url=BASE_URL,
            clock=clock,
            **overrides,
        )

    yield SimpleNamespace(db=db, transport=transport, clock=clock, config=config, service=service)
    await db.close()


async def incident(world, **overrides):
    values = {
        "id": "esc-1",
        "project_id": "p",
        "task_id": None,
        "source_kind": "question",
        "source_identity": "q-1",
        "incident_key": "q:q-1",
        "supervisor_owner": "supervisor-p",
        "summary": "Nobody can tell which branch to cut from",
        "investigation": "Two release branches both claim the fix",
        "decision_requested": "Cut from main or from the release branch?",
        "severity": "medium",
        "now": 10.0,
    }
    values.update(overrides)
    row, _ = await world.db.create_escalation(**values)
    return row


async def post_once(world, row):
    """Reconcile and pump until nothing is due, then return the transport calls."""
    await world.service().reconcile(row)
    for _ in range(6):
        report = await world.service().pump()
        if not report.sent and not report.retried and not report.unknown:
            break
    return world.transport.calls


async def reply(world, row, *, text="cut from main", external="msg-1", at=None):
    return await world.db.accept_escalation_reply(
        row["id"],
        transport="discord",
        external_message_id=external,
        verified_actor="human:discord:1",
        text=text,
        received_at=at if at is not None else world.clock(),
    )


async def test_one_incident_never_gets_a_second_root_post(world):
    row = await incident(world)
    await post_once(world, row)
    # root, thread opener, and nothing else: §5.1's one post, one thread.
    assert world.transport.calls.count("post_root") == 1
    assert world.transport.edits == []


async def test_a_reply_rewrites_the_one_post_in_place_and_appends_nothing(world):
    row = await incident(world)
    await post_once(world, row)
    root = next(iter(world.transport.messages))
    assert world.transport.calls.count("post_root") == 1

    await reply(world, row)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    report = await world.service().pump()

    assert report.edits == 1
    assert world.transport.calls.count("post_root") == 1, "§5.1: nothing appends a second root"
    assert len(world.transport.edits) == 1
    edited = world.transport.messages[root].content
    assert "Answered" in edited and "acting on it" in edited
    # §5.2's answered row keeps the one link and nothing else.
    assert edited.endswith(f"<{BASE_URL}/focus/escalations/esc-1>")
    # The ack still goes to the thread; the channel post is not the conversation.
    assert "post_thread_message" in world.transport.calls


async def test_replaying_the_same_state_change_edits_the_post_exactly_once(world):
    row = await incident(world)
    await post_once(world, row)
    await reply(world, row)
    current = await world.db.get_escalation(row["id"])

    # Drain what the reply owes (the thread ack and the one state edit), then
    # replay: the planner re-derives the same dedup keys every time and the
    # unique key absorbs them, so nothing new is enqueued.
    await world.service().reconcile(current)
    for _ in range(4):
        await world.service().pump()
    for _ in range(3):
        assert await world.service().reconcile(current) == []
        await world.service().pump()

    assert len(world.transport.edits) == 1
    kinds = [r["kind"] for r in await world.db.list_escalation_deliveries(row["id"])]
    assert kinds.count("state") == 1


async def test_a_closed_incident_collapses_its_post_once_and_stamps_the_clock(world):
    row = await incident(world)
    await post_once(world, row)
    root = next(iter(world.transport.messages))

    await world.db.resolve_escalation_on_recovery(
        row["id"],
        expected_revision=0,
        source_kind=row["source_kind"],
        terminal_outcome="Cut from main; the release branch is redundant.",
        terminal_evidence={"decision": "cut from main"},
        outcome="human",
        now=world.clock(),
    )
    for _ in range(3):
        await world.service().reconcile(await world.db.get_escalation(row["id"]))
        await world.service().pump()

    collapsed = world.transport.messages[root].content
    assert collapsed.startswith("✅ Resolved: Cut from main")
    # One line, no link, no mention: §3.2's collapsed row.
    assert len(collapsed.splitlines()) == 2 and collapsed.splitlines()[1].startswith("-# ")
    assert "/focus/" not in collapsed
    stamped = await world.db.get_escalation(row["id"])
    assert stamped["collapsed_at"] == world.clock()
    # The thread is archived exactly once and the closed post is one line.
    assert len(world.transport.archived) == 1
    assert len([call for call in world.transport.calls if call == "edit_root"]) == 1


async def test_replaying_a_resolution_never_edits_a_collapsed_post_again(world):
    row = await incident(world)
    await post_once(world, row)
    await world.db.resolve_escalation_on_recovery(
        row["id"],
        expected_revision=0,
        source_kind=row["source_kind"],
        terminal_outcome="Rolled it forward",
        terminal_evidence={},
        outcome="human",
        now=world.clock(),
    )
    for _ in range(3):
        await world.service().reconcile(await world.db.get_escalation(row["id"]))
        await world.service().pump()
    edits_before = len(world.transport.edits)

    # The collapsed stamp is what a replay reads: the post is already the
    # one-line form, so the delivery is recorded sent and nothing is written.
    assert await world.db.record_escalation_collapse(row["id"], now=world.clock()) is not None
    again = await world.db.record_escalation_collapse(row["id"], now=world.clock() + 99)
    assert again["collapsed_at"] == world.clock(), "write-once: a retry cannot move the clock"
    assert len(world.transport.edits) == edits_before


async def test_a_rule_closed_incident_reads_as_no_longer_needed(world):
    row = await incident(world)
    await world.db.transition_escalation(
        row["id"],
        expected_revision=0,
        new_state="cancelled",
        terminal_outcome="The task reached COMPLETED; no decision is needed.",
        outcome="task_terminal",
        now=world.clock(),
    )
    text = render_collapsed_root(
        EscalationFacts.from_row(await world.db.get_escalation(row["id"])),
        display="obsolete",
        dedup_key=f"{row['id']}:resolution:0:root",
    )
    assert text.startswith("⚪ No longer needed: The task reached COMPLETED")
    # A question somebody answered is not "no longer needed".
    human = EscalationFacts.from_row(
        {**row, "state": "cancelled", "terminal_outcome": "Operator kept it blocked", "outcome": "human"}
    )
    assert render_collapsed_root(human, display="obsolete", dedup_key="k").startswith(
        "✅ Cancelled: Operator kept it blocked"
    )


async def test_the_stale_form_is_one_edit_that_keeps_the_threads_open(world):
    row = await incident(world, now=10.0)
    await post_once(world, row)
    world.clock.advance(3600)
    # A 30-minute reminder on a box that configured one: §5.2's stale timer.
    world.config.discord.escalation.reminder_minutes = 30
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    world.clock.advance(EDIT_COALESCE_SECONDS + 1)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    await world.service().pump()

    root = next(iter(world.transport.messages))
    assert "Still open since" in world.transport.messages[root].content
    assert world.transport.archived == set(), "a stale incident still wants a human"


# ======================================================================
# §7.2 — edits coalesce: at most one per escalation per 30 s
# ======================================================================


async def test_a_burst_of_state_changes_is_coalesced_then_sent_in_order(world):
    """§7.2: at most one edit per escalation per 30 s, and never dropped.

    The reachable burst is a long-idle incident that hits its reminder and is
    answered in the same breath: two distinct forms, two edits, one message.
    The first goes out, the second is deferred to the window rather than spent
    on a post that is already correct, and it goes out afterwards.
    """
    row = await incident(world, now=world.clock())
    world.config.discord.escalation.reminder_minutes = 30
    await post_once(world, row)
    root = next(iter(world.transport.messages))

    world.clock.advance(3600)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    assert (await world.service().pump()).edits == 1
    assert "Still open since" in world.transport.messages[root].content

    world.clock.advance(1)
    await reply(world, row)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    second = await world.service().pump()
    assert second.coalesced == 1, "the second form waits for the window"
    assert second.edits == 0
    assert len(world.transport.edits) == 1, "queued, never dropped — and never sent early"
    states = [
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    ]
    assert sorted(r["status"] for r in states) == ["retry", "sent"]

    world.clock.advance(EDIT_COALESCE_SECONDS + 1)
    for _ in range(3):
        await world.service().pump()
    assert len(world.transport.edits) == 2
    assert world.transport.calls.count("post_root") == 1, "one post, always"
    assert "Answered" in world.transport.messages[root].content
    settled = [
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    ]
    assert sorted(r["status"] for r in settled) == ["sent", "sent"]


async def test_a_coalesced_edit_is_deferred_to_the_window_edge_and_still_claimable(world):
    row = await incident(world, now=world.clock() - 3600)
    world.config.discord.escalation.reminder_minutes = 30
    await post_once(world, row)

    # One edit goes out...
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    assert (await world.service().pump()).edits == 1

    # ...and a second form becomes due inside the window.
    world.clock.advance(1)
    await reply(world, row)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    report = await world.service().pump()

    assert report.coalesced == 1
    deferred = [
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    ][-1]
    assert deferred["status"] == "retry"
    assert "§7.2" in deferred["last_error"]
    assert deferred["next_attempt_at"] == pytest.approx(
        world.clock() + EDIT_COALESCE_SECONDS, abs=1.0
    )

    # A deferral is a deferral, not an abandonment: the row is still owed and
    # still claimable once the window opens.
    world.clock.advance(EDIT_COALESCE_SECONDS + 1)
    assert (await world.service().pump()).edits == 1
    settled = [
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    ]
    assert sorted(r["status"] for r in settled) == ["sent", "sent"]


async def test_latest_state_wins_when_an_overtaken_edit_is_still_queued(world):
    row = await incident(world, now=world.clock() - 3600)
    world.config.discord.escalation.reminder_minutes = 30
    await post_once(world, row)
    root = next(iter(world.transport.messages))
    stale_row, _ = await world.db.enqueue_escalation_delivery(
        row["id"],
        dedup_key=state_dedup_key(row["id"], 0, "stale"),
        kind="state",
        payload={"display": "stale", "revision": 0},
        available_at=world.clock(),
        priority=5,
    )
    # Meanwhile the incident moved on: the stale form is no longer the truth.
    await reply(world, row)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    world.clock.advance(EDIT_COALESCE_SECONDS + 1)
    for _ in range(4):
        await world.service().pump()

    # The overtaken row wrote the *current* form rather than its own, so the
    # post reads correctly and the row that followed it has nothing to add.
    assert "Answered" in world.transport.messages[root].content
    assert len(world.transport.edits) == 1, "latest state wins, and only once"
    overtaken = await world.db.get_escalation_delivery(stale_row["id"])
    assert overtaken["status"] == "sent"
    assert overtaken["external_receipt_id"].endswith("|answered")


async def test_a_state_edit_waits_for_the_post_it_is_going_to_rewrite(world):
    row = await incident(world)
    await world.db.enqueue_escalation_delivery(
        row["id"],
        dedup_key=state_dedup_key(row["id"], 0, "answered"),
        kind="state",
        payload={"display": "answered", "revision": 1},
        available_at=world.clock(),
        priority=5,
    )
    report = await world.service().pump()
    assert report.retried == 1 and report.edits == 0
    deferred = next(
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    )
    assert deferred["status"] == "retry"
    assert "waiting for the incident's root post" in deferred["last_error"]
    # An edit never creates the post it would have rewritten.
    assert world.transport.calls == []


async def test_a_state_edit_whose_post_never_arrives_is_abandoned_not_forever(world):
    row = await incident(world)
    await world.db.enqueue_escalation_delivery(
        row["id"],
        dedup_key=state_dedup_key(row["id"], 0, "answered"),
        kind="state",
        payload={"display": "answered", "revision": 1},
        available_at=world.clock(),
        priority=5,
    )
    for _ in range(MAX_ATTEMPTS + 2):
        world.clock.advance(DEFER_SECONDS + 1)
        await world.service().pump()
    deferred = next(
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    )
    assert deferred["status"] == "unknown"
    assert "abandoned rather than deferred forever" in deferred["last_error"]


async def test_an_incident_that_closes_first_lets_the_resolution_own_the_post(world):
    """The ordinary race: a reply and a resolution land before the edit is pumped."""
    row = await incident(world)
    await post_once(world, row)
    await reply(world, row)
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    await world.db.resolve_escalation_on_recovery(
        row["id"],
        expected_revision=1,
        source_kind=row["source_kind"],
        terminal_outcome="Cut from main",
        terminal_evidence={},
        outcome="human",
        now=world.clock(),
    )
    for _ in range(4):
        await world.service().reconcile(await world.db.get_escalation(row["id"]))
        await world.service().pump()

    root = next(iter(world.transport.messages))
    # The post reads as the closed incident, and the abandoned live edit is not
    # reported as something needing attention.
    assert world.transport.messages[root].content.startswith("✅ Resolved: Cut from main")
    state_rows = [
        r for r in await world.db.list_escalation_deliveries(row["id"]) if r["kind"] == "state"
    ]
    assert [r["status"] for r in state_rows] == ["sent"]
    assert "owns the collapsed post" in state_rows[0]["last_error"]


# ======================================================================
# §5.5 — auto-resolution rules
# ======================================================================


def resolver(world, *, stateful=True):
    config = SimpleNamespace(
        discord=DiscordConfig(
            channel_id=CHANNEL,
            escalations=DiscordEscalationsConfig(stateful=stateful),
        )
    )
    return EscalationAutoResolver(world.db, config, clock=world.clock)


async def open_gate(world):
    gate_id, _ = await world.db.create_gate("p", "human", "Choose a branch")
    return gate_id


async def test_a_resolved_gate_resolves_its_incident_with_the_gate_decision(world):
    gate_id = await open_gate(world)
    row = await incident(
        world,
        id="esc-gate",
        source_kind="gate",
        source_identity=gate_id,
        incident_key=f"gate:{gate_id}",
    )
    await world.db.resolve_gate(gate_id, resolved_by="operator", resolution="cut from main")
    assert (await resolver(world).tick()).closed == 1

    closed = await world.db.get_escalation(row["id"])
    assert closed["state"] == "resolved"
    assert closed["outcome"] == "gate_resolved"
    assert gate_id in closed["terminal_outcome"]
    assert closed["terminal_evidence"]["gate_id"] == gate_id


async def test_an_open_gate_leaves_its_incident_alone(world):
    gate_id = await open_gate(world)
    row = await incident(
        world,
        id="esc-gate",
        source_kind="gate",
        source_identity=gate_id,
        incident_key=f"gate:{gate_id}",
    )
    assert (await resolver(world).tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["state"] == "needs_human"


async def test_a_completed_task_obsoletes_the_question_asked_about_it(world):
    await world.db.create_task(
        Task(id="t-1", project_id="p", title="Ship", description="d", status=TaskStatus.COMPLETED)
    )
    row = await incident(world, id="esc-task", task_id="t-1")
    assert (await resolver(world).tick()).closed == 1

    closed = await world.db.get_escalation(row["id"])
    assert closed["state"] == "cancelled", "§5.2's stored spelling of obsolete"
    assert closed["outcome"] == "task_terminal"
    assert closed["terminal_evidence"] == {"task_id": "t-1", "task_status": "COMPLETED"}


@pytest.mark.parametrize("status", ["DEFINED", "READY", "BLOCKED", "FAILED"])
async def test_a_task_that_is_not_completed_keeps_its_question_open(world, status):
    await world.db.create_task(
        Task(id="t-1", project_id="p", title="Ship", description="d", status=TaskStatus(status))
    )
    row = await incident(world, id="esc-task", task_id="t-1")
    assert (await resolver(world).tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["state"] == "needs_human"


async def test_only_completed_obsoletes_a_task_and_the_reason_is_spelled_out():
    # §5.5 row 3 read as §5.6 rule 3 states it concretely.  BLOCKED is exactly
    # the state a task sits in *because* the question is unanswered, and FAILED
    # is what a recovery escalation is raised against, so neither may retire it.
    assert AUTO_OBSOLETE_TASK_STATUSES == frozenset({"COMPLETED"})


async def test_a_week_of_no_activity_obsoletes_an_untouched_incident(world):
    row = await incident(world, now=world.clock())
    world.clock.advance(STALE_OBSOLETE_SECONDS - 1)
    assert (await resolver(world).tick()).closed == 0
    world.clock.advance(2)
    assert (await resolver(world).tick()).closed == 1

    closed = await world.db.get_escalation(row["id"])
    assert closed["state"] == "cancelled"
    assert closed["outcome"] == "stale_expired"
    assert closed["terminal_evidence"]["idle_seconds"] >= STALE_OBSOLETE_SECONDS


async def test_activity_resets_the_seven_day_clock(world):
    row = await incident(world, now=world.clock())
    world.clock.advance(STALE_OBSOLETE_SECONDS - 60)
    await reply(world, row, external="msg-1")
    world.clock.advance(120)
    assert (await resolver(world).tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["state"] == "reply_received"


async def test_an_open_gate_is_never_retired_by_the_seven_day_rule(world):
    gate_id = await open_gate(world)
    row = await incident(
        world,
        id="esc-gate",
        source_kind="gate",
        source_identity=gate_id,
        incident_key=f"gate:{gate_id}",
        now=world.clock(),
    )
    world.clock.advance(STALE_OBSOLETE_SECONDS * 3)
    assert (await resolver(world).tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["state"] == "needs_human"


async def test_the_auto_resolver_never_closes_anything_with_the_flag_off(world):
    gate_id = await open_gate(world)
    await world.db.create_task(
        Task(id="t-1", project_id="p", title="Ship", description="d", status=TaskStatus.COMPLETED)
    )
    gate_row = await incident(
        world,
        id="esc-gate",
        source_kind="gate",
        source_identity=gate_id,
        incident_key=f"gate:{gate_id}",
        now=world.clock(),
    )
    task_row = await incident(world, id="esc-task", task_id="t-1", now=world.clock())
    world.clock.advance(STALE_OBSOLETE_SECONDS * 3)
    await world.db.resolve_gate(gate_id, resolved_by="operator", resolution="cut from main")

    report = await resolver(world, stateful=False).tick()
    assert report.closed == 0 and report.skipped
    assert report.skipped == "discord.escalations.stateful is off"
    for row in (gate_row, task_row):
        current = await world.db.get_escalation(row["id"])
        assert current["state"] in {"needs_human", "reply_received", "resolving"}
        assert current["outcome"] is None


async def test_the_auto_resolver_closes_an_incident_once(world):
    await world.db.create_task(
        Task(id="t-1", project_id="p", title="Ship", description="d", status=TaskStatus.COMPLETED)
    )
    row = await incident(world, id="esc-task", task_id="t-1")
    resolver_once = resolver(world)
    assert (await resolver_once.tick()).closed == 1
    closed = await world.db.get_escalation(row["id"])
    # A second pass re-derives nothing: the incident is no longer in the open
    # set the rules scan, so the same evidence cannot close it twice.
    assert (await resolver_once.tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["revision"] == closed["revision"]


async def test_every_rule_names_a_stored_outcome_the_database_accepts(world):
    await world.db.create_task(
        Task(id="t-1", project_id="p", title="Ship", description="d", status=TaskStatus.COMPLETED)
    )
    row = await incident(world, id="esc-task", task_id="t-1")
    await resolver(world).tick()
    closed = await world.db.get_escalation(row["id"])
    assert closed["outcome"] in ESCALATION_OUTCOMES


async def test_an_unknown_outcome_cannot_be_written(world):
    row = await incident(world)
    from src.database.queries.escalation_queries import EscalationStateError

    with pytest.raises(EscalationStateError, match="unknown escalation outcome"):
        await world.db.transition_escalation(
            row["id"],
            expected_revision=0,
            new_state="cancelled",
            terminal_outcome="x",
            outcome="because-i-said-so",
            now=world.clock(),
        )


# ======================================================================
# §7.1 — the rollback flag restores today's create-only posts
# ======================================================================


def test_the_flag_off_plan_is_byte_for_byte_the_pre_phase_plan():
    posted = [
        {
            "dedup_key": "esc-1:root:0",
            "kind": "root",
            "status": "sent",
            "generation": 0,
            "channel_id": CHANNEL,
            "root_message_id": "m-1",
            "thread_id": "t-1",
            "updated_at": 1.0,
            "receipt_confirmed_at": 1.0,
        }
    ]

    def facts_for(state, **overrides):
        values = {
            "id": "esc-1",
            "project_id": "p",
            "state": state,
            "revision": 1,
            "severity": "high",
            "summary": "s",
            "investigation": "i",
            "decision_requested": "d",
        }
        values.update(overrides)
        return EscalationFacts.from_row(values)

    for state in ("needs_human", "reply_received", "resolving", "resolved", "cancelled"):
        for stale in (False, True):
            off = plan_deliveries(
                facts_for(state), deliveries=posted, messages=[], stateful=False, stale=stale
            )
            assert all(row.kind != "state" for row in off.deliveries)
            on = plan_deliveries(
                facts_for(state), deliveries=posted, messages=[], stateful=True, stale=stale
            )
            # With the flag on, only a *live* form that is not the root's own
            # text becomes an edit; everything else is unchanged.
            live_edit = state in {"reply_received", "resolving"} or (
                state == "needs_human" and stale
            )
            off_kinds = [row.kind for row in off.deliveries]
            assert [row.kind for row in on.deliveries] == off_kinds + (
                ["state"] if live_edit else []
            )

    # An unposted incident gets the same plan either way: the root post carries
    # whatever form the incident is in when it is first created.
    for stateful in (False, True):
        plan = plan_deliveries(
            facts_for("reply_received"), deliveries=[], messages=[], stateful=stateful
        )
        assert [row.kind for row in plan.deliveries] == ["root"]


async def test_with_the_flag_off_no_post_is_edited_in_place_and_nothing_closes(world):
    world.config.discord.escalations.stateful = False
    row = await incident(world, now=world.clock())
    await post_once(world, row)
    assert world.transport.edits == []

    await reply(world, row)
    for _ in range(3):
        await world.service().reconcile(await world.db.get_escalation(row["id"]))
        await world.service().pump()
    # Today: a reply posts an ack into the thread and the channel post stays
    # exactly as it was created.
    assert world.transport.edits == []
    assert world.transport.calls.count("post_root") == 1
    assert all(row["kind"] != "state" for row in await world.db.list_escalation_deliveries(row["id"]))

    # And the seven-day rule does not run either.
    world.clock.advance(STALE_OBSOLETE_SECONDS * 3)
    assert (await resolver(world, stateful=False).tick()).closed == 0
    assert (await world.db.get_escalation(row["id"]))["outcome"] is None


async def test_a_row_planned_before_rollback_is_not_delivered_after_it(world):
    row = await incident(world)
    await post_once(world, row)
    await world.db.enqueue_escalation_delivery(
        row["id"],
        dedup_key=state_dedup_key(row["id"], 0, "answered"),
        kind="state",
        payload={"display": "answered", "revision": 1},
        available_at=world.clock(),
        priority=5,
    )
    world.config.discord.escalations.stateful = False
    await world.service().pump()

    assert world.transport.edits == []
    delivery = (await world.db.list_escalation_deliveries(row["id"]))[-1]
    assert delivery["status"] == "unknown"
    assert "discord.escalations.stateful is off" in delivery["last_error"]


async def test_with_the_flag_off_a_closed_post_keeps_todays_wording(world):
    world.config.discord.escalations.stateful = False
    row = await incident(world)
    await post_once(world, row)
    await world.db.transition_escalation(
        row["id"],
        expected_revision=0,
        new_state="cancelled",
        terminal_outcome="The task reached COMPLETED",
        outcome="task_terminal",
        now=world.clock(),
    )
    await world.service().reconcile(await world.db.get_escalation(row["id"]))
    await world.service().pump()

    root = next(iter(world.transport.messages))
    collapsed = world.transport.messages[root].content
    assert collapsed.startswith("✅ Cancelled: The task reached COMPLETED")
    assert "No longer needed" not in collapsed
    # The retention clock is a stateful-phase fact; off means never stamped.
    assert (await world.db.get_escalation(row["id"]))["collapsed_at"] is None


# ======================================================================
# rendering — the §5.2 rows a reader of the channel actually sees
# ======================================================================


def test_the_answered_row_is_one_line_plus_the_incident_link():
    facts = EscalationFacts.from_row(
        {
            "id": "esc-1",
            "project_id": "p",
            "state": "reply_received",
            "revision": 2,
            "severity": "high",
            "summary": "s",
            "investigation": "i",
            "decision_requested": "d",
            "created_at": 1_750_000_000.0,
        }
    )
    text = render_state_root(
        facts,
        display="answered",
        base_url=BASE_URL,
        dedup_key="esc-1:state:0:answered",
        answered_at=1_750_003_600.0,
    )
    body = [line for line in text.splitlines() if not line.startswith("-# ")]
    assert body[0].startswith("💬 Answered <t:1750003600:f>")
    assert body[-1] == f"<{BASE_URL}/focus/escalations/esc-1>"
    assert text.count("<" + BASE_URL) == 1, "§3.1: exactly one link, last"


def test_the_stale_row_is_one_line_plus_the_incident_link():
    facts = EscalationFacts.from_row(
        {
            "id": "esc-1",
            "project_id": "p",
            "state": "needs_human",
            "revision": 0,
            "severity": "high",
            "summary": "s",
            "investigation": "i",
            "decision_requested": "d",
            "created_at": 1_750_000_000.0,
        }
    )
    text = render_state_root(
        facts, display="stale", base_url=BASE_URL, dedup_key="esc-1:state:0:stale"
    )
    assert "Still open since <t:1750000000:f>" in text
    assert text.endswith(f"<{BASE_URL}/focus/escalations/esc-1>")
    assert "/settings/" not in text
