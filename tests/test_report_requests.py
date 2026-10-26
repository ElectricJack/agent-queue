"""Durable hourly author requests, CAS submission and minimal no-report fallback."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
from src.database.tables import digest_windows, supervisor_report_requests, tasks
from src.digest import DigestScheduleService, schedule_for
from src.digest.aggregate import DigestResult
from src.digest.dispatch import marker_for
from src.digest.eligibility import Eligibility
from src.digest.facts import ActiveTask, DigestWindow, WorkFact
from src.digest.render import MAX_CHARS
from src.escalations.transport import SinkTransport
from src.models import Project, Task, TaskCompletion
from src.profiles.capabilities import DENY_ALL
from src.reports.hourly import MAX_BRIEF_BYTES, build_hourly_brief, local_day_bounds, quiet_at

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
async def db(reuse_database):
    database = await reuse_database("report_requests")
    await database.create_project(Project(id="p", name="Project P"))
    yield database


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
    assert request["brief"]["counts"] == {"facts": 1, "completed": 1, "active": 0}
    assert window["payload"]["text"] == request["fallback_text"] == (
        "No report this hour: the supervisor update is unavailable."
    )

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
    # The delivered body is precisely the author's prose plus server additions,
    # never the assembled digest or the JSON evidence brief.
    assert posted == (
        "Completed t1; delivery to main is pending.\n"
        f"[Link]({request['brief']['dashboard_url']}){marker_for(window['id'])}"
    )
    marker = marker_for(window["id"])
    assert posted.endswith(marker) and posted.count(marker) == 1
    assert "aq-dig:" not in posted
    assert len(posted) <= MAX_CHARS
    assert request["fallback_text"] not in posted
    assert (await digest.tick()).sent == 0
    assert len(transport.messages) == 1
    assert (await db.get_report_request(request["id"]))["state"] == "submitted"
    assert (await db.get_report_request(request["id"]))["source_links"] == [
        "https://queue.example.test/tasks/t1"
    ]


@pytest.mark.parametrize("extra", [-1, 0, 1])
@pytest.mark.parametrize("footer_kind", ["dashboard_url", "dashboard_notice"])
async def test_submission_budget_includes_footer_and_marker(db, monkeypatch, extra, footer_kind):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    monkeypatch.setattr("src.commands.report_commands.time.time", clock)
    digest, transport, _ = service(db, clock)
    await digest.tick()
    window, request = await request_for(db, digest)
    brief = dict(request["brief"])
    footer = (
        brief["dashboard_url"] if footer_kind == "dashboard_url" else "Dashboard unavailable."
    )
    brief["dashboard_url"] = footer if footer_kind == "dashboard_url" else ""
    brief["dashboard_notice"] = footer if footer_kind == "dashboard_notice" else ""
    # The server renders the URL as an explicit hyperlink; its Markdown also
    # spends the budget. Keep the notice branch as plain text.
    rendered_footer = f"[Link]({footer})" if footer_kind == "dashboard_url" else footer
    async with db._engine.begin() as conn:
        await conn.execute(
            update(supervisor_report_requests)
            .where(supervisor_report_requests.c.id == request["id"])
            .values(brief=brief)
        )
    queued = await db.request_report(request["id"], now=clock.now)
    tail = f"\n{rendered_footer}\n{marker_for(window['id'])}"
    text = "x" * (MAX_CHARS - len(tail) + extra)
    with principal_context(TRUSTED_LOCAL):
        result = await Commands(db)._cmd_report_submit(
            {
                "request_id": request["id"],
                "brief_hash": request["brief_hash"],
                "expected_version": queued["version"],
                "text": text,
            }
        )
    if extra > 0:
        assert result["error_code"] == "report.invalid"
        assert result["error"] == f"report exceeds {MAX_CHARS:,} characters after rendering"
        assert (await db.get_report_request(request["id"]))["state"] == "requested"
        assert transport.messages == {}
    else:
        assert result["success"] is True
        submitted = await db.get_report_request(request["id"])
        assert submitted["submitted_text"] == f"{text}\n{rendered_footer}"
        assert len(submitted["submitted_text"] + f"\n{marker_for(window['id'])}") == (
            MAX_CHARS + extra
        )
        assert (await digest.tick()).sent == 1
        posted = next(iter(transport.messages.values())).content
        # Delivery appends the invisible marker directly, without a separator.
        assert posted == f"{text}\n{rendered_footer}{marker_for(window['id'])}"
        assert len(posted) <= MAX_CHARS


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
    window, request = await request_for(db, digest)
    queued = await db.request_report(request["id"], now=clock.now)
    clock.now = BASE + HOUR + 301
    assert (await digest.tick()).sent == 1
    assert (await db.get_report_request(request["id"]))["state"] == "fallback"
    assert next(iter(transport.messages.values())).content == (
        "No report this hour: the supervisor update is unavailable."
        f"{marker_for(window['id'])}"
    )
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


@pytest.mark.parametrize(
    "skip_reason", ["playbook_inactive", "deadline_elapsed", "quiet_hours", "author_busy"]
)
async def test_unavailable_author_never_delivers_concatenated_facts(db, skip_reason):
    await completed_task(db, "t1", BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    digest, transport, config = service(db, clock)
    if skip_reason == "playbook_inactive":
        digest._authoring_ready = lambda: False
    elif skip_reason == "deadline_elapsed":
        clock.now = BASE + HOUR + 301
    elif skip_reason == "quiet_hours":
        config.reports.hourly.quiet_hours = ReportQuietHoursConfig("00:00", "23:59")
    else:
        # Hold the first author's slot through the next window.
        await digest.evaluate()
        _, first = await request_for(db, digest)
        async with db._engine.begin() as conn:
            await conn.execute(
                update(supervisor_report_requests)
                .where(supervisor_report_requests.c.id == first["id"])
                .values(deadline=BASE + 3 * HOUR)
            )
        await completed_task(db, "t2", BASE + HOUR + 600)
        clock.now = BASE + 2 * HOUR + 30
        await digest.evaluate()
    await digest.tick()
    window, request = await request_for(db, digest)
    assert request is None
    assert window["payload"]["author_skip_reason"] == skip_reason
    assert window["payload"]["text"] == (
        "No report this hour: the supervisor update is unavailable."
    )
    assert next(
        msg.content for msg in transport.messages.values()
        if msg.content.endswith(marker_for(window["id"]))
    ) == window["payload"]["text"] + marker_for(window["id"])


async def test_active_work_without_new_facts_stays_silent(db):
    clock = Clock(BASE + HOUR + 30)
    digest, transport, _ = service(db, clock)
    active = (ActiveTask(task_id="ongoing", project_id="p", title="Ongoing", started_at=BASE),)
    digest._evaluate_window = AsyncMock(return_value=DigestResult(
        send=True,
        reason="active_execution",
        text="Deterministic active-work counts must not be delivered",
        active_count=1,
        eligibility=Eligibility(send=True, reason="active_execution", facts=(), active=active),
    ))
    assert (await digest.tick()).sent == 0
    window, request = await request_for(db, digest)
    assert request is None
    assert window["send_status"] == "suppressed"
    assert window["suppression_reason"] == "no_new_facts"
    assert window["payload"] is None
    assert transport.messages == {}
    assert (await digest.tick()).evaluated == 0


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
    assert newest["payload"]["text"] == (
        "No report this hour: the supervisor update is unavailable."
    )
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


# Requests bind the global supervisor's messaging address; every launch of it is
# a UUID session row named ``n-supervisor--global`` with its own instance token.
GLOBAL_LAUNCH = "22f503aa-d483-47bc-aeb2-a867e800a529"
PROJECT_LAUNCH = "26e0b1a7-6671-472a-94cc-e1b5d71bae7f"


def launch_row(**fields):
    row = {
        "id": GLOBAL_LAUNCH,
        "name": "n-supervisor--global",
        "profile_id": "supervisor",
        "project_id": None,
        "lifecycle": "named",
        "state": "running",
        "instance_token": "current-launch",
    }
    return SimpleNamespace(**{**row, **fields})


def launch_principal(**fields):
    principal = {
        "session_id": GLOBAL_LAUNCH,
        "session_instance_token": "current-launch",
        "elevated": True,
        "project_id": None,
    }
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, **{**principal, **fields}
    )


class LaunchDb:
    def __init__(self, session) -> None:
        self.session = session

    async def get_report_request(self, _request_id):
        return {
            "author_session_id": "supervisor-global",
            "state": "requested",
            "deadline": BASE + HOUR,
            "version": 2,
            "brief_hash": "hash",
            "brief": {"facts": [], "active": []},
        }

    async def get_session(self, session_id):
        return self.session if self.session and self.session.id == session_id else None


async def test_live_global_supervisor_launch_reads_its_brief():
    with principal_context(launch_principal()):
        result = await Commands(LaunchDb(launch_row()))._cmd_report_brief({"request_id": "r"})
    assert result["success"] and result["version"] == 2


@pytest.mark.parametrize(
    ("principal", "session"),
    [
        pytest.param(
            launch_principal(session_instance_token="replaced-launch"),
            launch_row(),
            id="replaced-launch",
        ),
        pytest.param(launch_principal(elevated=False), launch_row(), id="not-elevated"),
        pytest.param(
            launch_principal(session_id=PROJECT_LAUNCH, project_id="agent-queue"),
            launch_row(id=PROJECT_LAUNCH, name="n-supervisor--agent-queue", project_id="agent-queue"),
            id="project-supervisor",
        ),
        pytest.param(
            launch_principal(),
            launch_row(name="n-supervisor--agent-queue"),
            id="other-named-session",
        ),
        pytest.param(launch_principal(), launch_row(state="stopped"), id="stopped-launch"),
        pytest.param(launch_principal(), None, id="unknown-session"),
        pytest.param(
            launch_principal(session_id="supervisor-global"),
            launch_row(),
            id="address-is-not-a-session",
        ),
    ],
)
async def test_only_the_live_global_supervisor_launch_reads_or_submits(principal, session):
    commands = Commands(LaunchDb(session))
    with principal_context(principal):
        brief = await commands._cmd_report_brief({"request_id": "r"})
        submitted = await commands._cmd_report_submit({"request_id": "r"})
    assert brief["error_code"] == submitted["error_code"] == "out_of_scope"
