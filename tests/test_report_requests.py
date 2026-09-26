"""Durable hourly author requests, CAS submission and deterministic fallback."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.commands.report_commands import ReportCommandsMixin
from src.config import (
    AppConfig,
    ConfigValidationError,
    DiscordConfig,
    DiscordDigestConfig,
    HourlyReportsConfig,
    ReportQuietHoursConfig,
    ReportsConfig,
    load_config,
)
from src.database import Database
from src.database.tables import digest_windows, supervisor_report_requests, tasks
from src.digest import DigestScheduleService, schedule_for
from src.digest.aggregate import DigestResult
from src.digest.eligibility import Eligibility
from src.digest.facts import ActiveTask, DigestWindow, WorkFact
from src.escalations.transport import SinkTransport
from src.models import Project, Task, TaskCompletion
from src.profiles.capabilities import DENY_ALL
from src.reports.hourly import MAX_BRIEF_BYTES, build_hourly_brief, local_day_bounds, quiet_at
from tests.db_fixtures import lease_dsn

CHANNEL = "424242424242424242"
BASE = 1_800_000.0
HOUR = 3600.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Commands(ReportCommandsMixin):
    def __init__(self, db) -> None:
        self.db = db


@pytest.fixture
async def db():
    database = Database(lease_dsn("report_requests"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project P"))
    yield database
    await database.close()


async def completed_task(db, task_id: str, at: float) -> None:
    await db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status="COMPLETED"))
    await db.save_task_completion(
        TaskCompletion(
            id=f"completion-{uuid4().hex}",
            task_id=task_id,
            outcome="pass",
            summary=f"completed {task_id}",
            completed_at=at,
        )
    )


def service(db, clock, *, daily_cap=12, project_ids=None):
    config = AppConfig(
        discord=DiscordConfig(
            channel_id=CHANNEL,
            digest=DiscordDigestConfig(project_ids=list(project_ids or [])),
        ),
        reports=ReportsConfig(
            hourly=HourlyReportsConfig(
                enabled=True, full_fleet_visibility=True, max_requests_per_day=daily_cap
            )
        ),
    )
    transport = SinkTransport()
    digest = DigestScheduleService(
        db,
        transport,
        config=config,
        lease_owner="test-daemon",
        base_url="https://queue.example.test",
        clock=clock,
        authoring_ready=lambda: True,
    )
    schedule = schedule_for(config.discord)
    digest._anchors[(schedule.destination, schedule.generation)] = BASE
    return digest, transport, config


async def request_for(db, digest):
    row = (await db.list_digest_windows(destination=f"discord:{CHANNEL}", limit=1))[0]
    return row, await db.get_report_request(f"report-hourly-{row['id']}")


async def test_author_submission_wins_one_window_and_preserves_one_marker(db, monkeypatch):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    digest, transport, _ = service(db, clock)
    first = await digest.tick()
    assert first.evaluated == 1 and first.sent == 0
    window, request = await request_for(db, digest)
    assert request["state"] == "reserved"
    assert window["due_at"] == BASE + HOUR + 300
    assert request["brief"]["facts"][0]["delivery"] == "unknown"
    assert request["brief"]["facts"][0]["source_url"].endswith("/tasks/t1")

    queued = await db.request_report(request["id"], now=clock.now)
    repeated = await db.request_report(request["id"], now=clock.now)
    assert queued["request_message_id"] == repeated["request_message_id"]
    assert queued["version"] == repeated["version"] == 2
    command = Commands(db)
    with principal_context(TRUSTED_LOCAL):
        result = await command._cmd_report_submit(
            {
                "request_id": request["id"],
                "brief_hash": request["brief_hash"],
                "expected_version": queued["version"],
                "text": "Completed t1; delivery to main is pending.",
                "evidence_refs": [request["brief"]["facts"][0]["key"]],
            }
        )
    assert result["success"] is True
    second = await digest.tick()
    assert second.sent == 1
    posted = next(iter(transport.messages.values())).content
    assert "Completed t1; delivery to main is pending." in posted
    assert posted.count("aq-dig:") == 1
    assert len(posted) <= 1200
    assert (await db.get_report_request(request["id"]))["state"] == "submitted"
    assert (await db.get_report_request(request["id"]))["source_links"] == [
        "https://queue.example.test/tasks/t1"
    ]


async def test_a_missing_link_posts_the_unavailable_notice_not_a_blank_line(db, monkeypatch):
    """With no usable dashboard link the report keeps its footer: the notice."""
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    digest, _transport, _ = service(db, clock)
    await digest.tick()
    _window, request = await request_for(db, digest)

    brief = dict(request["brief"])
    brief["dashboard_url"] = ""
    brief["dashboard_notice"] = "Remote dashboard link unavailable (tailscale is not running; open it on the daemon host)."
    async with db._engine.begin() as conn:
        await conn.execute(
            update(supervisor_report_requests)
            .where(supervisor_report_requests.c.id == request["id"])
            .values(brief=brief)
        )
    queued = await db.request_report(request["id"], now=clock.now)

    command = Commands(db)
    with principal_context(TRUSTED_LOCAL):
        result = await command._cmd_report_submit(
            {
                "request_id": request["id"],
                "brief_hash": request["brief_hash"],
                "expected_version": queued["version"],
                "text": "Completed t1.",
            }
        )
    assert result["success"] is True
    posted = (await db.get_report_request(request["id"]))["submitted_text"]
    assert "Remote dashboard link unavailable (tailscale is not running" in posted
    assert "queue.example.test" not in posted


async def test_deadline_falls_back_and_late_submission_cannot_edit(db, monkeypatch):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    digest, transport, _ = service(db, clock)
    await digest.tick()
    _window, request = await request_for(db, digest)
    queued = await db.request_report(request["id"], now=clock.now)
    clock.now = BASE + HOUR + 301
    assert (await digest.tick()).sent == 1
    assert (await db.get_report_request(request["id"]))["state"] == "fallback"
    with principal_context(TRUSTED_LOCAL):
        result = await Commands(db)._cmd_report_submit(
            {
                "request_id": request["id"],
                "brief_hash": request["brief_hash"],
                "expected_version": queued["version"],
                "text": "Late narrative",
            }
        )
    assert result["error_code"] == "report.closed"
    assert len(transport.messages) == 1


async def test_disable_releases_fallback_without_waiting_for_deadline(db):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, transport, config = service(db, clock)
    await digest.tick()
    _, request = await request_for(db, digest)
    config.reports.hourly.enabled = False
    clock.now += 1
    assert (await digest.tick()).sent == 1
    assert (await db.get_report_request(request["id"]))["state"] == "cancelled"
    assert len(transport.messages) == 1


async def test_restricted_destination_uses_immediate_deterministic_payload(db):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, transport, _ = service(db, clock, project_ids=["p"])
    assert (await digest.tick()).sent == 1
    window, request = await request_for(db, digest)
    assert request is None
    assert window["payload"]["author_skip_reason"] == "restricted_destination"
    assert len(transport.messages) == 1


async def test_pump_claim_freezes_fallback_before_a_late_author_write(db):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, _transport, _ = service(db, clock)
    await digest.tick()
    window, request = await request_for(db, digest)
    queued = await db.request_report(request["id"], now=clock.now)
    # Force an early due time to isolate the claim CAS from deadline rejection.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(digest_windows)
            .where(digest_windows.c.id == window["id"])
            .values(due_at=clock.now)
        )
    claimed = await db.claim_digest_windows(lease_owner="pump", now=clock.now)
    assert len(claimed) == 1
    changed = await db.submit_hourly_report(
        request["id"],
        brief_hash=request["brief_hash"],
        expected_version=queued["version"],
        text="An author raced the pump",
        evidence_refs=[],
        source_links=[],
        now=clock.now,
    )
    assert changed is None
    assert (await db.get_report_request(request["id"]))["state"] == "fallback"


async def test_daily_cap_counts_a_failed_author_reservation(db):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, transport, _ = service(db, clock, daily_cap=1)
    await digest.tick()
    first, request = await request_for(db, digest)
    assert request is not None
    clock.now = BASE + HOUR + 301
    assert (await digest.tick()).sent == 1
    await completed_task(db, "t2", BASE + HOUR + 600)
    clock.now = BASE + 2 * HOUR + 30
    assert (await digest.tick()).sent == 1
    windows = await db.list_digest_windows(destination=f"discord:{CHANNEL}", limit=2)
    newest = windows[0]
    assert newest["id"] != first["id"]
    assert newest["payload"]["author_skip_reason"] == "daily_cap"
    assert len(transport.messages) == 2


async def test_narrowed_visibility_cancels_pending_author_and_never_posts_old_text(db):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, transport, config = service(db, clock)
    await digest.tick()
    window, request = await request_for(db, digest)
    config.discord.digest.project_ids = ["p"]
    clock.now += 1
    await digest.tick()
    assert (await db.get_report_request(request["id"]))["state"] == "cancelled"
    assert (await db.get_digest_window(window["id"]))["send_status"] == "unknown"
    assert transport.messages == {}


def test_quiet_hours_span_midnight_and_local_day_handles_dst():
    config = ReportsConfig(
        timezone="America/Los_Angeles",
        hourly=HourlyReportsConfig(quiet_hours=ReportQuietHoursConfig("22:00", "07:00")),
    )
    zone = ZoneInfo(config.timezone)
    for day, hour, expected in ((8, 1, True), (8, 7, False), (8, 23, True)):
        now = datetime(2026, 3, day, hour, tzinfo=zone).timestamp()
        assert quiet_at(now, config) is expected
    spring = datetime(2026, 3, 8, 12, tzinfo=zone).timestamp()
    fall = datetime(2026, 11, 1, 12, tzinfo=zone).timestamp()
    start, end = local_day_bounds(spring, config.timezone)
    assert end - start == 23 * HOUR
    start, end = local_day_bounds(fall, config.timezone)
    assert end - start == 25 * HOUR


def test_reports_config_loads_and_rejects_invalid_zone_or_interval(tmp_path):
    path = tmp_path / "config.yaml"
    base = (
        "database:\n  url: postgresql+asyncpg://test:test@localhost/test\n"
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
    )
    path.write_text(
        base
        + "reports:\n  timezone: America/Los_Angeles\n  hourly:\n"
        + "    enabled: true\n    full_fleet_visibility: true\n"
        + "    quiet_hours:\n      start: '22:00'\n      end: '07:00'\n"
    )
    config = load_config(str(path))
    assert config.reports.hourly.enabled is True
    assert config.reports.hourly.full_fleet_visibility is True
    assert config.reports.hourly.quiet_hours.end == "07:00"
    path.write_text(base + "reports:\n  timezone: Mars/Olympus\n")
    with pytest.raises(ConfigValidationError, match="IANA time zone"):
        load_config(str(path))
    path.write_text(
        base + "reports:\n  hourly:\n    quiet_hours:\n      start: '22:00'\n      end: '22:00'\n"
    )
    with pytest.raises(ConfigValidationError, match="must differ"):
        load_config(str(path))


def test_bounded_brief_keeps_delivery_truth_active_work_and_omission_count():
    facts = tuple(
        WorkFact(
            key=f"completion:{number}",
            kind="completed",
            category="work",
            project_id="p",
            task_id=f"t{number}",
            title="Finished task",
            at=BASE + number,
            detail="completed work " * 40,
        )
        for number in range(200)
    )
    active = (ActiveTask(task_id="ongoing", project_id="p", title="Ongoing", started_at=BASE),)
    result = DigestResult(
        send=True,
        reason="activity",
        text="deterministic fallback",
        eligibility=Eligibility(send=True, reason="activity", facts=facts, active=active),
    )
    brief, digest = build_hourly_brief(
        result,
        DigestWindow(BASE, BASE + HOUR),
        destination=f"discord:{CHANNEL}",
        dashboard_url="https://queue.example.test",
    )
    encoded = json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert len(encoded.encode("utf-8")) <= MAX_BRIEF_BYTES
    assert brief["omitted"]["facts"] > 0
    assert brief["active"][0]["task_id"] == "ongoing"
    assert all(fact["delivery"] == "unknown" for fact in brief["facts"])
    assert len(digest) == 64


async def test_replaced_supervisor_token_cannot_read_or_submit():
    class StubDb:
        async def get_report_request(self, _request_id):
            return {"author_session_id": "supervisor-global", "brief": {}}

        async def get_session(self, _session_id):
            return SimpleNamespace(
                id="supervisor-global", lifecycle="named", instance_token="current-launch"
            )

    stale = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="supervisor-global",
        session_instance_token="replaced-launch",
    )
    with principal_context(stale):
        result = await Commands(StubDb())._cmd_report_brief({"request_id": "r"})
    assert result["error_code"] == "out_of_scope"
