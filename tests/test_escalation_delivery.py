"""One channel post and one thread per incident (discord-simplification §7).

Every test here drives the real durable outbox against
:class:`~src.escalations.transport.SinkTransport`; nothing touches a gateway.
"""

from __future__ import annotations

import pytest

from src.config import DiscordConfig, DiscordEscalationConfig
from src.database import Database
from src.escalations import (
    EscalationDeliveryService,
    EscalationFacts,
    MentionPolicy,
    SinkTransport,
    TransportAmbiguous,
    TransportMissing,
    TransportRetryable,
    TransportUnavailable,
    binding_from_deliveries,
    plan_deliveries,
    plan_replacement,
    render_root,
    sanitise,
)
from src.escalations.plan import root_dedup_key
from src.models import Project
from tests.db_fixtures import lease_dsn

BASE_URL = "https://queue.example.test"
CHANNEL = "424242424242424242"
MENTION_USER = "111111111111111111"
MENTION_ROLE = "222222222222222222"


def make_config(*, enabled: bool = True, channel_id: str = CHANNEL) -> DiscordConfig:
    return DiscordConfig(
        channel_id=channel_id,
        escalation=DiscordEscalationConfig(
            enabled=enabled,
            mention_user_ids=[MENTION_USER],
            mention_role_ids=[MENTION_ROLE],
        ),
    )


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
async def db():
    database = Database(lease_dsn("escalation_delivery"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    yield database
    await database.close()


async def make_incident(db: Database, **overrides):
    values = {
        "id": "esc-1",
        "project_id": "p",
        "task_id": "t-1",
        "source_kind": "task_attempt",
        "source_identity": "attempt-1",
        "incident_key": "task:t-1:attempt:attempt-1",
        "supervisor_owner": "supervisor-p",
        "task_title": "Deploy the migration",
        "task_status": "BLOCKED",
        "summary": "The migration cannot apply on the replica",
        "investigation": "Reproduced on staging; the replica is three revisions behind",
        "decision_requested": "Roll the replica forward or hold the release?",
        "choices": ["roll forward", "hold"],
        "severity": "high",
        "now": 10.0,
    }
    values.update(overrides)
    row, _ = await db.create_escalation(**values)
    return row


def make_service(db, transport, config=None, *, clock=None, owner="daemon-a", **kwargs):
    return EscalationDeliveryService(
        db,
        transport,
        config=config or make_config(),
        lease_owner=owner,
        base_url=BASE_URL,
        clock=clock or Clock(),
        **kwargs,
    )


def facts_for(row) -> EscalationFacts:
    return EscalationFacts.from_row(row)


# ---------------------------------------------------------------- rendering


def test_root_post_carries_the_identifying_fields_and_only_configured_mentions():
    facts = EscalationFacts(
        id="esc-1",
        project_id="agent-queue",
        state="needs_human",
        revision=0,
        severity="high",
        summary="Migration blocked",
        investigation="Checked the replica",
        decision_requested="Roll forward or hold?",
        task_id="t-1",
        task_title="Deploy the migration",
        task_status="BLOCKED",
        choices=("roll forward", "hold"),
    )
    text = render_root(
        facts,
        mentions=MentionPolicy(user_ids=(MENTION_USER,), role_ids=(MENTION_ROLE,)),
        base_url=BASE_URL,
        dedup_key=root_dedup_key("esc-1", 0),
    )

    assert "agent-queue" in text
    assert "Deploy the migration" in text and "t-1" in text
    assert "Migration blocked" in text
    assert "Roll forward or hold?" in text
    assert f"<@{MENTION_USER}>" in text and f"<@&{MENTION_ROLE}>" in text
    assert f"{BASE_URL}/settings/messaging#escalation-reply-esc-1" in text
    assert "esc-1" in text and "aq-esc:esc-1:root:0" in text


def test_replacement_root_repeats_the_incident_without_repeating_the_ping():
    facts = EscalationFacts(
        id="esc-1",
        project_id="agent-queue",
        state="needs_human",
        revision=0,
        severity="high",
        summary="Migration blocked",
        investigation="Checked the replica",
        decision_requested="Roll forward or hold?",
        task_id="t-1",
        task_title="Deploy the migration",
        task_status="BLOCKED",
    )
    mentions = MentionPolicy(user_ids=(MENTION_USER,), role_ids=(MENTION_ROLE,))
    text = render_root(
        facts,
        mentions=mentions,
        base_url=BASE_URL,
        dedup_key=root_dedup_key("esc-1", 1),
        replacement=True,
    )

    # Same incident, re-posted because the original message was deleted: the
    # configured mention belongs to the initial escalation alone.
    assert "<@" not in text
    assert "reposted" in text
    assert "Migration blocked" in text
    assert "Roll forward or hold?" in text
    assert f"{BASE_URL}/settings/messaging#escalation-reply-esc-1" in text
    assert "aq-esc:esc-1:root:1" in text


def test_authored_text_can_never_become_a_mention_or_a_log_dump():
    hostile = "@everyone <@999999999999999999> ping <@&888888888888888888>"
    assert "@everyone" not in sanitise(hostile)
    assert "<@" not in sanitise(hostile)

    facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="needs_human",
        revision=0,
        severity="low",
        summary=hostile,
        investigation="Traceback (most recent call last):\n  File x\n    boom",
        decision_requested=hostile,
        task_title=hostile,
    )
    text = render_root(facts, mentions=MentionPolicy(), base_url=BASE_URL, dedup_key="esc-1:root:0")
    assert "@everyone" not in text
    assert "<@999999999999999999>" not in text
    assert "<@&888888888888888888>" not in text
    assert "File x" not in text


def test_resolved_root_drops_the_mention_entirely():
    from src.escalations import render_resolved_root

    facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="resolved",
        revision=3,
        severity="high",
        summary="s",
        investigation="i",
        decision_requested="d",
        terminal_outcome="Rolled the replica forward",
    )
    text = render_resolved_root(facts, base_url=BASE_URL, dedup_key="esc-1:resolution:0:root")
    assert "<@" not in text
    assert "Rolled the replica forward" in text
    assert "Resolved" in text


