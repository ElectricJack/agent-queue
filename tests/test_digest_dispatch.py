"""Scheduling and delivering the hourly digest (implementation spec §8).

Every test drives the real ``digest_windows`` outbox with an injected clock
against :class:`~src.escalations.transport.SinkTransport`; nothing here touches
a gateway or a wall clock.  The properties under test are the ones §8 states
as absolutes: one normal message per window however many evaluators run, a
persisted silent window for an idle hour, one *labelled* catch-up after an
outage rather than an hourly backlog, a new schedule generation that does not
replay history into a newly selected channel, and an external send that is
never assumed to have landed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import update

from src.config import DiscordConfig, DiscordDigestConfig, DiscordEscalationConfig
from src.database import Database
from src.database.tables import tasks
from src.digest import DigestScheduleService, schedule_for
from src.digest.dispatch import MARKER_PREFIX, marker_for
from src.escalations.transport import (
    SinkTransport,
    TransportAmbiguous,
    TransportRetryable,
    TransportUnavailable,
)
from src.models import Project, Task, TaskCompletion
from tests.db_fixtures import lease_dsn

CHANNEL = "424242424242424242"
HOUR = 3600.0
#: A round grid boundary, so a window's bounds are easy to read in a failure.
BASE = 1_800_000.0
BASE_URL = "https://queue.example.test"


def make_config(
    *,
    enabled: bool = True,
    channel_id: str = CHANNEL,
    interval_minutes: int = 60,
    catchup_hours: int = 24,
    project_ids: list[str] | None = None,
    categories: list[str] | None = None,
) -> DiscordConfig:
    return DiscordConfig(
        channel_id=channel_id,
        digest=DiscordDigestConfig(
            enabled=enabled,
            interval_minutes=interval_minutes,
            catchup_hours=catchup_hours,
            project_ids=list(project_ids or []),
            categories=list(categories or ["work", "vcs", "budget", "system"]),
        ),
        escalation=DiscordEscalationConfig(enabled=True),
    )


class Clock:
    def __init__(self, now: float = BASE) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
async def db():
    database = Database(lease_dsn("digest_dispatch"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Agent Queue"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


def make_service(db, transport, config=None, *, clock=None, owner="daemon-a", **kwargs):
    return DigestScheduleService(
        db,
        transport,
        config=config or make_config(),
        lease_owner=owner,
        base_url=BASE_URL,
        clock=clock or Clock(),
        **kwargs,
    )


async def make_task(db, task_id, *, project_id="p", title=None):
    await db.create_task(
        Task(id=task_id, project_id=project_id, title=title or task_id, description="")
    )
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status="COMPLETED"))


async def complete(db, task_id, *, at, summary="shipped the thing", project_id="p"):
    await make_task(db, task_id, project_id=project_id)
    await db.save_task_completion(
        TaskCompletion(
            id="completion-" + uuid4().hex,
            task_id=task_id,
            outcome="pass",
            summary=summary,
            completed_at=at,
        )
    )


async def windows(db, **kwargs):
    return await db.list_digest_windows(destination=f"discord:{CHANNEL}", **kwargs)


# ------------------------------------------------------- window boundaries


def test_bounds_are_snapped_to_the_interval_grid_so_two_daemons_agree():
    schedule = schedule_for(make_config())
    # Two evaluators a few seconds apart inside the same hour must compute the
    # same window key, or the uniqueness constraint can never fire.
    first = schedule.due_window(BASE + HOUR + 5, anchor=BASE)
    second = schedule.due_window(BASE + HOUR + 900, anchor=BASE)
    assert first == second
    assert first is not None and (first.since, first.until) == (BASE, BASE + HOUR)
    assert not first.catchup


def test_nothing_is_due_before_the_first_boundary_passes():
    schedule = schedule_for(make_config())
    assert schedule.due_window(BASE + 60, anchor=BASE) is None


def test_a_long_gap_becomes_one_catchup_window_bounded_by_the_horizon():
    schedule = schedule_for(make_config(catchup_hours=24))
    window = schedule.due_window(BASE + 100 * HOUR, anchor=BASE)
    assert window is not None
    assert window.catchup
    # Not 100 messages, and not 100 hours of history: one window, 24h wide.
    assert window.until - window.since == 24 * HOUR


# ------------------------------------------------------------- evaluation


async def test_an_hour_with_work_reserves_one_window_and_sends_one_message(db):
    await complete(db, "t1", at=BASE + 600, summary="finished the migration")
    clock = Clock(BASE + HOUR + 30)
    service = make_service(db, transport := SinkTransport(), clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE

    report = await service.tick()
    assert report.evaluated == 1
    assert report.sent == 1
    assert len(transport.messages) == 1
    posted = next(iter(transport.messages.values())).content
    assert "finished the migration" in posted
    assert MARKER_PREFIX in posted

    rows = await windows(db)
    assert len(rows) == 1
    assert rows[0]["send_status"] == "sent"
    assert rows[0]["external_receipt_id"]
    assert rows[0]["payload"]["reported_keys"]


async def test_an_idle_hour_is_persisted_as_silence_rather_than_rescanned(db):
    clock = Clock(BASE + HOUR + 30)
    service = make_service(db, transport := SinkTransport(), clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE

    report = await service.tick()
    assert report.suppressed == 1
    assert report.sent == 0
    assert transport.messages == {}

    rows = await windows(db)
    assert len(rows) == 1
    assert rows[0]["send_status"] == "suppressed"
    assert rows[0]["suppression_reason"]

    # The suppressed window is the anchor, so the same hour is never revisited.
    clock.advance(60)
    assert (await service.tick()).evaluated == 0
    assert len(await windows(db)) == 1


async def test_a_second_daemon_on_the_same_boundary_produces_one_message(db):
    await complete(db, "t1", at=BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    key = (f"discord:{CHANNEL}", schedule_for(make_config()).generation)
    first = make_service(db, transport, clock=clock, owner="daemon-a")
    second = make_service(db, transport, clock=clock, owner="daemon-b")
    first._anchors[key] = BASE
    second._anchors[key] = BASE

    reports = [await first.tick(), await second.tick()]
    assert sum(r.evaluated for r in reports) == 1
    assert sum(r.sent for r in reports) == 1
    assert len(transport.messages) == 1
    assert len(await windows(db)) == 1


async def test_a_restart_mid_hour_does_not_re_send_the_window(db):
    await complete(db, "t1", at=BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    key = (f"discord:{CHANNEL}", schedule_for(make_config()).generation)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    service._anchors[key] = BASE
    await service.tick()

    # A brand-new process: no in-memory anchor at all, only the durable rows.
    restarted = make_service(db, transport, clock=clock, owner="daemon-restarted")
    clock.advance(120)
    report = await restarted.tick()
    assert report.evaluated == 0
    assert report.sent == 0
    assert len(transport.messages) == 1


async def test_a_long_outage_produces_one_labelled_catchup_not_hourly_backlog(db):
    # One completion inside the bounded horizon and one far outside it: the
    # catch-up covers the recent horizon, not the whole outage.  Older
    # activity stays in the dashboard, which is exactly what §8 asks for.
    await complete(db, "old", at=BASE + 600, summary="landed before the outage")
    await complete(db, "t1", at=BASE + 99 * HOUR, summary="landed during the outage")
    clock = Clock(BASE + 100 * HOUR)
    service = make_service(db, transport := SinkTransport(), clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE

    report = await service.tick()
    assert report.catchup == 1
    assert report.evaluated == 1
    assert len(transport.messages) == 1
    posted = next(iter(transport.messages.values())).content
    assert "catch-up" in posted
    assert "landed during the outage" in posted
    assert "landed before the outage" not in posted
    rows = await windows(db)
    assert len(rows) == 1
    assert rows[0]["is_catchup"] is True


async def test_a_configuration_change_starts_a_new_generation_without_replay(db):
    await complete(db, "t1", at=BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE
    await service.tick()
    assert len(transport.messages) == 1

    # Point the installation at another channel: a different destination and a
    # different generation.  The new one anchors *now* and must not replay the
    # hours the retired schedule already owned.
    moved = make_config(channel_id="999999999999999999")
    service._config = moved
    clock.advance(HOUR)
    assert (await service.tick()).evaluated == 0
    assert len(transport.messages) == 1

    # It starts producing from its own anchor one interval later.
    clock.advance(HOUR)
    await complete(db, "t2", at=clock.now - 600, summary="after the move")
    report = await service.tick()
    assert report.evaluated == 1
    assert report.sent == 1
    moved_rows = await db.list_digest_windows(destination="discord:999999999999999999")
    assert len(moved_rows) == 1
    assert moved_rows[0]["window_start"] >= BASE + HOUR


async def test_activity_outside_the_configured_projects_is_filtered_to_silence(db):
    await complete(db, "t1", at=BASE + 600, project_id="other", summary="not selected")
    clock = Clock(BASE + HOUR + 30)
    config = make_config(project_ids=["p"])
    service = make_service(db, transport := SinkTransport(), config=config, clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(config).generation)] = BASE

    report = await service.tick()
    assert report.suppressed == 1
    assert transport.messages == {}
    assert (await windows(db))[0]["send_status"] == "suppressed"


async def test_a_fact_reported_once_is_never_reported_again(db):
    await complete(db, "t1", at=BASE + 600, summary="only news once")
    clock = Clock(BASE + HOUR + 30)
    service = make_service(db, transport := SinkTransport(), clock=clock)
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE
    await service.tick()
    assert len(transport.messages) == 1

    # The next window's lookback sees the same completion row again; the fact
    # key says it was already reported, so the hour stays silent.
    clock.advance(HOUR)
    report = await service.tick()
    assert report.suppressed == 1
    assert len(transport.messages) == 1


async def test_disabling_the_digest_sends_nothing_and_says_so(db):
    await complete(db, "t1", at=BASE + 600)
    clock = Clock(BASE + HOUR + 30)
    service = make_service(db, transport := SinkTransport(), config=make_config(enabled=False),
                           clock=clock)
    report = await service.tick()
    assert report.skipped == "discord.digest.enabled is false"
    assert transport.messages == {}
    assert await windows(db) == []


# --------------------------------------------------------------- delivery


async def _reserve_sendable(db, *, clock, service):
    """Evaluate one eligible window without letting it be delivered."""
    await complete(db, "t1", at=BASE + 600, summary="ready to post")
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE
    await service.evaluate()
    rows = await windows(db)
    assert rows and rows[0]["payload"] is not None
    return rows[0]


async def test_escalations_take_priority_and_the_digest_claims_nothing(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()

    async def owed(now: float) -> int:
        return 2

    service = make_service(db, transport, clock=clock, escalation_priority=owed)
    row = await _reserve_sendable(db, clock=clock, service=service)

    report = await service.pump()
    assert report.deferred == "2 escalation deliveries take priority"
    assert transport.calls == []
    # Nothing was leased and no attempt was spent establishing the priority.
    after = await db.get_digest_window(row["id"])
    assert after["send_status"] == "pending"
    assert after["attempt_count"] == 0
    assert after["lease_owner"] is None


async def test_the_rate_guard_holds_the_digest_without_burning_an_attempt(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock, rate_guard=lambda: False)
    row = await _reserve_sendable(db, clock=clock, service=service)

    report = await service.pump()
    assert report.deferred == "held by the Discord invalid-request rate guard"
    assert transport.calls == []
    after = await db.get_digest_window(row["id"])
    assert after["send_status"] == "pending"
    assert after["attempt_count"] == 0


async def test_a_transient_failure_retries_with_backoff_then_sends(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    transport.faults.append(("post_root", TransportRetryable("gateway hiccup")))
    service = make_service(db, transport, clock=clock)
    row = await _reserve_sendable(db, clock=clock, service=service)

    report = await service.pump()
    assert report.retried == 1
    after = await db.get_digest_window(row["id"])
    assert after["send_status"] == "retry"
    assert after["due_at"] > clock.now

    clock.now = after["due_at"]
    assert (await service.pump()).sent == 1
    assert (await db.get_digest_window(row["id"]))["send_status"] == "sent"
    assert len(transport.messages) == 1


async def test_an_ambiguous_send_is_reconciled_from_history_not_reposted(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    row = await _reserve_sendable(db, clock=clock, service=service)

    original = transport.post_root

    async def ambiguous(*, channel_id: str, content: str):
        # The request left and landed; the response never came back.
        transport.calls.append("post_root")
        transport.record(channel_id, content)
        raise TransportAmbiguous("timed out waiting for the response")

    transport.post_root = ambiguous
    report = await service.pump()
    # Reconciled inside the same attempt: the marker is already in history.
    assert report.sent == 1
    transport.post_root = original
    assert len(transport.messages) == 1
    assert (await db.get_digest_window(row["id"]))["send_status"] == "sent"


async def test_unreconcilable_ambiguity_is_recorded_unknown_not_reposted(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    row = await _reserve_sendable(db, clock=clock, service=service)

    async def ambiguous(*, channel_id: str, content: str):
        transport.calls.append("post_root")
        raise TransportAmbiguous("timed out waiting for the response")

    transport.post_root = ambiguous
    first = await service.pump()
    assert first.retried == 1
    stored = await db.get_digest_window(row["id"])
    assert stored["last_error"].startswith("ambiguous:")

    clock.now = stored["due_at"]
    second = await service.pump()
    assert second.unknown == 1
    assert transport.messages == {}
    final = await db.get_digest_window(row["id"])
    assert final["send_status"] == "unknown"
    assert "nothing was reposted" in final["last_error"]


async def test_a_lost_acknowledgement_is_recovered_from_the_marker(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    row = await _reserve_sendable(db, clock=clock, service=service)

    # The previous process posted and died before it could record the receipt;
    # its lease expires and this one reclaims the window.
    transport.record(CHANNEL, f"body\n{marker_for(row['id'])}")
    async with db._engine.begin() as conn:
        from src.database.tables import digest_windows

        await conn.execute(
            update(digest_windows)
            .where(digest_windows.c.id == row["id"])
            .values(send_status="sending", attempt_count=1, lease_owner="dead",
                    lease_expires_at=clock.now - 1)
        )

    report = await service.pump()
    assert report.sent == 1
    assert len(transport.messages) == 1  # recovered, never reposted
    assert (await db.get_digest_window(row["id"]))["send_status"] == "sent"


async def test_a_missing_permission_is_bounded_and_ends_in_attention_needed(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock, max_attempts=2)
    row = await _reserve_sendable(db, clock=clock, service=service)

    async def forbidden(*, channel_id: str, content: str):
        transport.calls.append("post_root")
        raise TransportUnavailable("missing SEND_MESSAGES in the configured channel")

    transport.post_root = forbidden
    for _ in range(4):
        stored = await db.get_digest_window(row["id"])
        if stored["send_status"] == "unknown":
            break
        clock.now = max(clock.now, stored["due_at"])
        await service.pump()
    final = await db.get_digest_window(row["id"])
    assert final["send_status"] == "unknown"
    assert "SEND_MESSAGES" in final["last_error"]
    assert transport.messages == {}


async def test_an_unconfigured_channel_never_invents_one(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, config=make_config(channel_id=""), clock=clock)
    await complete(db, "t1", at=BASE + 600)
    service._anchors[("discord:unconfigured", schedule_for(make_config(channel_id="")).generation)]\
        = BASE
    report = await service.tick()
    assert report.unknown == 1
    assert transport.calls == []
    rows = await db.list_digest_windows(destination="discord:unconfigured")
    assert rows[0]["send_status"] == "unknown"
    assert "not configured" in rows[0]["last_error"]


async def test_a_window_older_than_the_catchup_horizon_is_never_posted_late(db):
    clock = Clock(BASE + HOUR + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    row = await _reserve_sendable(db, clock=clock, service=service)

    # Discord was unreachable for a week; posting last Tuesday's hour now is
    # exactly the stale backlog §8 rules out.
    clock.advance(7 * 24 * HOUR)
    report = await service.pump()
    assert report.expired == 1
    assert transport.messages == {}
    final = await db.get_digest_window(row["id"])
    assert final["send_status"] == "unknown"
    assert "catch-up horizon" in final["last_error"]


async def test_a_transport_that_explodes_never_escapes_the_tick(db):
    clock = Clock(BASE + HOUR + 30)

    class Broken(SinkTransport):
        async def post_root(self, *, channel_id: str, content: str):
            raise RuntimeError("boom")

    service = make_service(db, Broken(), clock=clock)
    await _reserve_sendable(db, clock=clock, service=service)
    report = await service.tick()
    assert report.sent == 0  # and no exception reached the orchestrator cycle


async def test_the_rendered_body_leaves_room_for_the_marker(db):
    """The finished message, marker included, still honours the §8 budget."""
    clock = Clock(BASE + HOUR + 30)
    service = make_service(db, transport := SinkTransport(), clock=clock)
    for index in range(40):
        await complete(
            db,
            f"t{index}",
            at=BASE + 600 + index,
            summary=f"completed a long-titled unit of work number {index} " + "x" * 90,
        )
    service._anchors[(f"discord:{CHANNEL}", schedule_for(make_config()).generation)] = BASE
    await service.tick()
    assert len(transport.messages) == 1
    assert len(next(iter(transport.messages.values())).content) <= 1200


async def test_the_orchestrator_cycle_evaluates_and_survives_a_broken_digest(tmp_path):
    """A Discord outage must not stop scheduling (§7/§8)."""
    from src.config import AppConfig, DatabaseConfig
    from src.orchestrator import Orchestrator
    from src.runtimes import default_registry

    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("digest_cycle")),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
        messaging_platform="none",
    )
    orch = Orchestrator(config, runtimes=None)
    orch._runtimes = default_registry(config=config)
    await orch.initialize()
    try:
        assert orch.digest_schedule is None  # nothing attached, nothing ticks
        await orch.run_one_cycle()

        calls: list[int] = []

        class Stub:
            async def tick(self):
                calls.append(1)

        orch.digest_schedule = Stub()
        await orch.run_one_cycle()
        assert calls == [1]

        class Broken:
            async def tick(self):
                raise RuntimeError("discord is down")

        orch.digest_schedule = Broken()
        await orch.run_one_cycle()  # the cycle completes anyway
    finally:
        await orch.shutdown()
