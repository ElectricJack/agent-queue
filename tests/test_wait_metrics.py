"""Wait accounting reproduces from persisted timestamps, including window edges."""

import json

import pytest
from sqlalchemy import insert, text, update

from src.api.models.metrics import MetricsSample
from src.config_wait_policy import parse_wait_policy
from src.database import Database
from src.database.queries.wait_metrics import slot_accounting, wait_metrics_snapshot
from src.database.queries.wait_queries import wait_graph
from src.database.tables import (
    admission_reservations,
    continuations,
    events,
    messages,
    wait_measurements,
    wait_records,
)
from src.metrics.sampler import aggregate_samples
from src.metrics.waits import with_wait_policy
from src.models import Project, SessionRecord, Task
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0


@pytest.fixture
async def db():
    database = Database(lease_dsn("wait-metrics"))
    await database.initialize()
    await database.create_project(Project(id="p", name="P"))
    await database.create_project(Project(id="q", name="Q"))
    yield database
    await database.close()


async def add_wait(conn, key, *, project="p", created=None, **extra):
    await conn.execute(
        insert(wait_records)
        .values(
            id=key,
            project_id=project,
            owner_kind="session",
            owner_id=f"session-{key}",
            origin="agent",
            session_id=f"session-{key}",
            condition_kind="timer",
            condition={"kind": "timer", "due_at": NOW + 3600},
            source_key=f"timer:{key}",
            source_generation=1,
            source_watermark_seq=0,
            verification_state="verified",
            evidence_ref="test",
            verified_at=NOW - 3600,
            created_at=NOW - 3600 if created is None else created,
            sleep_decision="in_place",
            decision_inputs={},
            request_hash=key,
        )
        .values(**extra)
    )


async def add_continuation(conn, key, *, stopped=None, **extra):
    await conn.execute(
        insert(continuations)
        .values(
            id=key,
            wait_id=key,
            task_id=key,
            suspension_generation=1,
            facts={},
            branch="aq/task",
            prior_status="IN_PROGRESS",
            held_cursor_seq=0,
            resume_state="sleeping",
            stopped_at=stopped,
        )
        .values(**extra)
    )


async def test_pre_migration_is_null_and_empty_migrated_group_has_honest_gaps(db):
    sample = await db.metrics_waits_snapshot(NOW)
    assert sample["pending_in_place"] == sample["wakes_1h"]["other"] == 0
    assert sample["released_slot_seconds_1h"] == {"condition": 0, "capacity": 0}
    assert sample["resume_cost_p50"] is sample["decision_seconds_p50"] is None
    assert sample["human_age_max_seconds"] is sample["tokens_saved_counterfactual"] is None
    assert MetricsSample(ts=NOW, waits=sample).waits.resume_cost_p95 is None
    async with db._engine.begin() as conn:
        # Rename in a rolled-back transaction; no operator schema is involved.
        await conn.execute(text("ALTER TABLE wait_records RENAME TO test_wait_records"))
        assert await wait_metrics_snapshot(conn, now=NOW) is None
        await conn.rollback()


