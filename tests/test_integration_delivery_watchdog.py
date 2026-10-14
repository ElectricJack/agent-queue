"""Threshold and producer progress evidence for the delivery watchdog."""

from dataclasses import replace

import pytest
from sqlalchemy import insert

from src.database import Database
from src.database.tables import wait_measurements, wait_records
from src.integration.delivery_watchdog import (
    classify_delivery_progress,
    derive_threshold,
    measured_threshold_on,
)
from src.models import Project
from tests.db_fixtures import lease_dsn


@pytest.fixture
def clock():
    return [1_800_000_000.0]


def test_threshold_uses_floor_without_measurements(clock):
    threshold = derive_threshold([], now=clock[0])
    assert threshold.stuck_after_seconds == 1800
    assert threshold.p90_batch_ci_seconds is None
    assert threshold.sample_count == 0


def test_threshold_follows_long_project_ci(clock):
    threshold = derive_threshold([2400] * 10, now=clock[0])
    assert threshold.stuck_after_seconds == 4800
    assert threshold.p90_batch_ci_seconds == 2400
    assert threshold.sample_count == 10


def test_threshold_continuous_percentile_and_project_overrides(clock):
    threshold = derive_threshold([100, 200, 300], now=clock[0],
                                 min_seconds=60, ci_multiplier=3)
    assert threshold.p90_batch_ci_seconds == 280
    assert threshold.stuck_after_seconds == 840


def test_invalid_measurements_cannot_poison_threshold(clock):
    threshold = derive_threshold([True, -1, float("nan"), float("inf"), 0, 10], now=clock[0])
    assert threshold.sample_count == 2
    assert threshold.stuck_after_seconds == 1800


@pytest.mark.parametrize("options", [{"min_seconds": 0}, {"ci_multiplier": -1},
                                     {"min_seconds": True}, {"ci_multiplier": float("nan")}])
def test_invalid_threshold_configuration_refused(clock, options):
    with pytest.raises(ValueError):
        derive_threshold([], now=clock[0], **options)


def test_threshold_daily_refresh_boundary_and_clock_reversal(clock):
    threshold = derive_threshold([], now=clock[0])
    assert not threshold.refresh_due(clock[0] + 86399)
    assert threshold.refresh_due(clock[0] + 86400)
    assert threshold.refresh_due(clock[0] - 1)


def progress(clock, wait, *, age=3600, escalation=None):
    return classify_delivery_progress(
        completed_at=clock[0] - age, now=clock[0],
        threshold=derive_threshold([], now=clock[0]), wait=wait, escalation=escalation,
        decision_budget_seconds=300,
    )


@pytest.fixture
def producer(clock):
    return {"id": "ci-wait", "origin": "integration", "outcome": "pending",
            "verification_state": "verified", "escalate_at": clock[0] + 60,
            "decision_inputs": {"delivery_reason": "candidate_ci_pending"}}


def test_progressing_ci_is_slow_and_preserves_first_completion_age(clock, producer):
    result = progress(clock, producer)
    assert (result.state, result.reason, result.age_seconds, result.wait_id) == (
        "slow", "candidate_ci_pending", 3600, "ci-wait",
    )
    assert progress(clock, producer, age=60).state == "progressing"


def test_absent_or_unavailable_producer_is_stuck(clock, producer):
    assert progress(clock, None).reason == "publisher_unavailable"
    result = progress(clock, {**producer, "outcome": "source_unavailable"})
    assert (result.state, result.reason) == ("stuck", "target_unavailable")


def test_agent_wait_does_not_supply_delivery_ownership(clock, producer):
    assert progress(clock, {**producer, "origin": "agent"}).state == "stuck"


def test_watchdog_blocker_cannot_make_itself_progressing(clock, producer):
    producer["decision_inputs"].update(delivery_watchdog_projection=True,
                                      delivery_reason="manual_hold")
    result = progress(clock, producer)
    assert (result.state, result.reason) == ("stuck", "manual_hold")


def test_supervisor_decision_budget_uses_committed_escalation(clock, producer):
    producer["escalate_at"] = clock[0] - 300
    escalation = {"raised_at": clock[0] - 299, "decided_at": None}
    assert progress(clock, producer, escalation=escalation).state == "progressing"
    escalation["raised_at"] -= 1
    result = progress(clock, producer, escalation=escalation)
    assert (result.state, result.reason) == ("stuck", "awaiting_supervisor_decision")


def test_decided_escalation_uses_current_extended_deadline(clock, producer):
    escalation = {"raised_at": clock[0] - 3600, "decided_at": clock[0] - 1}
    assert progress(clock, producer, escalation=escalation).state == "slow"


@pytest.mark.parametrize("change", [{"verification_state": "stale"},
                                     {"escalate_at": None}, {"escalate_at": 0}])
def test_incomplete_observation_is_never_recovery_authority(clock, producer, change):
    result = progress(clock, {**producer, **change})
    assert (result.state, result.reason) == ("unknown", "delivery_observation_unavailable")


def test_slow_threshold_can_vary_without_changing_completion_age(clock, producer):
    threshold = replace(derive_threshold([], now=clock[0]), stuck_after_seconds=4800)
    result = classify_delivery_progress(completed_at=clock[0] - 3600, now=clock[0],
                                       threshold=threshold, wait=producer,
                                       decision_budget_seconds=300)
    assert result.state == "progressing"
    assert result.age_seconds == 3600


async def test_measured_threshold_excludes_foreign_non_ci_agent_old_and_future_samples(clock):
    db = Database(lease_dsn("delivery-watchdog"))
    await db.initialize()
    try:
        for project_id in ("p", "q"):
            await db.create_project(Project(id=project_id, name=project_id))
        samples = [
            ("p", "integration", "ci_run", 2400, 0),
            ("p", "integration", "job", 2400, -7 * 86400),
            ("p", "agent", "job", 100000, 0),
            ("q", "integration", "ci_run", 100000, 0),
            ("p", "integration", "timer", 100000, 0),
            ("p", "integration", "ci_run", 100000, -7 * 86400 - 1),
            ("p", "integration", "job", 100000, 1),
        ]
        async with db.immediate() as conn:
            for index, (project_id, origin, kind, duration, offset) in enumerate(samples):
                wait_id = f"measurement-{index}"
                await conn.execute(insert(wait_records).values(
                    id=wait_id, project_id=project_id, owner_kind="integration",
                    owner_id=wait_id, origin=origin, condition_kind=kind,
                    condition={"kind": kind}, source_key=wait_id, source_generation=0,
                    source_watermark_seq=0, verification_state="verified", evidence_ref=wait_id,
                    verified_at=clock[0], created_at=clock[0] - duration,
                    outcome="satisfied", resolved_at=clock[0], request_hash=wait_id,
                ))
                await conn.execute(insert(wait_measurements).values(
                    id=wait_id, project_id=project_id, condition_kind=kind,
                    source_class="fixture", wait_id=wait_id, decision="in_place",
                    waited_seconds=duration, hibernated=False, evicted=False,
                    recorded_at=clock[0] + offset,
                ))
            threshold = await measured_threshold_on(conn, "p", now=clock[0])
        assert threshold.sample_count == 2
        assert threshold.stuck_after_seconds == 4800
    finally:
        await db.close()
