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

from typing import Any

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
    #: Tasks the current outage's batch moved off this provider and not
    #: undone (provider-failover D20); ``batch_id`` names that batch.
    rerouted: int = 0
    batch_id: str | None = None
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


class ProviderHeldTask(BaseModel):
    """One queued task an unavailable provider is holding (D18, D20).

    The task's own identity plus the derived hold ``aq task explain``
    reports: ``kind`` says why it is not moving, ``ahead`` its place in the
    failover trickle for ``awaiting_failover_capacity``.
    """

    task_id: str
    project_id: str
    title: str = ""
    status: str = ""
    priority: int = 100
    provider: str
    vendor: str = ""
    state: str
    since: float | None = None
    until: float | None = None
    kind: str
    ahead: int | None = None
    detail: str = ""
    profile_id: str | None = None
    reason: str = ""
    remediation: str = ""


class ProviderHeldTasksResponse(BaseModel):
    """``provider_held_tasks``: every held task, and how many per ``kind``."""

    success: bool = True
    now: float
    tasks: list[ProviderHeldTask] = []
    total: int = 0
    by_kind: dict[str, int] = {}


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


class RerouteDecision(BaseModel):
    """What one re-route sweep did (or would do) with one task (D12-D15).

    ``action`` is ``move``, ``hold`` or ``skip``; ``kind`` names why a held
    task is not moving (``provider_pinned``, ``no_equivalent_rung``,
    ``awaiting_failover_capacity`` with ``ahead``, the capacity spill kinds
    ``spill_no_target`` / ``spill_pinned`` / ..., ...).
    """

    task_id: str
    project_id: str
    from_profile_id: str
    from_provider: str = ""
    provider_state: str = ""
    action: str
    kind: str | None = None
    to_profile_id: str | None = None
    to_provider: str | None = None
    to_class: str | None = None
    ahead: int | None = None
    detail: str = ""
    resume: bool = False
    intelligence_class: str | None = None
    intent: str = "class_only"
    priority: int = 100
    title: str = ""
    status: str = ""
    provider_generation: int | None = None
    #: Which pass decided it: ``provider_unavailable`` (failover),
    #: ``operator_forced`` or ``capacity_spill`` (D24).
    reason_code: str | None = None


class ProviderRerouteResponse(BaseModel):
    """``provider_reroute``: one sweep's plan and what it applied (D11).

    ``outcome`` is ``rerouted``, ``held``, ``idle`` or ``disabled``.  With
    ``dry_run`` (or while re-routing is off) ``applied`` is false and ``moved``
    lists what a live sweep would move.
    """

    success: bool = True
    outcome: str
    dry_run: bool = False
    applied: bool = False
    disabled_reason: str | None = None
    unavailable_providers: list[str] = []
    moved: list[RerouteDecision] = []
    held: list[RerouteDecision] = []
    held_by_kind: dict[str, int] = {}
    resumed: list[str] = []
    lost: list[str] = []
    skipped: list[RerouteDecision] = []
    batch_ids: list[str] = []
    notices: list[str] = []


class ProviderRerouteBody(BaseModel):
    """``POST /api/providers/reroute`` (D20)."""

    provider: str | None = None
    task_id: list[str] | None = None
    to_profile: str | None = None
    include_paused: bool | None = None
    dry_run: bool | None = None
    force: bool | None = None


class RerouteUndone(BaseModel):
    task_id: str
    from_profile_id: str | None = None
    to_profile_id: str | None = None
    reroute_id: int | None = None


class RerouteUndoRefusal(BaseModel):
    task_id: str
    reason: str


class ProviderRerouteUndoResponse(BaseModel):
    """``provider_reroute_undo`` (D16): which tasks went back, and which were refused."""

    success: bool = True
    outcome: str
    undone: list[RerouteUndone] = []
    refused: list[RerouteUndoRefusal] = []


class ProviderRerouteUndoBody(BaseModel):
    """``POST /api/providers/reroute/undo`` (D16): a batch or tasks."""

    batch_id: str | None = None
    task_id: list[str] | None = None
    force: bool | None = None


class ProviderAllocationSupply(BaseModel):
    """Supply counters as the pool sizer reads them.

    ``ready`` is pool demand and is ``None`` for a task-lifecycle profile,
    which has live sessions but no pool.
    """

    ready: int | None = 0
    idle: int = 0
    busy: int = 0
    starting: int = 0
    draining: int = 0
    unresponsive: int = 0


class ProviderAllocationProjectSupply(ProviderAllocationSupply):
    """One project's share of a profile's supply."""

    project_id: str | None = None