async def test_slot_accounting_clips_and_splits_without_duplicate_join_totals(db):
    for key in ("evicted", "released", "uncertain", "ongoing"):
        await db.create_task(Task(id=key, project_id="p", title="Task", description=""))
    async with db._engine.begin() as conn:
        await add_wait(conn, "held", created=NOW - 4000)
        await add_wait(conn, "done", created=NOW - 1800, outcome="satisfied", resolved_at=NOW - 600)
        await add_wait(
            conn,
            "evicted",
            created=NOW - 1800,
            sleep_decision="hibernate",
            decision_reason="evicted",
            decision_inputs={"suspension_requested_at": NOW - 1300},
        )
        await add_continuation(conn, "evicted", stopped=NOW - 1200)
        await add_wait(conn, "released", created=NOW - 7200, sleep_decision="hibernate")
        await add_continuation(conn, "released", stopped=NOW - 5000)
        await add_wait(conn, "uncertain", sleep_decision="hibernate")
        await add_continuation(conn, "uncertain")
        await add_wait(conn, "ongoing", created=NOW - 110000, sleep_decision="hibernate")
        await add_continuation(conn, "ongoing", stopped=NOW - 100000)
        await conn.execute(
            insert(admission_reservations).values(
                id="pending",
                project_id="p",
                continuation_id="ongoing",
                scope={},
                effective_priority=1,
                provenance="resume",
                deadline_at=NOW - 2000,
                state="pending",
                created_at=NOW - 4000,
            )
        )
        for key, requested, granted, state in (
            ("first", NOW - 2000, NOW - 1000, "consumed"),
            ("retry", NOW - 500, None, "cancelled"),
        ):
            await conn.execute(
                insert(admission_reservations).values(
                    id=key,
                    project_id="p",
                    continuation_id="released",
                    scope={},
                    effective_priority=1,
                    provenance="resume",
                    deadline_at=NOW,
                    state=state,
                    created_at=requested,
                    granted_at=granted,
                )
            )
        result = await wait_metrics_snapshot(conn, now=NOW, project_id="p")
        assert result["in_place_slot_seconds_1h"] == 3600 + 1200 + 600
        assert result["released_slot_seconds_1h"] == {"condition": 1600 + 1200, "capacity": 4600}
        assert result["released_slot_seconds_24h"] == {
            "condition": 3000 + 1200 + 82400,
            "capacity": 5000,
        }
        assert await slot_accounting(conn, now=NOW, project_id="q") == {
            "in_place_slot_seconds_1h": 0,
            "released_slot_seconds_1h": {"condition": 0, "capacity": 0},
        }
        graph = await wait_graph(conn, project_id="p", now=NOW, costs=True)
        assert graph["projects"][0]["costs"]["metrics"] == result


async def test_measurement_cohorts_percentiles_defaults_and_counterfactual(db):
    async with db._engine.begin() as conn:
        for key, seconds in (("low", 60), ("high", 180)):
            await add_wait(conn, key, created=NOW - 1200, outcome="satisfied", resolved_at=NOW)
            await conn.execute(
                insert(wait_measurements).values(
                    id=key,
                    wait_id=key,
                    project_id="p",
                    condition_kind="timer",
                    source_class="timer",
                    decision="hibernate",
                    waited_seconds=seconds * 10,
                    hibernated=True,
                    evicted=False,
                    summary_tokens=10,
                    resume_tokens=20,
                    teardown_seconds=seconds / 3,
                    setup_seconds=seconds / 3,
                    rebuild_seconds=seconds / 3,
                    recorded_at=NOW,
                )
            )
        await add_wait(conn, "foreign", project="q")
        await conn.execute(
            insert(wait_measurements).values(
                id="foreign",
                wait_id="foreign",
                project_id="q",
                condition_kind="timer",
                source_class="timer",
                decision="hibernate",
                waited_seconds=9999,
                hibernated=True,
                evicted=False,
                recorded_at=NOW,
            )
        )
    raw = await db.metrics_waits_snapshot(NOW)
    assert raw["resume_cost_p50"] == 120
    assert raw["resume_cost_p95"] == pytest.approx(174)
    assert raw["by_project"]["q"]["resume_cost_p50"] is None
    policy = parse_wait_policy(
        {
            "projects": {"p": {"resume_cost_minutes": 7}},
            "metrics": {
                "pre_rollout_turn_rate": 2,
                "pre_rollout_tokens_per_turn": 100,
                "cohort": "workers-before-rollout",
                "window": "2026-10-01/2026-10-07",
            },
        }
    )
    assert not policy.validate()
    observed = with_wait_policy(raw, policy)
    assert observed["by_project"]["p"]["resume_cost_minutes"] == 7
    assert observed["by_project"]["p"]["durations"] == [
        {
            "condition_kind": "timer",
            "source_class": "timer",
            "wait_duration_p50": 1200,
            "expected_duration_seconds": None,
        }
    ]
    # Incomplete overhead is unknown, even with a configured baseline.
    assert observed["tokens_saved_counterfactual"]["tokens"] is None
    async with db._engine.begin() as conn:
        await conn.execute(
            update(wait_measurements)
            .where(wait_measurements.c.id == "foreign")
            .values(summary_tokens=10, resume_tokens=20)
        )
    observed = with_wait_policy(await db.metrics_waits_snapshot(NOW), policy)
    assert observed["tokens_saved_counterfactual"] == {
        "tokens": 2 * (1200 + 1200 + 3600) / 60 * 100 - 90,
        "cohort": "workers-before-rollout",
        "window": "2026-10-01/2026-10-07",
    }


