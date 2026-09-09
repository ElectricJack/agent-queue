"""Response models for ``GET /api/providers/usage``.

A snapshot is one observation of one provider limit window --- ``(provider,
window, scope) -> used_percent, resets_at``.  The endpoint's whole job is to
hand the newest one per series to a client that must not have to reason about
freshness itself, so ``stale`` is a field here rather than a rule each client
re-derives: the dashboard mutes a bar on it, the doctor check WARNs on it, and
two horizons that drift apart would make those two disagree about the same
card.

``last_seen_at`` is the field freshness is measured from, not ``observed_at``.
The writer drops a reading identical to the newest row already stored, so a
Claude week window sitting at 81% all afternoon has an ``observed_at`` hours
old while the probe succeeds every ten minutes; measuring from ``observed_at``
would report a healthy account as stale (spec amendment A3).
"""

from __future__ import annotations

from pydantic import BaseModel


class ProviderUsageSnapshot(BaseModel):
    """One observation of one limit window.

    ``resets_at`` is nullable because a percentage without a clock still beats
    no reading at all --- the providers do not always print a reset clause.
    """

    id: int
    provider: str
    account_label: str = ""
    window: str
    scope: str = ""
    used_percent: float
    resets_at: float | None = None
    observed_at: float
    #: When this value was last *confirmed*, which for an unchanged reading is
    #: later than ``observed_at``.  Falls back to ``observed_at`` on a row
    #: written before the column existed.
    last_seen_at: float
    source: str
    #: Server-computed: ``now - last_seen_at`` exceeded this series' horizon.
    stale: bool = False
    #: Seconds since the value was last confirmed, at the instant of the read.
    age_seconds: float = 0.0


class ProviderUsageResponse(BaseModel):
    """``GET /api/providers/usage``.

    ``snapshots`` is the newest reading per series --- what the cards render.
    ``series`` is keyed ``"<provider>/<window>/<scope>"`` and is populated only
    when ``?since=`` is given, so the default response stays one row per card.
    ``now`` is the server clock the staleness verdicts were computed against;
    without it a client cannot tell a stale reading from a skewed clock.
    """

    now: float
    snapshots: list[ProviderUsageSnapshot] = []
    series: dict[str, list[ProviderUsageSnapshot]] = {}