class ProviderAllocationSession(BaseModel):
    """One live session of an ordinary worker profile."""

    session_id: str
    project_id: str | None = None
    lifecycle: str
    state: str
    #: The supply bucket it counts in: idle, busy, starting, draining, unresponsive.
    activity: str
    agent_id: str | None = None
    task_id: str | None = None
    task_title: str | None = None
    idle_seconds: float | None = None
    started_at: float | None = None


class ProviderAllocationIntent(BaseModel):
    """READY/ASSIGNED/IN_PROGRESS tasks carrying one explicit provider intent.

    ``count`` and ``by_status`` are fleet-wide; ``task_ids`` holds only the
    ones inside the caller's view.
    """

    count: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    task_ids: list[str] = []


class ProviderAllocationHidden(BaseModel):
    """What the caller's view left out of one profile."""

    projects: int = 0
    sessions: int = 0
    tasks: int = 0


class ProviderAllocationProfile(BaseModel):
    """One ordinary worker profile: its bounds, supply, sessions and pins."""

    profile_id: str
    name: str = ""
    harness: str
    lifecycle: str
    enabled: bool = True
    intelligence_class: str = ""
    min_active: int | None = None
    max_active: int | None = None
    min_per_project: int | None = None
    supply: ProviderAllocationSupply
    projects: list[ProviderAllocationProjectSupply] = []
    sessions: list[ProviderAllocationSession] = []
    pinned: ProviderAllocationIntent
    preferred: ProviderAllocationIntent
    hidden: ProviderAllocationHidden


class ProviderAllocationCeiling(BaseModel):
    """The provider-wide configured ceiling over its enabled pool profiles.

    ``max_active`` is ``None`` (and ``unbounded`` true) when any of them is
    unbounded.  Bounds stay per profile; this total is for reading only.
    """

    min_active: int = 0
    max_active: int | None = 0
    unbounded: bool = False
    pool_profiles: int = 0


class ProviderAllocationManualAgent(BaseModel):
    """A durable agent definition no live pool session owns.

    ``harness`` / ``intelligence_class`` / ``model`` are the agent's own
    overrides; ``effective_*`` is what a launch would use.  Allocation never
    rewrites any of them.
    """

    agent_id: str
    name: str
    profile_id: str
    enabled: bool = True
    state: str
    harness: str | None = None
    intelligence_class: str | None = None
    model: str | None = None
    has_overrides: bool = False
    effective_harness: str = ""
    effective_class: str | None = None
    current_task_id: str | None = None
    current_task_title: str | None = None
    current_project_id: str | None = None
    #: True when the current task lies outside the caller's view.
    redacted: bool = False


class ProviderAllocationEvent(BaseModel):
    """The newest ``provider.allocation_changed`` event for a provider."""

    event_id: int | None = None
    at: float | None = None
    request_id: str | None = None
    status: str | None = None
    actor: str | None = None


class ProviderAllocationGroup(BaseModel):
    """One provider: its ordinary worker profiles and what runs on them."""

    provider: str
    vendor: str = ""
    state: str = "available"
    harnesses: list[str] = []
    supply: ProviderAllocationSupply
    ceiling: ProviderAllocationCeiling
    profiles: list[ProviderAllocationProfile] = []
    manual_agents: list[ProviderAllocationManualAgent] = []
    pinned_tasks: int = 0
    preferred_tasks: int = 0
    last_allocation: ProviderAllocationEvent | None = None


class ProviderAllocationProject(BaseModel):
    """A project's routing preference and effective limit."""

    project_id: str
    name: str = ""
    status: str = ""
    preferred_provider: str | None = None
    default_profile_id: str | None = None
    max_concurrent_agents: int | None = None


class ProviderAllocationDiagnostic(BaseModel):
    """A profile (or agent) bulk allocation never selects, and why."""

    kind: str
    id: str
    harness: str | None = None
    lifecycle: str | None = None
    provider: str | None = None
    #: retired_project_scoped, template, named, role, malformed or unknown_provider.
    reason: str


class ProviderAllocationStatusResponse(BaseModel):
    """``provider_allocation_status`` and ``GET /api/providers/allocation``."""

    success: bool = True
    now: float
    project_id: str | None = None
    #: True when the caller's scope hid other projects' detail.
    redacted: bool = False
    global_max_active: int | None = None
    providers: list[ProviderAllocationGroup] = []
    projects: list[ProviderAllocationProject] = []
    diagnostics: list[ProviderAllocationDiagnostic] = []


class ProviderAllocationBoundsBody(BaseModel):
    """Per selected pool profile; an explicit ``max: null`` removes the ceiling."""

    min: int | None = None
    #: An integer, ``null`` (unbounded) or the string ``"unbounded"``.
    max: int | str | None = None


