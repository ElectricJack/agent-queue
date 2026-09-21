"""Response models for ``/api/providers/*`` -- usage and availability.

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

from pydantic import BaseModel, Field


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


# ---------------------------------------------------------------------------
# Provider availability (docs/specs/provider-failover.md D7, D20)
# ---------------------------------------------------------------------------


class ProviderOverride(BaseModel):
    """An operator override (D6).  ``until`` is ``None`` only for ``disabled``."""

    state: str
    until: float | None = None
    by: str | None = None
    reason: str | None = None
    set_at: float | None = None


class ProviderUsageReading(BaseModel):
    """The newest fresh account-wide usage window (the fullest one)."""

    window: str
    scope: str = ""
    used_percent: float
    resets_at: float | None = None
    observed_at: float


class ProviderTransition(BaseModel):
    """One change of effective state, from the audit trail."""

    id: int | None = None
    provider: str
    from_state: str
    to_state: str
    reason_code: str = ""
    reason: str = ""
    until: float | None = None
    generation: int
    actor: str = "system"
    detail: dict = {}
    at: float


class ProviderAvailabilityStatus(BaseModel):
    """One provider's availability, as the CLI, API, doctor and cards show it.

    Every field is server-derived; the dashboard never recomputes a state.
    ``state`` is the *effective* state (the override while one is active);
    ``derived_*`` is what the evidence alone says.  ``until`` is the expected
    recovery -- the provider's own reset clock or a backoff deadline -- and
    ``None`` when nothing is known (``unauthenticated`` needs a human).
    """

    provider: str
    vendor: str = ""
    state: str
    half: str
    reason_code: str = ""
    reason: str = ""
    since: float | None = None
    until: float | None = None
    derived_state: str = ""
    derived_reason: str = ""
    derived_until: float | None = None
    override: ProviderOverride | None = None
    held: int = 0
    level: int = 0
    generation: int = 0
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    last_probe_at: float | None = None
    probation: bool = False
    remediation: str = ""
    mode: str = "enforce"
    usage: ProviderUsageReading | None = None
    #: Only with ``verbose``: the evidence ring, newest first.
    evidence: list[dict] = []
    #: Only with ``verbose``: the last ten transitions.
    transitions: list[ProviderTransition] = []
    updated_at: float | None = None


class ProviderStatusResponse(BaseModel):
    """``provider_status`` and ``GET /api/providers/availability``."""

    success: bool = True
    mode: str = "enforce"
    now: float
    providers: list[ProviderAvailabilityStatus] = []


class ProviderHistoryResponse(BaseModel):
    success: bool = True
    provider: str
    transitions: list[ProviderTransition] = []


class ProviderRecheckResponse(BaseModel):
    """``provider_recheck``: the probe's answer and the resulting state.

    ``probe`` is ``authenticated``, ``not_authenticated``, ``cannot_tell`` or
    ``not_probeable`` (the provider has no login probe).
    """

    success: bool = True
    provider: str
    probe: str
    probe_detail: dict = {}
    state: str
    transition: dict | None = None
    status: ProviderAvailabilityStatus | None = None


class ProviderSetStateResponse(BaseModel):
    success: bool = True
    provider: str
    state: str
    transition: dict | None = None
    status: ProviderAvailabilityStatus | None = None


class ProviderStateRequest(BaseModel):
    """``POST /api/providers/{provider}/state`` (D6).

    ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
    ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
    ``until`` and ``no_expiry``.
    """

    model_config = {"populate_by_name": True}

    state: str
    reason: str | None = None
    for_: str | None = Field(default=None, alias="for")
    until: str | None = None
    no_expiry: bool | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "provider_status": ProviderStatusResponse,
    "provider_history": ProviderHistoryResponse,
    "provider_recheck": ProviderRecheckResponse,
    "provider_set_state": ProviderSetStateResponse,
}