async def test_pending_reasons_human_age_and_wakes_only_count_observed_deliveries(db):
    await db.create_session(
        SessionRecord(
            id="session-held",
            project_id="p",
            profile_id="worker-codex",
            name="held",
            harness="codex",
            provider="openai",
            lifecycle="pool",
            state="running",
            work_dir="/tmp/metrics",
            epoch="test",
            instance_token="test",
            started_at=NOW - 7200,
        )
    )
    async with db._engine.begin() as conn:
        await add_wait(conn, "held")
        await add_wait(conn, "human", condition_kind="human", sleep_decision="hibernate")
        await add_wait(conn, "resource", origin="scheduler", source_key="resource:workspace:p")
        for key, outcome, inputs in (
            ("outcome", "satisfied", {}),
            ("decision", "cancelled", {"supervisor_decision": {"decision": "wake"}}),
            ("lift", "lifted_by_supervisor", {}),
        ):
            await add_wait(
                conn,
                key,
                outcome=outcome,
                decision_inputs=inputs,
                resolved_at=NOW - 100,
                wake_delivered_at=NOW - 50,
            )
        await add_wait(conn, "queued", outcome="satisfied", resolved_at=NOW - 100)
        await conn.execute(
            insert(events).values(
                event_type="wait.loop_detected",
                project_id="p",
                timestamp=NOW - 1,
                payload=json.dumps({"wait_id": "held"}),
            )
        )
    sample = await db.metrics_waits_snapshot(NOW)
    assert sample["pending_in_place"] == 2
    assert sample["pending_hibernated"] == sample["pending_human"] == sample["human_pending"] == 1
    assert sample["human_age_max_seconds"] == 3600
    assert sample["scheduler_by_reason"] == {"resource:workspace:p": 1}
    assert sample["loops_1h"] == 1
    assert sample["wakes_1h"] == {
        "outcome": 1,
        "decision": 1,
        "lift": 1,
        "launch_failed": 0,
        "other": 0,
    }
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(messages).values(
                id="unexpected",
                project_id="p",
                from_kind="system",
                from_id="test",
                to_kind="session",
                to_id="held",
                body="Unexpected wake",
                created_at=NOW - 100,
                delivered_at=NOW - 50,
            )
        )
    assert (await db.metrics_waits_snapshot(NOW))["wakes_1h"]["other"] == 1


def test_wait_rollups_keep_nulls_and_average_overlapping_trailing_windows():
    assert aggregate_samples([{"waits": None}, {"waits": None}]) == {"waits": None}
    observed = aggregate_samples(
        [
            {
                "waits": {
                    "pending_in_place": count,
                    "loops_1h": 1,
                    "in_place_slot_seconds_1h": 100,
                    "released_slot_seconds_1h": {"condition": 100, "capacity": None},
                    "resume_cost_p50": None,
                }
            }
            for count in (2, 3)
        ]
    )
    parsed = MetricsSample(ts=NOW, **observed).waits
    assert parsed.pending_in_place == 2.5
    assert parsed.loops_1h == 1
    assert parsed.in_place_slot_seconds_1h == 100
    assert parsed.released_slot_seconds_1h.condition == 100
    assert parsed.released_slot_seconds_1h.capacity is parsed.resume_cost_p50 is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("pre_rollout_turn_rate", -1),
        ("pre_rollout_tokens_per_turn", float("nan")),
        ("cohort", ""),
        ("window", 1),
    ],
)
def test_counterfactual_config_validation(field, value):
    assert parse_wait_policy({"metrics": {field: value}}).validate()