class ProviderAllocationReceiveNewWorkBody(BaseModel):
    """One project's preferred provider for unpinned work."""

    project_id: str
    #: ``prefer`` or ``clear``.
    mode: str


class ProviderAllocationPreviewBody(BaseModel):
    """``POST /api/providers/allocation/preview``: one allocation request (spec §Backend commands)."""

    provider: str
    #: ``null`` or omitted selects every ordinary worker profile of the provider.
    profile_ids: list[str] | None = None
    #: ``pool`` or ``task``.
    participation: str | None = None
    bounds: ProviderAllocationBoundsBody | None = None
    receive_new_work: ProviderAllocationReceiveNewWorkBody | None = None
    #: ``graceful`` (default), ``idle-now`` or ``interrupt-busy``.
    drain: str | None = None
    allow_pinned_wait: bool | None = None


class ProviderAllocationRequest(BaseModel):
    """The canonical request a preview token was issued for.

    ``bounds`` keeps only the keys the caller gave, so ``{"max": null}``
    (unbounded) stays distinct from an omitted ``max``.
    """

    provider: str
    profile_ids: list[str] | None = None
    participation: str | None = None
    bounds: dict[str, int | None] | None = None
    receive_new_work: dict[str, str] | None = None
    drain: str = "graceful"
    allow_pinned_wait: bool = False


class ProviderAllocationProfileState(BaseModel):
    """The fields an allocation compares on one profile."""

    lifecycle: str
    enabled: bool = True
    min_active: int | None = None
    max_active: int | None = None
    min_per_project: int | None = None


class ProviderAllocationPreviewProfile(BaseModel):
    """One eligible profile of the provider, before and after the request."""

    profile_id: str
    name: str = ""
    harness: str | None = None
    intelligence_class: str | None = None
    selected: bool = False
    changed: bool = False
    changed_fields: list[str] = []
    before: ProviderAllocationProfileState
    after: ProviderAllocationProfileState


class ProviderAllocationCeilingChange(BaseModel):
    """The provider-wide configured ceiling before and after."""

    before: ProviderAllocationCeiling
    after: ProviderAllocationCeiling


class ProviderAllocationProjectLimit(BaseModel):
    """One project's effective max for one changed pool profile (``aq pool scale``)."""

    project_id: str
    profile_id: str
    max_concurrent_agents: int | None = None
    lifecycle_before: str
    lifecycle_after: str
    effective_max_before: int | None = None
    effective_max_after: int | None = None


class ProviderAllocationPreviewSession(BaseModel):
    """A live session of a changed profile and what the request does to it.

    ``action``: ``none``, ``stop`` (marked stopped, reconciler teardown),
    ``terminate`` (idle, now), ``stop_after_task`` (busy, finishes first) or
    ``interrupt`` (busy, only under an authorized ``interrupt-busy``).
    """

    session_id: str
    project_id: str | None = None
    profile_id: str
    lifecycle: str
    state: str
    activity: str
    task_id: str | None = None
    task_title: str | None = None
    action: str


class ProviderAllocationBusySet(BaseModel):
    """The busy sessions (and their tasks) the request stops."""

    session_ids: list[str] = []
    task_ids: list[str] = []


class ProviderAllocationPinnedTask(BaseModel):
    """An explicit pin on a changed profile; allocation never rewrites it."""

    task_id: str
    project_id: str | None = None
    profile_id: str
    status: str
    #: READY on a profile leaving the pool: the task stays on this provider.
    waits: bool = False


class ProviderAllocationPushChange(BaseModel):
    """A manual agent definition whose push eligibility the request changes."""

    agent_id: str
    name: str | None = None
    profile_id: str
    provider: str
    effective_harness: str | None = None
    state: str | None = None
    current_task_id: str | None = None
    push_before: bool
    push_after: bool


class ProviderAllocationPreference(BaseModel):
    """A project's preferred provider for unpinned work, before and after."""

    project_id: str
    mode: str
    before: str | None = None
    after: str | None = None
    changed: bool = False


class ProviderAllocationWarning(BaseModel):
    """``pinned_ready_wait`` (blocking), ``manual_agent_push_changes``, ``bounds_skipped``,
    ``no_change``."""

    code: str
    blocking: bool = False
    acknowledged: bool = False
    message: str
    subjects: list[str] = []


