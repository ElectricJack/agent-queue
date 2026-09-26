"""Shared outboxes, frozen routes and lost-event hourly author recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.report_commands import ReportCommandsMixin
from src.config import AppConfig, DiscordConfig, DiscordDigestConfig
from src.database import Database
from src.database.tables import messages
from src.digest import DigestScheduleService
from src.escalations.transport import SinkTransport, TransportAmbiguous, TransportUnavailable
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import load_definition_json
from src.playbooks.required import DEFAULT_SYSTEM_PLAYBOOK_IDS, REQUIRED_SYSTEM_PLAYBOOK_IDS
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn
from tests.test_report_requests import completed_task, service as hourly_service
from tests.test_report_requests import Clock

CHANNEL = "424242424242424242"
BASE = 1_800_000.0


@pytest.fixture
async def db():
    database = Database(lease_dsn("report_delivery"))
    await database.initialize()
    from src.models import Project

    await database.create_project(Project(id="p", name="Project"))
    yield database
    await database.close()


async def reserve(
    db,
    *,
    key="daily",
    due_at=BASE,
    text="Published branch; delivery pending.",
    thread_id=None,
    owner_kind="morning",
    destination=None,
):
    return await db.reserve_outbound_delivery(
        owner_kind=owner_kind,
        owner_id="report-1",
        dedup_key=key,
        destination=destination
        or {
            "transport": "discord",
            "channel_id": CHANNEL,
            **({"thread_id": thread_id} if thread_id else {}),
        },
        payload={"text": text},
        due_at=due_at,
        now=BASE,
    )


def dispatcher(db, transport, clock, *, digest_enabled=False, **kwargs):
    return DigestScheduleService(
        db,
        transport,
        config=AppConfig(
            discord=DiscordConfig(
                channel_id="999999999999999999",
                digest=DiscordDigestConfig(enabled=digest_enabled),
            )
        ),
        lease_owner="worker-a",
        clock=clock,
        include_outbound=True,
        **kwargs,
    )


async def test_replayed_reservation_freezes_owner_route_and_payload(db):
    (first, created), (again, replayed) = await asyncio.gather(reserve(db), reserve(db))
    assert sorted((created, replayed)) == [False, True]
    assert first == again
    for changed in (
        {"text": "other"},
        {
            "destination": {
                "transport": "discord",
                "channel_id": "111111111111111111",
            }
        },
        {"owner_kind": "conversation"},
    ):
        with pytest.raises(ValueError, match="different frozen"):
            await reserve(db, **changed)
    assert (await db.get_outbound_delivery(first["id"]))["payload"] == first["payload"]


async def test_disabled_digest_still_delivers_frozen_outbound_route(db):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db, text="Hello <@123> @everyone `trace`\x1b")
    report = await dispatcher(db, transport, clock).tick()
    assert report.skipped == "discord.digest.enabled is false"
    sent = await db.get_outbound_delivery(row["id"])
    assert sent["state"] == "sent" and sent["external_receipt_id"]
    message = next(iter(transport.messages.values()))
    assert message.where == CHANNEL
    assert message.content.endswith(row["marker"])
    assert "@everyone" not in message.content and "<@123>" not in message.content
    assert "\x1b" not in message.content


async def test_dispatch_selects_oldest_across_domain_tables_with_bounded_batch(db):
    clock, transport = Clock(BASE + 10), SinkTransport()
    early, _ = await reserve(db, key="early", due_at=BASE)
    later, _ = await reserve(db, key="later", due_at=BASE + 2)
    window, _ = await db.reserve_digest_window(
        destination=f"discord:{CHANNEL}",
        config_generation=1,
        window_start=BASE - 3600,
        window_end=BASE,
        due_at=BASE + 1,
        activity_cursor=None,
    )
    await db.complete_digest_evaluation(
        window["id"],
        payload={"text": "Digest"},
        output_hash="digest",
        suppression_reason=None,
        activity_cursor={},
    )
    pump = dispatcher(db, transport, clock, digest_enabled=True)
    await pump.pump(limit=2)
    assert (await db.get_outbound_delivery(early["id"]))["state"] == "sent"
    assert (await db.get_digest_window(window["id"]))["send_status"] == "sent"
    assert (await db.get_outbound_delivery(later["id"]))["attempt_count"] == 0
    assert [m.where for m in transport.messages.values()] == [CHANNEL, CHANNEL]
    with pytest.raises(ValueError, match="batch"):
        await pump.pump(limit=21)


async def test_concurrent_dispatchers_claim_one_logical_send(db):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db)
    await asyncio.gather(*(dispatcher(db, transport, clock).pump() for _ in range(2)))
    assert len(transport.messages) == 1
    assert (await db.get_outbound_delivery(row["id"]))["attempt_count"] == 1


async def test_new_escalation_or_hot_guard_stops_before_claiming_next_message(db):
    clock, transport = Clock(BASE), SinkTransport()
    first, _ = await reserve(db, key="first")
    second, _ = await reserve(db, key="second")

    async def priority(now):
        return int(bool(transport.messages))

    pump = dispatcher(db, transport, clock, escalation_priority=priority)
    assert "priority" in (await pump.pump()).deferred
    assert len(transport.messages) == 1
    rows = [await db.get_outbound_delivery(row["id"]) for row in (first, second)]
    assert sorted(row["attempt_count"] for row in rows) == [0, 1]
    pump = dispatcher(db, transport, clock, rate_guard=lambda: False)
    assert "guard" in (await pump.pump()).deferred
    assert sorted(
        [(await db.get_outbound_delivery(row["id"]))["attempt_count"] for row in (first, second)]
    ) == [0, 1]


@pytest.mark.parametrize("landed", [True, False])
async def test_ambiguous_write_reconciles_or_stays_unknown_without_repost(db, landed):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db)

    async def ambiguous(*, channel_id, content):
        if landed:
            transport.record(channel_id, content)
        raise TransportAmbiguous("ack lost")

    transport.post_root = ambiguous
    await dispatcher(db, transport, clock).pump()
    stored = await db.get_outbound_delivery(row["id"])
    assert stored["state"] == ("sent" if landed else "unknown")
    clock.now += 10000
    await dispatcher(db, transport, clock).pump()
    assert (await db.get_outbound_delivery(row["id"]))["attempt_count"] == 1
    assert len(transport.messages) == int(landed)


@pytest.mark.parametrize("history", [True, False])
async def test_expired_lease_reconciles_crash_without_blind_resend(db, history):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db)
    claimed = await db.claim_report_deliveries(lease_owner="dead", now=BASE)
    assert claimed[0]["id"] == row["id"]
    transport.record(CHANNEL, f"{row['payload']['text']}\n{row['marker']}")
    if not history:

        async def forbidden(**kwargs):
            raise TransportUnavailable("READ_MESSAGE_HISTORY missing")

        transport.find_marker = forbidden
    clock.now += 121
    await dispatcher(db, transport, clock).pump()
    assert len(transport.messages) == 1
    assert (await db.get_outbound_delivery(row["id"]))["state"] == (
        "sent" if history else "unknown"
    )
    assert (
        await db.finish_outbound_delivery(
            row["id"],
            lease_owner="dead",
            status="sent",
            now=clock.now,
            external_receipt_id="stale",
        )
        is None
    )


async def test_permission_faults_back_off_and_stop_without_burning_held_attempts(db):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db)
    transport.faults.extend(
        ("post_root", TransportUnavailable("SEND_MESSAGES missing")) for _ in range(2)
    )
    pump = dispatcher(db, transport, clock, max_attempts=2)
    await pump.pump()
    stored = await db.get_outbound_delivery(row["id"])
    assert stored["state"] == "retry" and stored["due_at"] > BASE
    await pump.pump()
    assert (await db.get_outbound_delivery(row["id"]))["attempt_count"] == 1
    clock.now = stored["due_at"]
    await pump.pump()
    assert (await db.get_outbound_delivery(row["id"]))["state"] == "unknown"
    assert not transport.messages


async def test_thread_adapter_and_morning_summary_budget(db):
    clock, transport = Clock(BASE), SinkTransport()
    row, _ = await reserve(db, text="x" * 1400)
    transport.threads["thread-1"] = "root-1"
    thread, _ = await reserve(db, key="reply", thread_id="thread-1", owner_kind="conversation")
    await dispatcher(db, transport, clock).pump()
    assert (await db.get_outbound_delivery(row["id"]))["state"] == "sent"
    assert (await db.get_outbound_delivery(thread["id"]))["state"] == "sent"
    assert any(message.thread_id == "thread-1" for message in transport.messages.values())
    with pytest.raises(ValueError, match="1500"):
        await reserve(db, key="too-long", text="x" * 1500)


class Commands(ReportCommandsMixin):
    def __init__(self, db, config):
        self.db = db
        self.orchestrator = SimpleNamespace(config=config)


async def test_lost_window_event_and_duplicate_ticks_queue_one_author_message(db, monkeypatch):
    clock = Clock(BASE)
    digest, _, _ = hourly_service(db, clock)
    await digest.tick()
    await completed_task(db, "done", BASE + 600)
    clock.now += 3600
    await digest.evaluate()  # no event subscriber; the request is nevertheless durable
    commands = Commands(db, digest._config)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    with principal_context(ExecutionPrincipal.service("reports")):
        results = await asyncio.gather(*(commands._cmd_report_reconcile({}) for _ in range(2)))
    assert all(result["success"] for result in results)
    async with db._engine.connect() as conn:
        queued = (
            (await conn.execute(select(messages).where(messages.c.body_kind == "report_request")))
            .mappings()
            .all()
        )
    assert len(queued) == 1
    assert queued[0]["to_id"] == "supervisor-global"
    with principal_context(ExecutionPrincipal.service("reports")):
        assert (await commands._cmd_report_reconcile({}))["requested"] == 0


async def test_disabled_or_narrowed_reports_never_request_author(db, monkeypatch):
    clock = Clock(BASE)
    digest, _, _ = hourly_service(db, clock)
    await digest.tick()
    await completed_task(db, "done", BASE + 600)
    clock.now += 3600
    report = await digest.evaluate()
    commands = Commands(db, digest._config)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    digest._config.reports.hourly.full_fleet_visibility = False
    with principal_context(ExecutionPrincipal.service("reports")):
        assert (await commands._cmd_report_reconcile({}))["requested"] == 0
    row = await db.get_digest_window(report.window_id)
    assert row["send_status"] == "unknown"
    async with db._engine.connect() as conn:
        assert not (
            await conn.execute(select(messages).where(messages.c.body_kind == "report_request"))
        ).all()
    for principal in (
        ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL),
        ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL, project_id="private"),
    ):
        with principal_context(principal):
            assert not (await commands._cmd_report_reconcile({}))["success"]


def test_optional_bundle_uses_only_durable_requests_and_no_model_steps():
    source = Path("src/prompts/default_playbooks/supervisor-hourly-report.md")
    parsed = PlaybookSource.load(source, vault_root=source.parent)
    assert parsed.frontmatter["enabled"] is False
    definition = load_definition_json(
        Path("tests/fixtures/playbooks/v2/supervisor-hourly-report/artifact.json").read_text()
    )
    assert {r.trigger.event_type for r in definition.rules} == {"digest.window_ready", "timer.1m"}
    assert {step.type for step in definition.steps.values()} == {"command", "terminal"}
    assert definition.id not in REQUIRED_SYSTEM_PLAYBOOK_IDS + DEFAULT_SYSTEM_PLAYBOOK_IDS


async def test_migration_creates_missing_outbox_and_preserves_existing_receipts(db):
    import importlib
    from unittest.mock import patch
    from src.database.tables import outbound_deliveries

    revision = importlib.import_module("migrations.versions.a00000000032_outbound_deliveries")

    def create(bind):
        outbound_deliveries.drop(bind)
        with patch.object(revision.op, "get_bind", return_value=bind):
            revision.upgrade()
            revision.upgrade()

    async with db._engine.begin() as conn:
        await conn.run_sync(create)
    row, _ = await reserve(db)
    await dispatcher(db, SinkTransport(), Clock(BASE)).pump()

    def repeat(bind):
        with patch.object(revision.op, "get_bind", return_value=bind):
            revision.upgrade()
            revision.downgrade()

    async with db._engine.begin() as conn:
        await conn.run_sync(repeat)
    assert (await db.get_outbound_delivery(row["id"]))["state"] == "sent"


async def test_disabling_author_activation_releases_reserved_fallback_immediately(db):
    clock = Clock(BASE)
    digest, _, _ = hourly_service(db, clock)
    await digest.tick()
    await completed_task(db, "done", BASE + 600)
    clock.now += 3600
    report = await digest.evaluate()
    before = await db.get_digest_window(report.window_id)
    assert before["due_at"] > clock.now
    digest._authoring_ready = lambda: False
    assert (await digest.tick()).sent == 1
    request = await db.get_report_request(f"report-hourly-{report.window_id}")
    assert request["state"] == "cancelled"
    async with db._engine.connect() as conn:
        assert not (
            await conn.execute(select(messages).where(messages.c.body_kind == "report_request"))
        ).all()


async def test_escalation_probe_failure_holds_all_shared_sends(db):
    row, _ = await reserve(db)
    transport = SinkTransport()

    async def unavailable(now):
        raise RuntimeError("priority database unavailable")

    report = await dispatcher(db, transport, Clock(BASE), escalation_priority=unavailable).pump()
    assert report.deferred == "escalation priority unavailable"
    assert (await db.get_outbound_delivery(row["id"]))["attempt_count"] == 0
    assert not transport.messages


async def test_shared_pump_and_submission_choose_one_frozen_winner(db):
    from sqlalchemy import update
    from src.database.tables import digest_windows

    clock = Clock(BASE + 3600)
    digest, transport, _ = hourly_service(db, clock)
    await completed_task(db, "done", BASE + 600)
    report = await digest.evaluate()
    request_id = f"report-hourly-{report.window_id}"
    request = await db.request_report(request_id, now=clock.now)
    # Make fallback eligible while the author CAS races its claim. Real pump
    # and author paths both lock request before window in the shared dispatcher.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(digest_windows)
            .where(digest_windows.c.id == report.window_id)
            .values(due_at=clock.now)
        )
    digest._include_outbound = True
    submitted, pumped = await asyncio.gather(
        db.submit_hourly_report(
            request_id,
            brief_hash=request["brief_hash"],
            expected_version=request["version"],
            text="Author narrative",
            evidence_refs=[],
            source_links=[],
            now=clock.now,
        ),
        digest.pump(),
    )
    window = await db.get_digest_window(report.window_id)
    assert window["send_status"] == "sent"
    assert len(transport.messages) == 1
    expected = "Author narrative" if submitted else request["fallback_text"]
    assert next(iter(transport.messages.values())).content.startswith(expected)
    assert pumped.sent == 1
