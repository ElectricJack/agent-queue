"""The metrics sampler: one row per tick, honest roll-ups, real retention.

The sampler runs once a second for the life of the daemon, so the properties
worth pinning are the ones that go wrong slowly — a tick that duplicates its
bucket, a roll-up that sums gauges instead of averaging them, a retention
sweep that quietly keeps everything.
"""

from __future__ import annotations

import json
import time

import pytest

from src.config import AppConfig
from src.database import SQLiteDatabaseAdapter
from src.event_bus import EventBus
from src.metrics.sampler import (
    METRIC_TICK_EVENT,
    MetricsSampler,
    aggregate_samples,
    floor_bucket,
    read_machine,
)
from src.models import Agent, Project, SessionRecord, Task, TaskStatus

PROJECT = "p-metrics"

#: A timestamp that is exactly on a minute boundary, and one on an hour
#: boundary.  Bucket assertions are meaningless against an arbitrary epoch.
MINUTE = 1_699_999_980.0
HOUR = 1_699_999_200.0


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "metrics.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="metrics"))
    yield database
    await database.close()


class Clock:
    """A hand-cranked clock so bucket arithmetic is not a race."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_sampler(db, *, bus=None, clock=None, **overrides) -> MetricsSampler:
    config = AppConfig()
    for key, value in overrides.items():
        setattr(config.metrics, key, value)
    return MetricsSampler(db, config, bus, clock=clock or Clock())


async def add_ledger_row(db, *, timestamp, tokens, model=None, split=None):
    """Insert a ledger row at an explicit time.

    ``record_token_usage`` stamps ``time.time()`` itself, and these tests
    need rows on both sides of the 60-second window boundary.
    """
    from sqlalchemy import insert

    from src.database.tables import token_ledger

    async with db._engine.begin() as conn:
        await conn.execute(
            insert(token_ledger).values(
                id=f"tl-{timestamp}-{tokens}",
                project_id=PROJECT,
                agent_id="a1",
                task_id="t1",
                tokens_used=tokens,
                model=model,
                input_tokens=split[0] if split else None,
                output_tokens=split[1] if split else None,
                timestamp=timestamp,
            )
        )


async def add_session(db, sid, *, state="running", harness="claude", profile="worker", **kw):
    await db.create_session(
        SessionRecord(
            id=sid,
            task_id=None,
            project_id=PROJECT,
            profile_id=profile,
            harness=harness,
            provider="fake",
            name=f"s-{sid}",
            lifecycle=kw.pop("lifecycle", "task"),
            state=state,
            work_dir="/tmp",
            epoch="e1",
            instance_token=f"t-{sid}",
            started_at=time.time(),
            **kw,
        )
    )


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_floor_bucket_aligns_to_step():
    assert floor_bucket(1_700_000_061.9, 60) == 1_700_000_040.0
    assert floor_bucket(1_700_000_061.9, 1) == 1_700_000_061.0
    assert floor_bucket(1_700_000_061.9, 3600) == 1_699_999_200.0


def test_aggregate_averages_gauges_rather_than_summing():
    """Twelve agents held for a minute is twelve agents, not seven hundred."""
    merged = aggregate_samples(
        [
            {"agents": {"total": 10, "by_harness": {"claude": 10}}},
            {"agents": {"total": 14, "by_harness": {"claude": 14}}},
        ]
    )
    assert merged["agents"]["total"] == 12
    assert merged["agents"]["by_harness"]["claude"] == 12


def test_aggregate_takes_last_value_for_flags_and_skips_missing_keys():
    merged = aggregate_samples(
        [
            {"subagents": {"complete": True, "native": 2}},
            {"subagents": {"complete": False, "native": 4}, "extra": 9},
        ]
    )
    # The newest completeness reading wins; a stale True would overstate.
    assert merged["subagents"]["complete"] is False
    assert merged["subagents"]["native"] == 3
    # A key only one sample carried averages over that one sample, not over
    # a zero it never reported.
    assert merged["extra"] == 9


def test_aggregate_of_nothing_is_empty():
    assert aggregate_samples([]) == {}


def test_read_machine_reports_none_not_zero_for_absent_sources():
    machine = read_machine()
    for key in ("load1", "mem_total_mb"):
        assert key in machine
        assert machine[key] is None or isinstance(machine[key], float)


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


async def test_collect_folds_sessions_by_harness_profile_and_state(db):
    await add_session(db, "s1", harness="claude", profile="worker")
    await add_session(db, "s2", harness="codex", profile="worker")
    await add_session(db, "s3", harness="claude", profile="reviewer", state="starting")
    await add_session(db, "s4", harness="claude", profile="worker", state="draining")
    await add_session(db, "s5", harness="claude", profile="worker", state="stopped")

    sample = await make_sampler(db).collect()

    # Draining holds a slot but is not taking work — visible in by_state,
    # excluded from the running total.  Stopped is not live at all.
    assert sample["agents"]["total"] == 3
    assert sample["agents"]["by_state"]["draining"] == 1
    assert "stopped" not in sample["agents"]["by_state"]
    assert sample["agents"]["by_harness"] == {"claude": 2, "codex": 1}
    assert sample["agents"]["by_profile"] == {"worker": 2, "reviewer": 1}


async def test_collect_counts_tasks_by_status(db):
    for tid, status in (
        ("t1", TaskStatus.READY),
        ("t2", TaskStatus.READY),
        ("t3", TaskStatus.IN_PROGRESS),
        ("t4", TaskStatus.BLOCKED),
        ("t5", TaskStatus.COMPLETED),
    ):
        await db.create_task(
            Task(id=tid, project_id=PROJECT, title=tid, description="", status=status)
        )

    sample = await make_sampler(db).collect()

    assert sample["tasks"]["READY"] == 2
    assert sample["tasks"]["IN_PROGRESS"] == 1
    assert sample["tasks"]["BLOCKED"] == 1
    # COMPLETED is not graphed on its own but must still reach the total.
    assert sample["tasks"]["other"] == 1
    assert sample["tasks"]["total"] == 5


async def test_collect_folds_native_and_delegated_subagents(db):
    await add_session(db, "parent", hooks_provisioned=True)
    for i in range(3):
        await db.record_subagent_event(
            session_id="parent", harness="claude", event="start", subagent_id=f"c{i}"
        )
    await db.record_subagent_event(
        session_id="parent", harness="claude", event="stop", subagent_id="c0"
    )
    # An AQ-delegated child: a live task created by this session and picked up.
    await db.create_agent(Agent(id="a-worker", name="worker-1", profile_id="worker"))
    await db.create_task(
        Task(
            id="child",
            project_id=PROJECT,
            title="child",
            description="",
            status=TaskStatus.IN_PROGRESS,
            created_by_kind="session",
            created_by_id="parent",
            assigned_agent_id="a-worker",
        )
    )

    sample = await make_sampler(db).collect()

    assert sample["subagents"]["native"] == 2
    assert sample["subagents"]["aq"] == 1
    assert sample["subagents"]["total"] == 3
    assert sample["subagents"]["complete"] is True
    assert sample["subagents"]["by_session"]["s-parent"] == {
        "native": 2,
        "aq": 1,
        "hooks": True,
    }


async def test_subagent_completeness_is_false_when_a_launch_had_no_hooks(db):
    await add_session(db, "wired", hooks_provisioned=True)
    await add_session(db, "bare", hooks_provisioned=False)
    sample = await make_sampler(db).collect()
    # One session without hooks makes the fleet total a lower bound.
    assert sample["subagents"]["complete"] is False


async def test_collect_reports_token_rate_over_the_trailing_minute(db):
    now = time.time()
    await add_ledger_row(
        db, timestamp=now - 10, tokens=300, model="claude-opus-5", split=(200, 100)
    )
    await add_ledger_row(db, timestamp=now - 20, tokens=999, model="claude-opus-5")
    # Outside the trailing minute.
    await add_ledger_row(
        db, timestamp=now - 7200, tokens=50, model="claude-opus-5", split=(50, 0)
    )

    sampler = make_sampler(db, clock=lambda: now)
    sample = await sampler.collect()

    assert sample["tokens"]["input_per_min"] == 200
    assert sample["tokens"]["output_per_min"] == 100
    # Ledger volume with no input/output split is reported apart from the
    # rates rather than folded into them.
    assert sample["tokens"]["unattributed_per_min"] == 999
    assert sample["tokens"]["by_model"]["claude-opus-5"]["input_per_min"] == 200


async def test_collect_carries_slow_values_between_fast_ticks(db):
    """The expensive reads happen on their own cadence; samples stay whole."""
    clock = Clock()
    sampler = make_sampler(db, clock=clock, slow_interval_seconds=5.0)
    first = await sampler.collect()
    calls: list[int] = []
    original = db.metrics_slow_snapshot

    async def counted(since_ts):
        calls.append(1)
        return await original(since_ts)

    db.metrics_slow_snapshot = counted
    clock.advance(1)
    second = await sampler.collect()
    assert calls == []  # not recomputed …
    assert second["tokens"] == first["tokens"]  # … but still present

    clock.advance(5)
    await sampler.collect()
    assert calls == [1]


async def test_collect_reports_its_own_cost(db):
    sample = await make_sampler(db).collect()
    assert sample["sampler"]["collect_ms"] >= 0


# ---------------------------------------------------------------------------
# tick, roll-up, retention
# ---------------------------------------------------------------------------


async def test_tick_writes_exactly_one_row_per_second_and_emits(db):
    clock = Clock()
    bus = EventBus(validate_events=True)
    seen: list[dict] = []
    bus.subscribe(METRIC_TICK_EVENT, lambda data: seen.append(data))
    sampler = make_sampler(db, bus=bus, clock=clock)

    for _ in range(3):
        await sampler.tick()
        clock.advance(1)
    await sampler.flush()

    rows = await db.read_metrics_samples("1s", clock.now - 60, clock.now + 60)
    assert len(rows) == 3
    assert [row["ts"] for row in rows] == [1_700_000_000.0, 1_700_000_001.0, 1_700_000_002.0]
    assert len(seen) == 3
    assert seen[0]["_event_type"] == METRIC_TICK_EVENT


async def test_two_ticks_in_one_second_update_the_bucket_rather_than_duplicate(db):
    clock = Clock()
    sampler = make_sampler(db, clock=clock)
    await sampler.tick()
    clock.advance(0.4)
    await sampler.tick()
    await sampler.flush()
    rows = await db.read_metrics_samples("1s", clock.now - 60, clock.now + 60)
    assert len(rows) == 1


async def test_samples_are_buffered_and_committed_in_one_batch(db):
    """A commit per second is an fsync per second; the buffer is the point."""
    clock = Clock()
    sampler = make_sampler(db, clock=clock, flush_interval_seconds=5.0)
    batches: list[int] = []
    original = db.write_metrics_samples

    async def counted(resolution, rows):
        batches.append(len(rows))
        return await original(resolution, rows)

    db.write_metrics_samples = counted

    for _ in range(7):
        await sampler.tick()
        clock.advance(1)

    # Six seconds of samples, one commit — not six commits.
    assert batches == [6]
    stored = await db.read_metrics_samples("1s", 0, clock.now + 60)
    assert len(stored) == 6

    # Shutting down must not drop the second still in the buffer.
    await sampler.stop()
    assert len(await db.read_metrics_samples("1s", 0, clock.now + 60)) == 7


async def test_a_failed_flush_does_not_end_the_series(db):
    sampler = make_sampler(db)

    async def boom(resolution, rows):
        raise RuntimeError("disk full")

    db.write_metrics_samples = boom
    await sampler.tick()
    # The buffer is cleared rather than retried forever; the next tick keeps
    # sampling instead of accumulating an unbounded backlog.
    assert await sampler.flush() == 0
    assert sampler._pending == {}


async def test_rollup_averages_the_minute_and_leaves_the_open_bucket_alone(db):
    clock = Clock(MINUTE)
    sampler = make_sampler(db, clock=clock)

    # 60 seconds of the closed minute, then one second into the next.
    for i in range(60):
        await db.write_metrics_sample(
            "1s", MINUTE + i, {"ts": MINUTE + i, "agents": {"total": i}}
        )
    await db.write_metrics_sample(
        "1s", MINUTE + 60, {"ts": MINUTE + 60, "agents": {"total": 999}}
    )

    clock.now = MINUTE + 61
    written = await sampler.roll_up(clock.now)

    assert written["1m"] == 1
    minutes = await db.read_metrics_samples("1m", 0, clock.now + 600)
    assert len(minutes) == 1
    assert minutes[0]["ts"] == MINUTE
    assert minutes[0]["agents"]["total"] == pytest.approx(29.5)
    # The minute that is still filling must not be published early.
    assert all(row["ts"] != MINUTE + 60 for row in minutes)


async def test_rollup_backfills_the_minutes_a_restart_slept_through(db):
    sampler = make_sampler(db)
    for minute in range(3):
        base = MINUTE + minute * 60
        for i in range(0, 60, 10):
            await db.write_metrics_sample(
                "1s", base + i, {"ts": base + i, "agents": {"total": minute}}
            )

    written = await sampler.roll_up(MINUTE + 181)

    assert written["1m"] == 3
    minutes = await db.read_metrics_samples("1m", 0, MINUTE + 600)
    assert [row["agents"]["total"] for row in minutes] == [0, 1, 2]


async def test_rollup_is_idempotent(db):
    sampler = make_sampler(db)
    for i in range(60):
        await db.write_metrics_sample("1s", MINUTE + i, {"ts": MINUTE + i, "agents": {"total": 5}})
    await sampler.roll_up(MINUTE + 61)
    # A second pass over an already-rolled window writes nothing new: the
    # resume point has moved past it.
    assert (await sampler.roll_up(MINUTE + 61))["1m"] == 0
    assert len(await db.read_metrics_samples("1m", 0, MINUTE + 600)) == 1


async def test_rollup_of_an_idle_install_does_not_rewalk_its_whole_horizon(db):
    """An empty target tier must not re-scan its retention window forever."""
    sampler = make_sampler(db)
    reads: list[tuple] = []
    original = db.read_metrics_samples

    async def counted(resolution, from_ts, to_ts, **kw):
        reads.append((resolution, from_ts, to_ts))
        return await original(resolution, from_ts, to_ts, **kw)

    db.read_metrics_samples = counted
    await sampler.roll_up(HOUR + 7200)
    first = len(reads)
    await sampler.roll_up(HOUR + 7260)
    # One bounded read per tier per pass, not one per bucket in the horizon.
    assert first <= 2
    assert len(reads) - first <= 2


async def test_rollup_collapses_minutes_into_hours(db):
    sampler = make_sampler(db)
    for minute in range(60):
        base = HOUR + minute * 60
        await db.write_metrics_sample("1m", base, {"ts": base, "agents": {"total": 10}})
    await sampler.roll_up(HOUR + 3601)
    hours = await db.read_metrics_samples("1h", 0, HOUR + 7200)
    assert len(hours) == 1
    assert hours[0]["agents"]["total"] == 10


async def test_prune_drops_each_tier_past_its_own_horizon(db):
    now = 1_800_000_000.0
    sampler = make_sampler(
        db, retain_seconds_1s=3600, retain_seconds_1m=86400, retain_seconds_1h=0
    )
    await db.write_metrics_sample("1s", now - 10, {"a": 1})
    await db.write_metrics_sample("1s", now - 7200, {"a": 1})
    await db.write_metrics_sample("1m", now - 3600, {"a": 1})
    await db.write_metrics_sample("1m", now - 200_000, {"a": 1})
    await db.write_metrics_sample("1h", now - 100_000_000, {"a": 1})

    deleted = await sampler.prune(now)

    assert deleted["1s"] == 1
    assert deleted["1m"] == 1
    # Zero retention means "keep forever", not "delete everything".
    assert deleted["1h"] == 0
    assert len(await db.read_metrics_samples("1s", 0, now)) == 1
    assert len(await db.read_metrics_samples("1h", 0, now)) == 1


# ---------------------------------------------------------------------------
# bus-only counters
# ---------------------------------------------------------------------------


async def test_bus_only_events_become_per_hour_rates(db):
    clock = Clock()
    bus = EventBus(validate_events=False)
    sampler = make_sampler(db, bus=bus, clock=clock)
    sampler.subscribe()

    await bus.emit("task.nudged", {"task_id": "t1"})
    await bus.emit("task.nudged", {"task_id": "t2"})
    await bus.emit("session.killed", {"session_id": "s1"})
    await bus.emit("merge.succeeded", {"task_id": "t1"})

    sample = await sampler.collect()
    assert sample["stall"]["nudges_per_hour"] == 2
    assert sample["stall"]["kills_per_hour"] == 1
    assert sample["merges_per_hour"] == 1

    # Older than an hour, so out of the window.
    clock.advance(3601)
    sample = await sampler.collect()
    assert sample["stall"]["nudges_per_hour"] == 0


async def test_disabled_sampler_starts_nothing(db):
    sampler = make_sampler(db, enabled=False)
    await sampler.start()
    assert sampler._task is None
    await sampler.stop()


async def test_daemon_restart_count_survives_the_restart(db):
    assert await db.bump_daemon_start_count() == 1
    assert await db.bump_daemon_start_count() == 2
    sampler = make_sampler(db)
    sampler._daemon_starts = 2
    sample = await sampler.collect()
    # Two starts is one restart.
    assert sample["daemon"]["restarts"] == 1


async def test_stored_payload_is_compact_json(db):
    """Samples are written once a second forever; the encoding matters."""
    from sqlalchemy import select

    from src.database.tables import metrics_samples

    sampler = make_sampler(db)
    await sampler.tick()
    await sampler.flush()
    async with db._engine.connect() as conn:
        stored = (await conn.execute(select(metrics_samples.c.payload))).scalar()
    assert stored
    # json.dumps' default separators would add a space after every comma and
    # colon — a few percent of every row, forever.
    assert ", " not in stored
    assert json.loads(stored)["agents"]["total"] == 0