class ProviderAllocationPreviewResponse(BaseModel):
    """``provider_allocation_preview`` and ``POST /api/providers/allocation/preview``."""

    success: bool = True
    now: float | None = None
    provider: str
    vendor: str = ""
    state: str | None = None
    request: ProviderAllocationRequest
    #: ``operator`` or ``project_admin``: the least scope that may apply it.
    required_scope: str
    global_max_active: int | None = None
    selected: list[str] = []
    profiles: list[ProviderAllocationPreviewProfile] = []
    ceiling: ProviderAllocationCeilingChange
    project_limits: list[ProviderAllocationProjectLimit] = []
    sessions: list[ProviderAllocationPreviewSession] = []
    busy: ProviderAllocationBusySet
    pinned: list[ProviderAllocationPinnedTask] = []
    manual_agents: list[ProviderAllocationPushChange] = []
    preference: ProviderAllocationPreference | None = None
    warnings: list[ProviderAllocationWarning] = []
    #: A blocking warning is not acknowledged; apply refuses until it is.
    blocked: bool = False
    #: SHA-256 over the canonical request plus everything the preview observed.
    preview_token: str


class ProviderAllocationApplyBody(BaseModel):
    """``POST /api/providers/allocation/apply``: apply a reviewed preview by its token."""

    preview_token: str
    #: For ``drain: interrupt-busy``: exactly the preview's busy set (session or task ids).
    authorize_busy_interrupt: list[str] | None = None
    allow_pinned_wait: bool | None = None


class ProviderAllocationAppliedProfile(BaseModel):
    """One changed profile and what apply did to it.

    ``status``: ``applied``, ``failed``, ``rolled_back`` (applied, then
    compensated), ``rollback_failed`` or ``skipped`` (never reached after an
    earlier failure).  ``before`` / ``after`` are the previewed rows.
    """

    profile_id: str
    status: str
    changed_fields: list[str] = []
    before: ProviderAllocationProfileState
    after: ProviderAllocationProfileState
    error: str | None = None
    compensated: bool | None = None
    compensation_error: str | None = None


class ProviderAllocationSessionAction(BaseModel):
    """One thing apply did to a live session (``drain``, ``terminate``, ``interrupt`` ...)."""

    project_id: str | None = None
    profile_id: str | None = None
    session_id: str
    action: str
    reason: str | None = None
    error: str | None = None


class ProviderAllocationPlacement(BaseModel):
    """The queued ``class_only`` READY tasks a ``prefer`` moved to the provider.

    ``moved`` / ``held`` / ``skipped`` are ``provider_reroute`` decisions;
    ``batch_ids`` undo with ``provider_reroute_undo``.
    """

    applied: bool = False
    moved: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    batch_ids: list[str] = []
    detail: str | None = None
    #: Moves that raised; the apply is then ``partial``.
    errors: list[str] = []


class ProviderAllocationAppliedPreference(ProviderAllocationPreference):
    """The project preference apply wrote, and the queued work it re-placed."""

    applied: bool | None = None
    placement: ProviderAllocationPlacement | None = None


class ProviderAllocationApplyResponse(BaseModel):
    """``provider_allocation_apply`` and ``POST /api/providers/allocation/apply``.

    ``success`` only when ``status`` is ``applied``.  A refusal before any
    change carries ``error_code`` (``preview_unknown``, ``preview_stale``,
    ``pinned_wait_unacknowledged``, ``busy_authorization_required`` /
    ``_mismatch`` / ``_unexpected``) and the current ``preview`` when there
    is one; an apply that failed part-way carries ``status`` ``rolled_back``
    or ``partial`` with every row.
    """

    success: bool = True
    status: str | None = None
    error: str | None = None
    error_code: str | None = None
    #: A refusal's current preview (``preview_stale``: apply its token after review).
    preview: ProviderAllocationPreviewResponse | None = None
    request_id: str | None = None
    event_id: int | None = None
    provider: str | None = None
    vendor: str = ""
    actor: str | None = None
    preview_token: str | None = None
    request: ProviderAllocationRequest | None = None
    profiles: list[ProviderAllocationAppliedProfile] = []
    ceiling: ProviderAllocationCeilingChange | None = None
    preference: ProviderAllocationAppliedPreference | None = None
    session_actions: list[ProviderAllocationSessionAction] = []
    pinned: list[ProviderAllocationPinnedTask] = []
    manual_agents: list[ProviderAllocationPushChange] = []
    warnings: list[ProviderAllocationWarning] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "provider_allocation_apply": ProviderAllocationApplyResponse,
    "provider_allocation_preview": ProviderAllocationPreviewResponse,
    "provider_allocation_status": ProviderAllocationStatusResponse,
    "provider_status": ProviderStatusResponse,
    "provider_history": ProviderHistoryResponse,
    "provider_held_tasks": ProviderHeldTasksResponse,
    "provider_recheck": ProviderRecheckResponse,
    "provider_set_state": ProviderSetStateResponse,
    "provider_reroute": ProviderRerouteResponse,
    "provider_reroute_undo": ProviderRerouteUndoResponse,
}
