"""Response models for the delivery latency report (spec section 10).

Kept apart from :mod:`src.api.models.metrics`: a report is computed on read
and never rolled up, so its counters stay ``int``.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeliveryLatencyTarget(BaseModel):
    """Section 9.2's per-project threshold, which is also the SLO target.

    ``source`` is ``floor`` until integration CI has been measured.
    """

    project_id: str | None = None
    target_seconds: float | None = None
    min_seconds: float | None = None
    ci_multiplier: float | None = None
    p90_batch_ci_seconds: float | None = None
    batch_ci_samples: int = 0
    source: str | None = None


class DeliveryLatencyExcluded(BaseModel):
    """Completions that are not samples, counted by reason."""

    reason: str
    count: int = 0


class DeliveryLatencySegment(BaseModel):
    """Median of one segment over the cohort's samples; segments overlap."""

    segment: str
    median_seconds: float | None = None
    generations: int = 0


class DeliveryLatencyHistogramBucket(BaseModel):
    bucket: str
    count: int = 0


class DeliveryLatencyBatchDay(BaseModel):
    """Integration batches created on one UTC day (``day`` is its epoch start)."""

    day: float
    batches: int = 0
    members: int = 0
    red_rounds: int = 0


class DeliveryLatencyWait(BaseModel):
    """A wait record linked to a generation (its task, batch or repair).

    ``segment`` names the breakdown segment an ``integration`` wait counts
    toward; ``ci_url`` is the exact CI run, set only for an http(s) result.
    """

    wait_id: str
    origin: str
    condition_kind: str
    segment: str | None = None
    outcome: str
    created_at: float
    resolved_at: float | None = None
    evidence_ref: str | None = None
    result_ref: str | None = None
    decision_reason: str | None = None
    ci_url: str | None = None


class DeliveryLatencyGeneration(BaseModel):
    """One completed generation in drill-down.

    ``status`` is ``sample``, ``pending``, ``unknown``, ``historical_unknown``,
    ``left_censored`` or ``excluded``. ``current_blocker`` marks a sample whose
    dev ref no longer contains it (a rewrite after the timing was taken).
    ``observed_at``, ``reason`` and the OIDs come from the latest recorded
    containment transition (on dev when there is one); transitions are written
    only when the state changes, so ``observed_at`` is not a freshness probe.
    """

    task_id: str
    generation_id: str
    project_id: str | None = None
    status: str
    exclusion: str | None = None
    completed_at: float | None = None
    anchor_at: float | None = None
    anchor_kind: str | None = None
    on_dev_at: float | None = None
    parent_at: float | None = None
    parent_ref: str | None = None
    dev_ref: str | None = None
    source_ref: str | None = None
    source_oid: str | None = None
    target_oid: str | None = None
    observed_at: float | None = None
    reason: str | None = None
    latest_state: str | None = None
    approval_to_dev_seconds: float | None = None
    completion_to_dev_seconds: float | None = None
    age_seconds: float | None = None
    watchdog: str | None = None
    human_wait_seconds: float | None = None
    segments: dict[str, float] = {}
    residual_seconds: float | None = None
    rebuild_seconds: float | None = None
    setup_seconds: float | None = None
    cache_hit_rate: float | None = None
    clock_skew: bool = False
    late: bool = False
    current_blocker: bool = False
    waits: list[DeliveryLatencyWait] = []


class DeliveryLatencyResponse(BaseModel):
    """``GET /api/metrics/delivery-latency`` — one completion cohort.

    The headline (``metric``) is approval to first sight on dev, minus linked
    human-wait time; the completion-to-dev figures sit beside it. Percentiles
    cover delivered samples only (``percentiles_exclude_pending``), so read
    them with the pending, unknown and excluded counts. Null means no data,
    never zero. ``cache_hit_rate`` is derived from ``rebuild_seconds == 0``.
    No field declares the SLO met.
    """

    observed_at: float
    window: str
    since: float
    until: float
    project_id: str | None = None
    metric: str = "approval_to_dev"
    percentiles_exclude_pending: bool = True
    median_seconds: float | None = None
    p90_seconds: float | None = None
    completion_to_dev_median_seconds: float | None = None
    completion_to_dev_p90_seconds: float | None = None
    sample_count: int = 0
    eligible_count: int = 0
    pending_count: int = 0
    unknown_count: int = 0
    historical_unknown_count: int = 0
    left_censored_count: int = 0
    slow_count: int = 0
    stuck_count: int = 0
    late_sample_count: int = 0
    clock_skew_count: int = 0
    current_blocker_count: int = 0
    excluded: list[DeliveryLatencyExcluded] = []
    oldest_pending_age_seconds: float | None = None
    within_target_fraction: float | None = None
    coverage: float | None = None
    unknown_fraction: float | None = None
    target: DeliveryLatencyTarget | None = None
    targets: list[DeliveryLatencyTarget] = []
    segments: list[DeliveryLatencySegment] = []
    residual_median_seconds: float | None = None
    rebuild_seconds_total: float | None = None
    cache_hit_rate: float | None = None
    cache_hit_rate_derived: bool = True
    parent_delay_median_seconds: float | None = None
    author_merge_median_seconds: float | None = None
    historical_age_histogram: list[DeliveryLatencyHistogramBucket] = []
    batches_per_day: list[DeliveryLatencyBatchDay] = []
    generations: list[DeliveryLatencyGeneration] = []
