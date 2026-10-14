"""Delivery age and progress observations for the train policy.

These functions neither wake agents nor mutate delivery. Recovery decisions
belong to the reviewed policy; Git containment and canonical wait outcomes
remain the authority. A watchdog projection must never count its own blocker
row as evidence that a delivery producer is progressing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select

from src.database.tables import wait_measurements, wait_records

SAMPLE_WINDOW_SECONDS = 7 * 24 * 3600
THRESHOLD_REFRESH_SECONDS = 24 * 3600


@dataclass(frozen=True)
class DeliveryThreshold:
    stuck_after_seconds: float
    min_seconds: float
    ci_multiplier: float
    p90_batch_ci_seconds: float | None
    sample_count: int
    measured_at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def refresh_due(self, now: float) -> bool:
        return now < self.measured_at or now >= self.measured_at + THRESHOLD_REFRESH_SECONDS


def derive_threshold(
    durations: Iterable[float], *, now: float, min_seconds: float = 1800,
    ci_multiplier: float = 2,
) -> DeliveryThreshold:
    """Continuous p90, matching PostgreSQL percentile_cont, with a no-data floor."""
    for value in (now, min_seconds, ci_multiplier):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("threshold inputs must be finite numbers")
    if min_seconds <= 0 or ci_multiplier <= 0:
        raise ValueError("watchdog floor and CI multiplier must be positive")
    samples = sorted(float(v) for v in durations
                     if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    p90 = None
    if samples:
        position = (len(samples) - 1) * 0.9
        lower, upper = math.floor(position), math.ceil(position)
        p90 = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
    return DeliveryThreshold(
        max(min_seconds, ci_multiplier * p90) if p90 is not None else min_seconds,
        min_seconds, ci_multiplier, p90, len(samples), now,
    )


async def measured_threshold_on(
    conn, project_id: str, *, now: float, min_seconds: float = 1800,
    ci_multiplier: float = 2,
) -> DeliveryThreshold:
    """Use only this project's trailing-week integration CI measurements.

    Origin belongs to the canonical wait, not to its source_class label. Agent
    job waits, other projects and future-dated samples cannot raise this SLO.
    The caller retains the returned inputs and recomputes them once per day.
    """
    durations = (await conn.execute(select(wait_measurements.c.waited_seconds).join(
        wait_records, wait_records.c.id == wait_measurements.c.wait_id,
    ).where(
        wait_measurements.c.project_id == project_id,
        wait_records.c.project_id == project_id,
        wait_records.c.origin == "integration",
        wait_measurements.c.condition_kind.in_(("ci_run", "job")),
        wait_records.c.condition_kind == wait_measurements.c.condition_kind,
        wait_measurements.c.recorded_at >= now - SAMPLE_WINDOW_SECONDS,
        wait_measurements.c.recorded_at <= now,
    ))).scalars()
    return derive_threshold(
        durations, now=now, min_seconds=min_seconds, ci_multiplier=ci_multiplier,
    )


@dataclass(frozen=True)
class DeliveryProgress:
    state: str
    reason: str
    age_seconds: float
    wait_id: str | None


def classify_delivery_progress(
    *, completed_at: float, now: float, threshold: DeliveryThreshold,
    wait: Mapping[str, Any] | None, escalation: Mapping[str, Any] | None = None,
    decision_budget_seconds: float,
) -> DeliveryProgress:
    """Classify an exactly bound producer wait without treating age as failure.

    The reader must first bind the wait to this repository, completion generation
    and target. In particular a live wait for another batch is not evidence.
    Completion age is immutable; neither wait creation nor a retry resets it.
    The shared wait subsystem owns deadline firing and supervisor decisions.
    """
    if not math.isfinite(decision_budget_seconds) or decision_budget_seconds <= 0:
        raise ValueError("a positive supervisor decision budget is required")
    age = max(0, now - completed_at)
    wait_id = wait["id"] if wait else None

    def result(state, reason):
        return DeliveryProgress(state, reason, age, wait_id)

    if wait is None:
        return result("stuck", "publisher_unavailable")
    inputs = wait.get("decision_inputs") or {}
    if inputs.get("delivery_watchdog_projection"):
        return result("stuck", inputs.get("delivery_reason", "publisher_unavailable"))
    if wait.get("origin") != "integration":
        return result("stuck", "publisher_unavailable")
    if wait.get("outcome") == "source_unavailable":
        return result("stuck", "target_unavailable")
    if wait.get("outcome") != "pending":
        return result("stuck", "policy_reobserve_due")
    if wait.get("verification_state") != "verified":
        return result("unknown", "delivery_observation_unavailable")
    if escalation and escalation.get("decided_at") is None:
        if now >= escalation["raised_at"] + decision_budget_seconds:
            return result("stuck", "awaiting_supervisor_decision")
        return result("progressing", "awaiting_supervisor_decision")
    deadline = wait.get("escalate_at")
    if deadline is None or deadline <= now:
        # An expired deadline without its committed escalation is incomplete
        # observation, not authority to wake or merge anything.
        return result("unknown", "delivery_observation_unavailable")
    return result("slow" if age >= threshold.stuck_after_seconds else "progressing",
                  inputs.get("delivery_reason", "policy_reobserve_due"))