# ------------------------------------------------------------------ planning


def test_plan_is_idempotent_and_never_asks_for_a_second_root():
    facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="needs_human",
        revision=0,
        severity="high",
        summary="s",
        investigation="i",
        decision_requested="d",
    )
    first = plan_deliveries(facts, deliveries=[], messages=[])
    assert [item.kind for item in first.deliveries] == ["root"]

    existing = [
        {
            "kind": "root",
            "dedup_key": first.deliveries[0].dedup_key,
            "generation": 0,
            "status": "sent",
            "channel_id": CHANNEL,
            "root_message_id": "m1",
            "thread_id": "th1",
        }
    ]
    assert plan_deliveries(facts, deliveries=existing, messages=[]).deliveries == ()


def test_terminal_incident_that_was_never_posted_stays_silent():
    facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="resolved",
        revision=1,
        severity="low",
        summary="s",
        investigation="i",
        decision_requested="d",
        terminal_outcome="handled in the dashboard",
    )
    plan = plan_deliveries(facts, deliveries=[], messages=[])
    assert plan.deliveries == ()
    assert plan.skipped == ("terminal incident was never posted",)


def test_replacement_is_refused_for_a_closed_incident_and_while_one_is_pending():
    open_facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="needs_human",
        revision=0,
        severity="low",
        summary="s",
        investigation="i",
        decision_requested="d",
    )
    sent_root = {
        "kind": "root",
        "dedup_key": "esc-1:root:0",
        "generation": 0,
        "status": "sent",
        "channel_id": CHANNEL,
        "root_message_id": "m1",
        "thread_id": "th1",
    }
    replacement = plan_replacement(open_facts, [sent_root])
    assert replacement is not None and replacement.generation == 1

    pending = dict(sent_root, dedup_key="esc-1:root:1", generation=1, status="pending")
    assert plan_replacement(open_facts, [sent_root, pending]) is None

    closed = EscalationFacts(**{**open_facts.__dict__, "state": "resolved"})
    assert plan_replacement(closed, [sent_root]) is None


def test_binding_prefers_the_newest_generation_that_actually_landed():
    rows = [
        {
            "kind": "root",
            "generation": 0,
            "status": "sent",
            "channel_id": CHANNEL,
            "root_message_id": "m1",
            "thread_id": "th1",
            "dedup_key": "esc-1:root:0",
        },
        {
            "kind": "root",
            "generation": 1,
            "status": "sent",
            "channel_id": CHANNEL,
            "root_message_id": "m9",
            "thread_id": "th9",
            "dedup_key": "esc-1:root:1",
        },
    ]
    binding = binding_from_deliveries(rows)
    assert (binding.root_message_id, binding.thread_id, binding.generation) == ("m9", "th9", 1)


