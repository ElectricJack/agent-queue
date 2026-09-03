"""Response models for the fleet metrics series endpoint.

The shapes mirror :mod:`src.metrics.sampler` exactly.  They are spelled out
rather than typed as a free ``dict`` so the OpenAPI document — and every
client generated from it — describes what a sample actually contains.

Every field is optional with a neutral default: a sample is deliberately
partial when its source is unavailable (a host with no ``/proc/meminfo``, a
tier that has not had a slow tick yet), and a strict model would turn an
honest gap into a 500.
"""

from __future__ import annotations

from pydantic import BaseModel


class AgentMetrics(BaseModel):
    """Live sessions, split the three ways the tab graphs them."""

    total: int = 0
    by_state: dict[str, float] = {}
    by_harness: dict[str, float] = {}
    by_profile: dict[str, float] = {}
    by_lifecycle: dict[str, float] = {}


class SessionSubagents(BaseModel):
    """One session's children.

    ``native`` and ``aq`` are children open right now, so they are 0 for a
    session that has already exited; ``spawned`` counts the starts it
    recorded inside the sampler's window and outlives the session row, which
    is why an entry can appear here keyed by a session id rather than a name.

    ``hooks`` is False when the session was launched without its harness
    sub-agent hooks wired, which makes ``native`` a floor rather than a total.
    """

    native: float = 0
    aq: float = 0
    spawned: float = 0
    hooks: bool = True


class SubagentMetrics(BaseModel):
    """Fleet sub-agent totals plus the per-session drill-down.

    Two different questions, kept apart because they answer differently on a
    pool fleet: ``active`` (== the older ``total``) is how many children are
    open at this instant, and reads ~0 when the sessions that start children
    are shorter-lived than the children; ``spawned_per_hour`` is how many
    were started over the sampler's window, counted from the event table
    regardless of whether the parent session still exists.

    ``complete`` is the conjunction over live sessions: one session without
    hooks makes ``native`` and ``active`` lower bounds for the whole fleet.
    """

    total: float = 0
    active: float = 0
    native: float = 0
    aq: float = 0
    spawned_per_hour: float = 0
    complete: bool = True
    by_session: dict[str, SessionSubagents] = {}


class ModelTokens(BaseModel):
    """One model's share of the window, scaled to per minute.

    ``total_per_min`` is that model's whole ledger volume — the other four
    fields are its breakdown, and cache is usually most of it.
    """

    input_per_min: float = 0
    output_per_min: float = 0
    cache_read_per_min: float = 0
    cache_write_per_min: float = 0
    total_per_min: float = 0


class TokenMetrics(BaseModel):
    """Ledger rates over ``window_seconds``, scaled to per minute.

    ``total_per_min`` is everything the ledger recorded, cache included: on a
    long-lived session cache reads are the overwhelming majority of the
    traffic, so a "total" of input+output alone understates it by orders of
    magnitude.  ``unattributed_per_min`` is what no column could account for
    — rows from writers that report only a total, or written before the cache
    columns existed — reported separately rather than folded into a model's
    rate, the same honesty rule ``get_costs`` applies to pricing.

    The ``*_per_min_1m`` fields are the raw trailing-minute counts, kept
    beside the smoothed rates so the unsmoothed flush pattern is still
    readable.
    """

    input_per_min: float = 0
    output_per_min: float = 0
    cache_read_per_min: float = 0
    cache_write_per_min: float = 0
    total_per_min: float = 0
    unattributed_per_min: float = 0
    input_per_min_1m: float = 0
    output_per_min_1m: float = 0
    total_per_min_1m: float = 0
    window_seconds: float = 60
    by_model: dict[str, ModelTokens] = {}


class TaskMetrics(BaseModel):
    READY: float = 0
    IN_PROGRESS: float = 0
    ASSIGNED: float = 0
    PAUSED: float = 0
    BLOCKED: float = 0
    WAITING_INPUT: float = 0
    other: float = 0
    total: float = 0


class SlotMetrics(BaseModel):
    """Worktree slots.  ``cap`` is null when worktree execution is off."""

    used: float = 0
    total: float = 0
    cap: float | None = None


class MachineMetrics(BaseModel):
    """Nulls mean the platform does not expose the value, not zero."""

    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    cpu_count: float | None = None
    mem_total_mb: float | None = None
    mem_free_mb: float | None = None
    mem_available_mb: float | None = None


class DaemonMetrics(BaseModel):
    uptime_seconds: float = 0
    restarts: float = 0


class StallMetrics(BaseModel):
    """Stall-ladder activity in the trailing hour.

    Sourced from bus events the reconciler does not persist, so both counters
    restart with the daemon — read them next to ``daemon.uptime_seconds``.
    """

    nudges_per_hour: float = 0
    kills_per_hour: float = 0


class ThroughputMetrics(BaseModel):
    completions_per_hour: float = 0
    prs_per_hour: float = 0


class SamplerMetrics(BaseModel):
    """The sampler's own per-tick cost, so its overhead is observable."""

    collect_ms: float = 0


class MetricsSample(BaseModel):
    """One point on every series.

    ``ts`` is the bucket start, not the collection instant, so points line up
    exactly across resolutions.
    """

    ts: float
    agents: AgentMetrics = AgentMetrics()
    tasks: TaskMetrics = TaskMetrics()
    subagents: SubagentMetrics = SubagentMetrics()
    tokens: TokenMetrics = TokenMetrics()
    slots: SlotMetrics = SlotMetrics()
    machine: MachineMetrics = MachineMetrics()
    daemon: DaemonMetrics = DaemonMetrics()
    stall: StallMetrics = StallMetrics()
    throughput: ThroughputMetrics = ThroughputMetrics()
    merges_per_hour: float = 0
    sampler: SamplerMetrics = SamplerMetrics()


class MetricsSeriesResponse(BaseModel):
    """``GET /api/metrics/series``.

    ``step`` is the resolution actually served, which may be coarser than
    the one requested when ``step=auto`` or when the requested span would
    exceed ``max_points``.
    """

    step: str
    from_ts: float
    to_ts: float
    truncated: bool = False
    samples: list[MetricsSample] = []