# ------------------------------------------------------------------ delivery


async def test_repeated_events_and_a_second_daemon_produce_one_post_and_one_thread(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    first = make_service(db, sink, clock=clock, owner="daemon-a")
    second = make_service(db, sink, clock=clock, owner="daemon-b")

    await first.tick()
    # Duplicate event, concurrent sender, and a gateway reconnect in between.
    await second.tick()
    await first.tick()

    assert sink.calls.count("post_root") == 1
    assert sink.calls.count("open_thread") == 1
    deliveries = await db.list_escalation_deliveries("esc-1")
    roots = [row for row in deliveries if row["kind"] == "root"]
    assert len(roots) == 1
    assert roots[0]["status"] == "sent"
    assert roots[0]["external_receipt_id"]
    assert roots[0]["thread_id"] and roots[0]["root_message_id"]


async def test_restart_rebinds_from_stored_ids_rather_than_reposting(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    await make_service(db, sink, clock=clock, owner="daemon-a").tick()
    root = (await db.list_escalation_deliveries("esc-1"))[0]

    # A fresh process with a different lease owner and no memory at all.
    restarted = make_service(db, sink, clock=Clock(clock.now + 3600), owner="daemon-restarted")
    await restarted.tick()

    assert sink.calls.count("post_root") == 1
    after = (await db.list_escalation_deliveries("esc-1"))[0]
    assert (after["root_message_id"], after["thread_id"]) == (
        root["root_message_id"],
        root["thread_id"],
    )


async def test_partial_root_failure_creates_only_the_missing_thread_on_retry(db):
    await make_incident(db)
    sink = SinkTransport()
    sink.faults.append(("open_thread", TransportRetryable("gateway hiccup")))
    clock = Clock()
    service = make_service(db, sink, clock=clock)

    await service.tick()
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "retry"
    assert row["root_message_id"] and not row["thread_id"]
    assert "TransportRetryable" in row["last_error"]

    clock.advance(120.0)
    await service.tick()
    assert sink.calls.count("post_root") == 1
    assert sink.calls.count("open_thread") == 2
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "sent" and row["thread_id"]


async def test_ambiguous_send_is_reconciled_from_history_instead_of_reposting(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)

    # The request left and landed; the response never arrived.
    original_post = sink.post_root

    async def ambiguous_post(*, channel_id: str, content: str):
        sink.record(channel_id, content)  # it really did land
        raise TransportAmbiguous("timed out waiting for the response")

    sink.post_root = ambiguous_post  # type: ignore[method-assign]
    await service.tick()
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "retry" and "TransportAmbiguous" in row["last_error"]

    sink.post_root = original_post  # type: ignore[method-assign]
    clock.advance(120.0)
    await service.tick()

    assert sink.calls.count("post_root") == 0  # never re-sent the root
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "sent"
    assert len([m for m in sink.messages.values() if m.thread_id is None]) == 1


async def test_unreconcilable_ambiguity_is_recorded_unknown_not_reposted(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)

    async def ambiguous_post(*, channel_id: str, content: str):
        raise TransportAmbiguous("timed out waiting for the response")

    sink.post_root = ambiguous_post  # type: ignore[method-assign]
    await service.tick()
    clock.advance(120.0)
    await service.tick()

    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "unknown"
    assert "nothing was reposted" in row["last_error"]
    assert sink.messages == {}


async def test_missing_permission_is_an_actionable_fault_and_stops_after_the_budget(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock, max_attempts=2)

    async def forbidden(*, channel_id: str, content: str):
        raise TransportUnavailable("missing permission (403)")

    sink.post_root = forbidden  # type: ignore[method-assign]
    await service.tick()
    assert (await db.list_escalation_deliveries("esc-1"))[0]["status"] == "retry"
    clock.advance(600.0)
    await service.tick()
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "unknown"
    assert "TransportUnavailable" in row["last_error"]


async def test_unconfigured_channel_never_invents_one(db):
    await make_incident(db)
    sink = SinkTransport()
    service = make_service(db, sink, make_config(channel_id=""))
    await service.tick()

    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "unknown"
    assert "channel_id is not configured" in row["last_error"]
    assert sink.calls == []


async def test_the_rate_guard_defers_rather_than_dropping(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    allow = {"value": False}
    service = make_service(db, sink, clock=clock, rate_guard=lambda: allow["value"])

    await service.tick()
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "retry" and "rate guard" in row["last_error"]
    assert sink.calls == []

    allow["value"] = True
    clock.advance(120.0)
    await service.tick()
    assert (await db.list_escalation_deliveries("esc-1"))[0]["status"] == "sent"


async def test_disabling_the_external_surface_sends_nothing_and_says_so(db):
    await make_incident(db)
    sink = SinkTransport()
    report = await make_service(db, sink, make_config(enabled=False)).tick()

    assert report.skipped == "discord.escalation.enabled is false"
    assert sink.calls == []
    assert await db.list_escalation_deliveries("esc-1") == []


async def test_a_persisted_reply_is_acknowledged_once_in_the_thread(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()

    await db.accept_escalation_reply(
        "esc-1",
        transport="discord",
        external_message_id="discord-msg-1",
        verified_actor="human:discord:1",
        text="Roll it forward",
        received_at=clock.now,
    )
    clock.advance(60.0)
    await service.tick()
    clock.advance(60.0)
    await service.tick()  # replay

    acks = [row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "ack"]
    assert len(acks) == 1 and acks[0]["status"] == "sent"
    posted = [m for m in sink.messages.values() if "reviewing it" in m.content]
    assert len(posted) == 1
    assert posted[0].thread_id


async def test_resolution_posts_the_outcome_then_edits_the_root_then_archives(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()
    root_row = (await db.list_escalation_deliveries("esc-1"))[0]

    # needs_human -> reply_received (only a persisted human reply may claim it)
    # -> resolving -> resolved, which is the lifecycle §4 fixes.
    await db.accept_escalation_reply(
        "esc-1",
        transport="discord",
        external_message_id="discord-msg-1",
        verified_actor="human:discord:1",
        text="Roll it forward",
        received_at=clock.advance(30.0),
    )
    current = await db.get_escalation("esc-1")
    current = await db.transition_escalation(
        "esc-1",
        expected_revision=current["revision"],
        new_state="resolving",
        now=clock.advance(10.0),
    )
    await db.transition_escalation(
        "esc-1",
        expected_revision=current["revision"],
        new_state="resolved",
        now=clock.advance(20.0),
        terminal_outcome="Rolled the replica forward and re-ran the migration",
    )
    clock.advance(60.0)
    await service.tick()

    resolution = [
        row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "resolution"
    ]
    assert len(resolution) == 1 and resolution[0]["status"] == "sent"
    # Order matters: the outcome is in the thread before the root says
    # "resolved", and the thread is archived only after that edit lands.
    outcome_post = next(
        message
        for message in sink.messages.values()
        if "Rolled the replica forward" in message.content and message.thread_id
    )
    assert outcome_post.id in sink.messages
    assert sink.calls.index("edit_root") < sink.calls.index("archive_thread")
    assert sink.calls[: sink.calls.index("edit_root")].count("post_thread_message") >= 1
    assert "Rolled the replica forward" in sink.messages[root_row["root_message_id"]].content
    assert root_row["thread_id"] in sink.archived

    # A replayed event must not repost or reopen anything.
    calls = list(sink.calls)
    await service.tick()
    assert sink.calls == calls


async def test_a_deleted_root_earns_exactly_one_replacement_generation(db):
    await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()
    first = (await db.list_escalation_deliveries("esc-1"))[0]

    # Somebody deleted the post; a follow-up now has nowhere to go.
    sink.delete(str(first["root_message_id"]))
    sink.delete_thread(str(first["thread_id"]))
    await db.accept_escalation_reply(
        "esc-1",
        transport="discord",
        external_message_id="discord-msg-1",
        verified_actor="human:discord:1",
        text="please repost",
        received_at=clock.now,
    )
    clock.advance(60.0)
    await service.tick()

    roots = [row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "root"]
    assert sorted(row["generation"] for row in roots) == [0, 1]

    # A second failure while the replacement is still pending adds nothing.
    clock.advance(120.0)
    await service.tick()
    roots = [row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "root"]
    assert len(roots) == 2
    assert sink.calls.count("post_root") == 2  # exactly one repost


async def test_a_resolved_incident_is_never_reposted_when_its_thread_disappears(db):
    incident = await make_incident(db)
    sink = SinkTransport()
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()
    root_row = (await db.list_escalation_deliveries("esc-1"))[0]

    sink.delete_thread(str(root_row["thread_id"]))
    await db.transition_escalation(
        "esc-1",
        expected_revision=incident["revision"],
        new_state="cancelled",
        now=clock.advance(30.0),
        terminal_outcome="withdrawn by the supervisor",
    )
    clock.advance(60.0)
    await service.tick()

    roots = [row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "root"]
    assert len(roots) == 1  # no replacement generation for a closed incident
    resolution = [
        row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "resolution"
    ]
    # The outcome still reaches the root even though the thread is gone.
    assert len(resolution) == 1 and resolution[0]["status"] == "sent"
    assert "thread unavailable" in (resolution[0]["last_error"] or "")
    assert "withdrawn by the supervisor" in sink.messages[root_row["root_message_id"]].content
    assert sink.calls.count("post_root") == 1


async def test_delivery_status_changes_are_published_for_the_dashboard(db):
    await make_incident(db)
    sink = SinkTransport()
    seen: list[tuple[str, str]] = []

    async def on_status(row):
        seen.append((row["kind"], row["status"]))

    await make_service(db, sink, on_status=on_status).tick()
    assert ("root", "sent") in seen


async def test_a_transport_that_explodes_never_escapes_the_tick(db):
    await make_incident(db)

    class Broken(SinkTransport):
        async def post_root(self, *, channel_id: str, content: str):
            raise RuntimeError("boom")

    report = await make_service(db, Broken()).tick()
    assert report.sent == 0
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "sending"  # left leased; the next lease reclaims it


async def test_a_missing_thread_defers_a_follow_up_rather_than_losing_it(db):
    await make_incident(db)
    sink = SinkTransport()
    # The thread stays unavailable across the first two passes, so the reply's
    # acknowledgement has nowhere to go yet.
    sink.faults.append(("open_thread", TransportRetryable("gateway hiccup")))
    sink.faults.append(("open_thread", TransportRetryable("gateway hiccup")))
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()  # root posted, thread failed

    await db.accept_escalation_reply(
        "esc-1",
        transport="discord",
        external_message_id="discord-msg-1",
        verified_actor="human:discord:1",
        text="hold it",
        received_at=clock.now,
    )
    clock.advance(60.0)
    await service.tick()
    ack = next(row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "ack")
    assert ack["status"] == "retry"
    assert "waiting for the incident's thread" in ack["last_error"]

    clock.advance(300.0)
    await service.tick()
    ack = next(row for row in await db.list_escalation_deliveries("esc-1") if row["kind"] == "ack")
    assert ack["status"] == "sent"


async def test_thread_missing_is_told_apart_from_a_transient_failure(db):
    await make_incident(db)
    sink = SinkTransport()
    assert isinstance(TransportMissing("x"), Exception)
    clock = Clock()
    service = make_service(db, sink, clock=clock)
    await service.tick()
    row = (await db.list_escalation_deliveries("esc-1"))[0]
    assert row["status"] == "sent"
    assert facts_for(await db.get_escalation("esc-1")).state == "needs_human"


async def test_the_orchestrator_cycle_pumps_deliveries_and_survives_a_broken_one(tmp_path):
    """Discord failure must not stop the scheduler (§7)."""
    from src.config import AppConfig, DatabaseConfig
    from src.orchestrator import Orchestrator
    from src.runtimes import default_registry

    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("escalation_cycle")),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
        messaging_platform="none",
    )
    orch = Orchestrator(config, runtimes=None)
    orch._runtimes = default_registry(config=config)
    await orch.initialize()
    try:
        assert orch.escalation_delivery is None  # nothing attached, nothing ticks
        await orch.run_one_cycle()

        calls: list[int] = []

        class Stub:
            async def tick(self):
                calls.append(1)

        orch.escalation_delivery = Stub()
        await orch.run_one_cycle()
        assert calls == [1]

        class Broken:
            async def tick(self):
                raise RuntimeError("discord is down")

        orch.escalation_delivery = Broken()
        await orch.run_one_cycle()  # the cycle completes anyway
    finally:
        await orch.shutdown()
