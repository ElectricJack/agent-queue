"""Deterministic route selection (mandatory-routing spec §6.4).

Every function here is a pure function of the task's facts, the policy
(:mod:`src.routing.policy`), an optional classification and one capacity
:class:`Snapshot`.  Nothing reads the database, the clock or a provider:
``task_route_plan`` builds the snapshot and calls :func:`plan_route`, and
``task_route_apply`` re-runs steps 3, 5 and 6 on a fresh snapshot through
:func:`reselect`.

The steps, as the spec numbers them:

1. **Kind and class** — the filer's hint beats the classification, which
   beats the kind's default; ``max_class`` clamps.  A classified risk with a
   ``risk`` rule then raises the class to that rule's floor, beating the hint
   and ``max_class`` alike.
2. **Candidates** — worker candidates for the lane (or the class), minus
   reserved cells, excluded providers, providers other than the project's
   ``preferred_provider``, for a task needing a non-pool workspace every
   pool profile and, for work the ``local_models`` gate refuses (a priority
   number at or below its floor, so more important work; a train bugfix;
   work others wait on), every
   self-hosted model, and every harness a classified risk's rule does not
   list.  A narrow lane's candidates form the preferred tier when the
   classification meets its ``requires``, its ``max_risk`` (an unknown risk
   does not) and its ``above_priority``.
2b. **Preference** — the filer's ``prefer_target``, a harness or a profile:
   ``strict`` leaves only the candidates that serve it and refuses to fall
   back, ``soft`` flags them so :func:`reselect` prefers them while they have
   headroom.  A task that names none is untouched.
3. **Availability** — an unlaunchable provider is dropped from the choice
   but stays in ``candidates``; nothing launchable is ``held``.
4. **Classification needed?** — only when the answer could change the route:
   a narrow lane's flags (and its ``max_risk``), and the risk when a ``risk``
   rule could raise the class or remove a candidate's harness.
5. **Load score** — ``pressure = (load + 1) / (slots × weight × usage ×
   availability)``; ``load = busy + eligible backlog``.  Blocked, unclaimable
   routed work (dependency, hold, origin, container, gate, already-assigned)
   is evidenced but never load: a worker cannot run it, so it must not
   consume a slot.
6. **Choice** — a hold lane's preferred tier, else the task's own preference
   with headroom, else a preferred-tier profile with a free slot, else the
   lowest pressure overall; ties go to candidate order.
7. **Reason** — one sentence naming what decided.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from src.models import TaskType
from src.profiles.catalog import worker_route
from src.routing.policy import (
    CLASSIFICATION_FLAGS,
    RISK_LEVELS,
    Balance,
    Lane,
    RoutingPolicy,
    risk_rank,
    selector_matches,
)
from src.routing.sources import LEGACY, OVERRIDE, ROLE, ROUTER, UNROUTED

#: A route the router must still write (§6.1).  ``legacy`` is re-routed.
ROUTABLE_SOURCES: frozenset[str] = frozenset({UNROUTED, LEGACY})
#: A route the router leaves alone: ``task_route_plan`` answers ``already_routed``.
ROUTED_SOURCES: frozenset[str] = frozenset({ROUTER, OVERRIDE, ROLE})
#: ``provider_availability`` states nothing may launch against (§6.4 step 3).
UNLAUNCHABLE_STATES: frozenset[str] = frozenset(
    {"exhausted", "unauthenticated", "failing", "disabled"}
)
#: Harness ``provider`` values that run a self-hosted model, so the profile is
#: subject to the policy's ``local_models`` gate.  ``ollama`` is what
#: ``vault/harnesses/opencode.md`` declares (``provider_liveness._OLLAMA_KEYS``).
LOCAL_MODEL_PROVIDERS: frozenset[str] = frozenset({"ollama"})

PREFERRED = "preferred"
FALLBACK = "fallback"

#: How the router may refuse a task's ``prefer_target`` (mandatory routing §4).
#: ``soft`` takes the target when it has headroom and routes normally when it
#: does not; ``strict`` allows only the target and never falls back.
PREFER_SOFT = "soft"
PREFER_STRICT = "strict"
PREFER_MODES: frozenset[str] = frozenset({PREFER_SOFT, PREFER_STRICT})

#: The kinds a classification may answer: the policy's kinds that are task
#: types, since ``task_route_apply`` may write the answer onto the task.
_TASK_TYPES: frozenset[str] = frozenset(member.value for member in TaskType)


# -- inputs --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """What the planner needs to know about one agent profile.

    ``classes`` holds the classes the profile's provider has a model for
    (``_validate_routing_class``); ``slots`` is ``max_active`` for a pool and
    the enabled agent count for a task-lifecycle profile.
    """

    id: str
    harness: str
    provider: str
    harness_family: str = ""
    lifecycle: str = "task"
    default_class: str = ""
    classes: frozenset[str] = frozenset()
    slots: int = 0
    enabled: bool = True
    template: bool = False
    read_only: bool = False
    runtime: str = ""
    needs_workspace: bool = True
    #: The harness runs a self-hosted model (:data:`LOCAL_MODEL_PROVIDERS`).
    local: bool = False


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    """One provider key's availability and its fullest fresh usage reading."""

    state: str = "available"
    launchable: bool = True
    usage_percent: float | None = None
    reason_code: str = ""
    updated_at: float | None = None
    quota: tuple[Mapping[str, Any], ...] = ()
    quota_source: str = "provider_usage_snapshots"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One capacity snapshot: profiles, providers and the live load per profile."""

    profiles: tuple[ProfileFacts, ...] = ()
    providers: Mapping[str, ProviderFacts] = field(default_factory=dict)
    #: Live busy sessions per profile, fleet-wide.
    busy: Mapping[str, int] = field(default_factory=dict)
    #: Routed backlog the planner's load admits: routed, READY work that passes
    #: the claim frontier (``count_routed_backlog_by_profile``'s ``eligible``).
    backlog: Mapping[str, int] = field(default_factory=dict)
    #: Routed work that is not currently claimable (the same query's
    #: ``blocked``): dependency-, hold-, origin-, container- or gate-blocked,
    #: plus already-assigned.  Evidenced in the reason, never load.
    blocked: Mapping[str, int] = field(default_factory=dict)
    #: Bounded observational context; never a capacity reservation or policy input.
    context: Mapping[str, Any] = field(default_factory=dict)
    #: Fresh server observations used only to rank hosted preference, not admission.
    headroom: Mapping[str, int] = field(default_factory=dict)
    #: Installed harness ids, for reporting lane selectors that match none;
    #: empty when unknown.
    harnesses: frozenset[str] = frozenset()

    def profile(self, profile_id: str) -> ProfileFacts | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def provider(self, key: str) -> ProviderFacts:
        return self.providers.get(key) or ProviderFacts()


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """The task-side inputs of a plan: hints, origin and constraints."""

    task_id: str
    title: str = ""
    description: str = ""
    task_type: str | None = None
    class_hint: str | None = None
    created_by_kind: str | None = None
    exclude_providers: frozenset[str] = frozenset()
    preferred_provider: str | None = None
    #: The filer's routing preference: a harness id or a profile id the router
    #: weighs before scoring.  ``None`` names nothing, which is the state of
    #: every task filed without ``--prefer``.
    prefer_target: str | None = None
    #: ``soft`` (:data:`PREFER_SOFT`) or ``strict`` (:data:`PREFER_STRICT`);
    #: :data:`PREFER_SOFT` for any value outside the vocabulary.
    prefer_mode: str = PREFER_SOFT
    #: The task needs a workspace kind other than ``project-repo``/``vault``,
    #: which keeps it away from pools (``_claim_preparation_predicates``).
    needs_task_lifecycle: bool = False
    #: ``benchmark:<arm>`` task labels; multiple selectors are an error.
    benchmark_arms: tuple[str, ...] = ()
    #: The task's priority; ``None`` (unknown) leaves the priority gate open.
    priority: int | None = None
    #: The task delivers through its project's integration train.
    on_train: bool = False
    #: An unfinished task waits on this one through a blocking edge.
    blocks_work: bool = False


# -- outputs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (profile, class) the policy allows for a task."""

    profile_id: str
    intelligence_class: str
    harness: str
    provider: str
    lifecycle: str
    tier: str = FALLBACK
    lane: str | None = None
    hold: bool = False
    preferred_hosted: bool = False
    #: The candidate serves the task's ``prefer_target`` (§4).
    prefer_target: bool = False
    local: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Candidate:
        tier = str(data.get("tier") or FALLBACK)
        return cls(
            profile_id=str(data["profile_id"]),
            intelligence_class=str(data["intelligence_class"]),
            harness=str(data.get("harness") or ""),
            provider=str(data.get("provider") or ""),
            lifecycle=str(data.get("lifecycle") or "task"),
            tier=tier if tier in {PREFERRED, FALLBACK} else FALLBACK,
            lane=(str(data["lane"]) if data.get("lane") else None),
            hold=bool(data.get("hold")),
            local=bool(data.get("local")),
            prefer_target=bool(data.get("prefer_target")),
            preferred_hosted=bool(data.get("preferred_hosted")),
        )


@dataclass(frozen=True, slots=True)
class Score:
    """The §6.4 step 5 terms for one launchable candidate."""

    profile_id: str
    intelligence_class: str
    tier: str
    slots: int
    busy: int
    backlog: int
    blocked: int
    load: int
    usage_percent: float | None
    usage_factor: float
    usage_soft_limited: bool
    avail_factor: float
    weight: float
    pressure: float
    effective_headroom: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Selection:
    """Steps 3, 5 and 6 over one candidate list."""

    chosen: Candidate
    scores: tuple[Score, ...]
    #: The preferred tier had a free slot and the choice came from it.
    took_preferred: bool
    took_hosted_preference: bool = False
    #: The choice came from the task's preferred target (§4).  With no
    #: preference, or when the target had no headroom, this is false.
    took_preferred_target: bool = False

    @property
    def score(self) -> Score:
        return next(s for s in self.scores if s.profile_id == self.chosen.profile_id
                    and s.intelligence_class == self.chosen.intelligence_class)

    @property
    def provider_intent(self) -> str:
        return "pinned" if self.chosen.hold else "class_only"


@dataclass(frozen=True, slots=True)
class PlanResult:
    """``task_route_plan``'s outcome and its value (§6.2)."""

    outcome: str
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Classification:
    """A validated §6.5 answer."""

    task_type: str
    intelligence_class: str
    flags: frozenset[str]
    reason: str = ""
    #: One of :data:`~src.routing.policy.RISK_LEVELS`; ``None`` when not answered.
    risk: str | None = None
    risk_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        record = {
            "task_type": self.task_type,
            "intelligence_class": self.intelligence_class,
            **{flag: flag in self.flags for flag in sorted(CLASSIFICATION_FLAGS)},
            "reason": self.reason,
        }
        if self.risk is not None:
            # Only an answered risk is recorded, so a record without one is
            # the record it was before risks existed.
            record["risk"] = self.risk
            record["risk_reason"] = self.risk_reason
        return record


# -- worker candidates ---------------------------------------------------------


def worker_classes(profile: ProfileFacts) -> frozenset[str]:
    """The classes *profile* is a worker candidate for; empty when it is none.

    A worker candidate (§4) is an enabled profile for which
    :func:`~src.profiles.catalog.worker_route` returns a route and which has
    at least one slot.  That excludes templates, ``named`` and read-only
    profiles and the shipped stage profiles.  A task-lifecycle worker with no
    fixed class is eligible for every class its provider maps, as in
    ``build_route_options``; a pool with no fixed class claims nothing.
    """
    if not profile.enabled or profile.slots <= 0 or ":" in profile.id:
        return frozenset()
    if profile.runtime == "supervisor" or not profile.harness:
        return frozenset()
    fixed = profile.default_class.strip()
    if not fixed and profile.lifecycle != "task":
        return frozenset()
    eligible = frozenset({fixed}) & profile.classes if fixed else profile.classes
    return frozenset(
        class_id for class_id in eligible
        if worker_route(
            profile.id,
            harness=profile.harness,
            default_class=class_id,
            lifecycle=profile.lifecycle,
            template=profile.template,
            read_only=profile.read_only,
        ) is not None
    )


def is_candidate(candidate: Candidate, snapshot: Snapshot) -> bool:
    """Is *candidate* still a worker candidate for its class in *snapshot*?"""
    profile = snapshot.profile(candidate.profile_id)
    return (
        profile is not None
        and profile.harness == candidate.harness
        and profile.provider == candidate.provider
        and profile.lifecycle == candidate.lifecycle
        and candidate.intelligence_class in worker_classes(profile)
    )


# -- classification ------------------------------------------------------------


def allowed_kinds(policy: RoutingPolicy) -> list[str]:
    return [kind for kind in policy.kinds if kind in _TASK_TYPES]


def read_classification(
    raw: Any, policy: RoutingPolicy
) -> tuple[Classification | None, bool, str | None]:
    """``(classification, failed, error)`` for a ``classification`` argument.

    ``None`` means no classification was asked for yet.  ``{"failed": true}``
    and an answer outside ``allowed_kinds`` / ``allowed_classes`` (or a
    ``risk`` outside :data:`~src.routing.policy.RISK_LEVELS`) are a failed
    classification (§6.5): the plan proceeds with the defaults and treats
    every ``requires`` flag as false and the risk as unknown.  An answer
    without ``risk`` leaves the risk unknown.
    """
    if raw is None:
        return None, False, None
    if not isinstance(raw, Mapping):
        return None, True, "classification is not an object"
    if raw.get("failed"):
        return None, True, None
    task_type = raw.get("task_type")
    class_id = raw.get("intelligence_class")
    if task_type not in allowed_kinds(policy):
        return None, True, f"classification task_type {task_type!r} is not an allowed kind"
    if class_id not in policy.class_order:
        return None, True, f"classification intelligence_class {class_id!r} is not an allowed class"
    flags: set[str] = set()
    for flag in CLASSIFICATION_FLAGS:
        value = raw.get(flag, False)
        if not isinstance(value, bool):
            return None, True, f"classification {flag} must be true or false"
        if value:
            flags.add(flag)
    risk = raw.get("risk")
    if risk is not None and (not isinstance(risk, str) or risk not in RISK_LEVELS):
        return None, True, f"classification risk {risk!r} is not a risk level"
    risk_reason = raw.get("risk_reason")
    if risk_reason is None:
        risk_reason = ""
    elif not isinstance(risk_reason, str):
        return None, True, "classification risk_reason must be a string"
    reason = raw.get("reason") or ""
    return (
        Classification(
            str(task_type), str(class_id), frozenset(flags), str(reason)[:400],
            risk=risk, risk_reason=risk_reason[:400],
        ),
        False,
        None,
    )


# -- steps 1 and 2 -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rule:
    kind: str
    rule: str
    origin: str | None
    class_id: str
    hint: str | None
    clamped_from: str | None
    lane: str | None
    narrow: bool
    prefer_harnesses: tuple[str, ...]
    notes: tuple[str, ...]
    #: The class before a ``risk`` rule's floor raised it, and that risk.
    raised_from: str | None = None
    raised_for_risk: str | None = None
    #: The risk was not answered, and :data:`ASSUMED_RISK` stood in for it.
    risk_assumed: bool = False


#: The risk a policy that reads risks applies when the classifier answered
#: without one, or failed: not knowing is not evidence the change is safe.
ASSUMED_RISK = "medium"


def _effective_risk(
    policy: RoutingPolicy, classification: Any, classified: Classification | None,
) -> tuple[str | None, bool]:
    """The risk the plan applies, and whether it was assumed.

    Only an answer, failed or not, assumes one: before the classifier runs an
    unknown risk stays unknown, so the planner still asks for it.
    """
    risk = classified.risk if classified else None
    if risk is None and classification is not None and policy.uses_risk:
        return ASSUMED_RISK, True
    return risk, False


def _rule(
    task: TaskFacts, policy: RoutingPolicy, classified: Classification | None,
    risk: str | None = None, *, risk_assumed: bool = False,
) -> _Rule:
    """Step 1: the kind, the merged rule and the class.

    The *risk*'s floor comes last, so it beats a hint and ``max_class``: it
    is a safety floor, and an operator who wants a task below it overrides
    the route.
    """
    notes: list[str] = []
    kind = task.task_type or (classified.task_type if classified else None) or policy.default_kind
    base = policy.kinds.get(kind)
    rule_name = f"kinds.{kind}"
    if base is None:
        notes.append(f"kind {kind} has no rule, used {policy.default_kind}")
        base = policy.kinds[policy.default_kind]
        rule_name = f"kinds.{policy.default_kind}"
    class_id, max_class, lane, narrow = base.class_, base.max_class, base.lane, base.narrow
    prefer_harnesses = base.prefer_harnesses
    origin_name = (task.created_by_kind or "").strip() or None
    origin = policy.origins.get(origin_name) if origin_name else None
    if origin is not None:
        rule_name += f"+origins.{origin_name}"
        class_id = origin.class_ if origin.class_ is not None else class_id
        max_class = origin.max_class if origin.max_class is not None else max_class
        lane = origin.lane if origin.lane is not None else lane
        narrow = origin.narrow if origin.narrow is not None else narrow
        if origin.prefer_harnesses is not None:
            prefer_harnesses = origin.prefer_harnesses
    else:
        origin_name = None
    hint = (task.class_hint or "").strip() or None
    if hint is not None and hint not in policy.class_order:
        notes.append(f"class hint {hint} is not on class_order, ignored")
        hint = None
    chosen = hint or (classified.intelligence_class if classified else None) or class_id
    clamped_from = None
    if max_class is not None and policy.rank(chosen) > policy.rank(max_class):
        clamped_from, chosen = chosen, max_class
    raised_from = raised_for_risk = None
    if risk_assumed:
        notes.append(f"no risk answer, treated as {risk}")
    risk_rule = policy.risk.get(risk) if risk is not None else None
    if risk_rule is not None:
        floor = risk_rule.floor(classified.flags if classified else ())
        if policy.rank(chosen) < policy.rank(floor):
            raised_from, raised_for_risk, chosen = chosen, risk, floor
            notes.append(f"class raised from {raised_from} to {chosen} for risk {risk}")
    return _Rule(
        kind=kind, rule=rule_name, origin=origin_name, class_id=chosen, hint=hint,
        clamped_from=clamped_from, lane=lane, narrow=narrow,
        prefer_harnesses=prefer_harnesses, notes=tuple(notes),
        raised_from=raised_from, raised_for_risk=raised_for_risk, risk_assumed=risk_assumed,
    )


def _risk_harnesses(policy: RoutingPolicy, risk: str | None) -> tuple[str, ...]:
    """The harness selectors a classified *risk* restricts candidates to; ``()`` for none."""
    risk_rule = policy.risk.get(risk) if risk is not None else None
    return risk_rule.harnesses if risk_rule is not None else ()


def _lane_admits_risk(lane: Lane, risk: str | None) -> bool:
    """A narrow lane's ``max_risk`` is met; an unknown risk meets no cap."""
    if lane.max_risk is None:
        return True
    return risk is not None and risk_rank(risk) <= risk_rank(lane.max_risk)


def _lane_admits_priority(lane: Lane, task: TaskFacts) -> bool:
    """A narrow lane's ``above_priority`` is met; an unknown priority meets it.

    A lower number is more important (the claim frontier takes it first), so
    the floor keeps a lane to the less important work above it.
    """
    return (
        lane.above_priority is None
        or task.priority is None
        or task.priority > lane.above_priority
    )


def _cells(
    snapshot: Snapshot,
    class_id: str,
    selectors: frozenset[str] | None,
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[ProfileFacts]:
    """Worker profiles at *class_id* a lane's *selectors* admit and *exclude* does not."""
    return [
        profile for profile in snapshot.profiles
        if class_id in worker_classes(profile)
        and (selectors is None or any(selector_matches(profile.harness, s) for s in selectors))
        and not any(selector_matches(profile.harness, s) for s in exclude)
    ]


def _order_key(policy: RoutingPolicy, lane: Lane | None):
    prefer = lane.preferred_harnesses if lane is not None else ()
    tie = policy.balance.tie_order

    def key(candidate: Candidate) -> tuple:
        return (
            prefer.index(candidate.harness) if candidate.harness in prefer else len(prefer),
            tie.index(candidate.harness) if candidate.harness in tie else len(tie),
            candidate.profile_id,
        )

    return key


def _reserved_away(policy: RoutingPolicy, candidate: Candidate) -> bool:
    return any(
        cell.class_ == candidate.intelligence_class
        and cell.harness == candidate.harness
        and candidate.lane not in cell.only_lanes
        for cell in policy.reserved
    )


def local_refusal(
    task: TaskFacts, policy: RoutingPolicy, kind: str | None = None
) -> str | None:
    """Why the policy keeps *task* off a local model, or ``None`` when it may run there.

    *kind* is the kind step 1 applied, which a classification may have
    supplied; without it the task's own kind is used.
    """
    gate = policy.local_models
    if task.priority is not None and task.priority <= gate.above_priority:
        return f"priority {task.priority} is not above {gate.above_priority}"
    kind = kind or task.task_type
    if task.on_train and kind in gate.train_kinds:
        return f"a {kind} delivered through the integration train"
    if task.blocks_work and not gate.allow_blocking:
        return "other work waits on it"
    return None


def is_local(candidate: Candidate, policy: RoutingPolicy) -> bool:
    """The candidate's harness runs a self-hosted model (``local_models``)."""
    return candidate.local or candidate.harness in policy.local_models.harnesses


def _filter(
    candidates: list[Candidate], task: TaskFacts, policy: RoutingPolicy,
    *, respect_reserved: bool = True, local_gate: bool = True, kind: str | None = None,
    risk_harnesses: tuple[str, ...] = (),
) -> tuple[list[Candidate], str | None]:
    """Step 2's removals, returning the survivors and why the list emptied.

    ``local_gate=False`` is for an allowlisted benchmark arm, which names its
    harness explicitly.  *risk_harnesses* are the selectors a classified
    risk's rule allows; empty restricts nothing.
    """
    local_refused = local_gate and local_refusal(task, policy, kind) is not None
    stages = (
        ("reserved", lambda c: not respect_reserved or not _reserved_away(policy, c)),
        ("excluded_providers", lambda c: c.provider not in task.exclude_providers),
        (
            "preferred_provider_unavailable",
            lambda c: not task.preferred_provider or c.provider == task.preferred_provider,
        ),
        ("workspace_requirement", lambda c: not task.needs_task_lifecycle or c.lifecycle != "pool"),
        ("local_model_gate", lambda c: not local_refused or not is_local(c, policy)),
        (
            "risk_harnesses",
            lambda c: not risk_harnesses
            or any(selector_matches(c.harness, s) for s in risk_harnesses),
        ),
    )
    reason = None if candidates else "no_worker_candidates"
    for name, keep in stages:
        kept = [candidate for candidate in candidates if keep(candidate)]
        if candidates and not kept:
            reason = name
        candidates = kept
    return candidates, reason


def _candidate(profile: ProfileFacts, class_id: str, *, tier: str, lane: str | None,
               hold: bool = False, preferred_hosted: bool = False) -> Candidate:
    return Candidate(
        profile_id=profile.id, intelligence_class=class_id, harness=profile.harness,
        provider=profile.provider, lifecycle=profile.lifecycle, tier=tier, lane=lane, hold=hold,
        preferred_hosted=preferred_hosted, local=profile.local,
    )


@dataclass(frozen=True, slots=True)
class _Pool:
    """Step 2's output: the ordered candidates, or why there are none."""

    candidates: tuple[Candidate, ...]
    #: Narrow-lane candidates whose ``requires`` are not known yet (step 4b).
    potential: tuple[Candidate, ...]
    reason: str | None


def _candidates(
    task: TaskFacts,
    policy: RoutingPolicy,
    snapshot: Snapshot,
    rule: _Rule,
    flags: frozenset[str] | None,
    risk: str | None = None,
) -> _Pool:
    """Step 2.  *flags* ``None`` means the classification is still unknown;
    *risk* ``None`` means no risk was classified."""
    risk_harnesses = _risk_harnesses(policy, risk)
    if rule.lane is not None:
        lane = policy.lanes[rule.lane]
        class_id = lane.class_ or rule.class_id
        cells = _cells(snapshot, class_id, frozenset(lane.harnesses))
        prefer = lane.preferred_harnesses
        found = [
            _candidate(
                profile, class_id,
                tier=PREFERRED if profile.harness in prefer else FALLBACK,
                lane=rule.lane, hold=lane.hold,
            )
            for profile in cells
        ]
        found, reason = _filter(
            found, task, policy, kind=rule.kind, risk_harnesses=risk_harnesses,
        )
        ordered = sorted(found, key=_order_key(policy, lane))
        preferred = [c for c in ordered if c.tier == PREFERRED]
        fallback = [c for c in ordered if c.tier == FALLBACK]
        return _Pool(tuple(preferred + fallback), (), reason)

    # OpenCode is eligible only through a narrow lane, independent of whether
    # the active policy names every installed member of that CLI family.
    from src.routing.policy import is_opencode_family

    general = [
        _candidate(profile, rule.class_id, tier=FALLBACK, lane=None,
                   preferred_hosted=profile.harness in rule.prefer_harnesses)
        for profile in _cells(
            snapshot, rule.class_id, None, exclude=policy.narrow_harnesses()
        )
        if not is_opencode_family(profile.harness, profile.provider, profile.harness_family)
    ]
    general, reason = _filter(
        general, task, policy, kind=rule.kind, risk_harnesses=risk_harnesses,
    )
    general.sort(key=_order_key(policy, None))

    preferred: list[Candidate] = []
    potential: list[Candidate] = []
    if rule.narrow:
        for name, lane in policy.narrow_lanes():
            mapped = (lane.classes or {}).get(rule.class_id)
            if mapped is None or not _lane_admits_priority(lane, task):
                # The priority floor is known before any classification, so
                # a lane it refuses is not even a potential one.
                continue
            lane_cells = [
                _candidate(profile, mapped, tier=PREFERRED, lane=name)
                for profile in _cells(snapshot, mapped, frozenset(lane.harnesses))
            ]
            lane_cells, _reason = _filter(
                lane_cells, task, policy, kind=rule.kind, risk_harnesses=risk_harnesses,
            )
            lane_cells.sort(key=_order_key(policy, lane))
            if flags is None:
                potential.extend(lane_cells)
            elif set(lane.requires) <= flags and _lane_admits_risk(lane, risk):
                preferred.extend(lane_cells)
    seen: set[tuple[str, str]] = set()
    ordered: list[Candidate] = []
    for candidate in preferred + general:
        key = (candidate.profile_id, candidate.intelligence_class)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return _Pool(tuple(ordered), tuple(potential), None if ordered else reason)


# -- step 2b: the task's preference ---------------------------------------------


def prefer_target(task: TaskFacts) -> str:
    """The preference *task* names, or ``""`` when it names none."""
    return (task.prefer_target or "").strip()


def prefer_mode(task: TaskFacts) -> str:
    """The preference's mode, defaulting to ``soft`` for any other value."""
    return task.prefer_mode if task.prefer_mode in PREFER_MODES else PREFER_SOFT


def serves_preference(candidate: Candidate, target: str) -> bool:
    """Whether *candidate* is the named profile or runs on the named harness.

    A harness target matches every candidate on that harness (each class rung of
    it); a profile target matches that one profile.  Filing resolves the name the
    same way -- a profile id is taken as a profile id when one exists -- so the
    resolution the filer saw is the one the planner applies.
    """
    return bool(target) and (candidate.profile_id == target or candidate.harness == target)


def _flagged(candidates: tuple[Candidate, ...], target: str) -> tuple[Candidate, ...]:
    return tuple(
        replace(candidate, prefer_target=True) if serves_preference(candidate, target)
        else replace(candidate, prefer_target=False)
        for candidate in candidates
    )


def _apply_preference(pool: _Pool, task: TaskFacts) -> tuple[_Pool, str | None]:
    """Step 2b: narrow the pool to the preference, or flag what serves it.

    A strict preference admits only candidates that serve it: when none does,
    the reason comes back and the caller reports ``no_candidates`` instead of
    routing somewhere else, so the task waits for its target.  A soft
    preference keeps the whole pool and only flags its own candidates, which
    :func:`reselect` prefers while they have headroom.  With no preference the
    pool comes back untouched.
    """
    target = prefer_target(task)
    if not target:
        return pool, None
    matching = tuple(c for c in pool.candidates if serves_preference(c, target))
    matching_potential = tuple(c for c in pool.potential if serves_preference(c, target))
    if prefer_mode(task) == PREFER_STRICT:
        if not matching and not matching_potential:
            return _Pool((), (), "prefer_target_unavailable"), "prefer_target_unavailable"
        return _Pool(matching, matching_potential, pool.reason), None
    return _Pool(_flagged(pool.candidates, target), _flagged(pool.potential, target),
                 pool.reason), None


def preference_record(
    task: TaskFacts,
    candidates: Sequence[Candidate],
    snapshot: Snapshot,
    selection: Selection | None = None,
) -> dict[str, Any] | None:
    """The preference as the route decision records it, or ``None`` for none.

    ``honoured`` is whether the chosen candidate serves the target.  For a
    soft preference that did not win, ``fallback_reason`` says why: no
    candidate serves it, its providers are not launchable, it had no headroom,
    or an allowlisted benchmark arm pins the task's model.
    """
    target = prefer_target(task)
    if not target:
        return None
    mode = prefer_mode(task)
    matching = [c for c in candidates if serves_preference(c, target)]
    honoured = selection is not None and serves_preference(selection.chosen, target)
    reason: str | None = None
    if not honoured:
        if task.benchmark_arms:
            reason = "benchmark_arm_pins_its_model"
        elif not matching:
            reason = "no_candidate_serves_the_target"
        elif not any(launchable(c, snapshot) for c in matching):
            reason = "provider_unavailable"
        else:
            reason = "no_headroom"
    return {
        "target": target,
        "mode": mode,
        "kind": "profile" if any(c.profile_id == target for c in candidates) else "harness",
        "honoured": honoured,
        "fallback_reason": reason,
    }


# -- steps 3, 5 and 6 ----------------------------------------------------------


def launchable(candidate: Candidate, snapshot: Snapshot) -> bool:
    facts = snapshot.provider(candidate.provider)
    return facts.launchable and facts.state not in UNLAUNCHABLE_STATES


def _usage_factor(usage: float | None, balance: Balance) -> float:
    soft = balance.usage_soft_percent
    if usage is None or usage <= soft:
        return 1.0
    span = (min(usage, 100.0) - soft) / (100.0 - soft)
    return max(balance.usage_floor_factor, 1.0 - span * (1.0 - balance.usage_floor_factor))


def score(candidate: Candidate, snapshot: Snapshot, balance: Balance) -> Score | None:
    """Step 5 for one candidate; ``None`` when its profile has no slot."""
    profile = snapshot.profile(candidate.profile_id)
    slots = profile.slots if profile is not None else 0
    if slots <= 0:
        return None
    facts = snapshot.provider(candidate.provider)
    busy = int(snapshot.busy.get(candidate.profile_id, 0))
    backlog = int(snapshot.backlog.get(candidate.profile_id, 0))
    blocked = int(snapshot.blocked.get(candidate.profile_id, 0))
    # Evaluate every window against its provider's own soft limit, then combine
    # capacity factors conservatively. Never compare raw percentages between
    # unlike windows/providers; stale/reset observations are evidence only.
    if facts.quota:
        factors = [(_usage_factor(float(q["used_percent"]), balance), float(q["used_percent"]))
                   for q in facts.quota if q["freshness"] == "fresh"]
        usage_factor, usage = min(factors, key=lambda f: f[0], default=(1.0, None))
    else:
        usage = facts.usage_percent
        usage_factor = _usage_factor(usage, balance)
    avail_factor = balance.degraded_factor if facts.state == "degraded" else 1.0
    weight = balance.weight(candidate.harness)
    pressure = (busy + backlog + 1) / (slots * weight * usage_factor * avail_factor)
    return Score(
        profile_id=candidate.profile_id, intelligence_class=candidate.intelligence_class,
        tier=candidate.tier, slots=slots, busy=busy, backlog=backlog, blocked=blocked, load=busy + backlog,
        usage_percent=usage, usage_factor=round(usage_factor, 4),
        usage_soft_limited=usage is not None and usage > balance.usage_soft_percent,
        avail_factor=avail_factor, weight=weight, pressure=round(pressure, 4),
        effective_headroom=snapshot.headroom.get(candidate.profile_id),
    )


def reselect(
    candidates: Sequence[Candidate], snapshot: Snapshot, balance: Balance
) -> Selection | None:
    """Steps 3, 5 and 6 over *candidates*; ``None`` when nothing is launchable.

    ``task_route_apply`` calls this on a fresh snapshot, so a burst of plans
    made against one snapshot spreads out as each apply sees the routes the
    applies before it wrote.
    """
    scored: list[tuple[int, Candidate, Score]] = []
    for index, candidate in enumerate(candidates):
        if not launchable(candidate, snapshot):
            continue
        result = score(candidate, snapshot, balance)
        if result is not None:
            scored.append((index, candidate, result))
    if not scored:
        return None
    free_preferred = [
        entry for entry in scored
        if entry[1].tier == PREFERRED and entry[2].load < entry[2].slots
    ]
    # A hold lane pins the provider, so its preferred tier outranks even a
    # preference that has headroom; every other preference tier falls below
    # the task's own.
    free_hold = [
        entry for entry in scored
        if entry[1].hold and entry[1].tier == PREFERRED and _free(entry[2])
    ]
    free_target = [entry for entry in scored if entry[1].prefer_target and _free(entry[2])]
    free_hosted = [
        entry for entry in scored
        if entry[1].preferred_hosted
        and snapshot.provider(entry[1].provider).state == "available"
        and not entry[2].usage_soft_limited
        and _free(entry[2])
    ]
    # Only the opt-in hosted preference changes capacity fallback. Other policy
    # documents keep their existing pressure and lane behavior.
    free_fallback = [entry for entry in scored if _free(entry[2])]
    hosted = any(candidate.preferred_hosted for candidate in candidates)
    if free_hold:
        pool, source = free_hold, "hold"
    elif free_target:
        pool, source = free_target, "preference"
    elif free_preferred:
        pool, source = free_preferred, "lane_preference"
    elif free_hosted:
        pool, source = free_hosted, "hosted_preference"
    elif hosted and free_fallback:
        pool, source = free_fallback, "pressure_fallback"
    else:
        pool, source = scored, "pressure_fallback"
    _index, chosen, _score = min(pool, key=lambda entry: (entry[2].pressure, entry[0]))
    return Selection(
        chosen=chosen,
        scores=tuple(entry[2] for entry in scored),
        took_preferred=source in {"hold", "lane_preference"},
        took_hosted_preference=source == "hosted_preference",
        took_preferred_target=source == "preference",
    )


def _free(result: Score) -> bool:
    return result.load < result.slots and (
        result.effective_headroom is None or result.effective_headroom > 0
    )


def selection_evidence(
    candidates: Sequence[Candidate], selection: Selection, snapshot: Snapshot,
    *, prefer_harnesses: Sequence[str] = (),
    preference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bounded fit, capacity and quota provenance for the actual fresh selection."""
    scores = {(s.profile_id, s.intelligence_class): s for s in selection.scores}
    observations = []
    bounded = [selection.chosen, *(c for c in candidates if c != selection.chosen)][:64]
    for candidate in bounded:
        facts = snapshot.provider(candidate.provider)
        result = scores.get((candidate.profile_id, candidate.intelligence_class))
        bypass = None
        if candidate.preferred_hosted:
            if not launchable(candidate, snapshot):
                bypass = "provider_unavailable"
            elif facts.state != "available":
                bypass = "provider_degraded"
            elif result is None or not _free(result):
                bypass = "capacity_full"
            elif result.usage_soft_limited:
                bypass = "own_usage_above_soft_limit"
        observations.append({
            "profile_id": candidate.profile_id, "provider": candidate.provider,
            "class": candidate.intelligence_class, "preferred_hosted": candidate.preferred_hosted,
            "state": facts.state, "launchable": launchable(candidate, snapshot),
            "load": result.load if result else None, "slots": result.slots if result else None,
            "effective_headroom": snapshot.headroom.get(candidate.profile_id),
            "usage_percent": result.usage_percent if result else None,
            "usage_factor": result.usage_factor if result else None,
            "usage_soft_limited": result.usage_soft_limited if result else None,
            "quota_status": "observed" if facts.quota else "unknown",
            "quota": list(facts.quota[:8]), "preference_bypassed": bypass,
        })
    if selection.took_preferred_target:
        mode = "task_preference"
    elif selection.took_preferred:
        mode = "lane_preference"
    elif selection.took_hosted_preference:
        mode = "hosted_preference"
    else:
        mode = "pressure_fallback"
    return {
        "mode": mode, "profile_id": selection.chosen.profile_id,
        "provider": selection.chosen.provider, "class": selection.chosen.intelligence_class,
        "snapshot_as_of": snapshot.context.get("as_of"),
        "snapshot_age_seconds": snapshot.context.get("collection_seconds"),
        "prefer_harnesses": list(prefer_harnesses),
        "preference_unavailable": bool(prefer_harnesses) and not any(
            c.preferred_hosted for c in candidates
        ),
        #: The task's own ``--prefer``: what was asked, in which mode, whether
        #: it decided the route, and why not when it did not (§4).
        "preference": dict(preference) if preference else None,
        "candidates": observations, "truncated": len(candidates) > 64,
    }


def selection_reason(evidence: Mapping[str, Any]) -> str:
    """Human-readable evidence, also used after apply reselects."""
    chosen = next(c for c in evidence["candidates"] if c["profile_id"] == evidence["profile_id"])
    headroom = chosen["effective_headroom"]
    windows = ", ".join(
        f"{q['window']}({q['scope']}) {q['used_percent']:g}% {q['freshness']} "
        f"age {q['age_seconds']:g}s" for q in chosen["quota"][:2]
    ) or (f"own usage {chosen['usage_percent']:g}% (window/age unknown)"
          if chosen["usage_percent"] is not None else "quota unknown")
    age = evidence["snapshot_age_seconds"]
    reason = (
        f"{evidence['mode']}: {evidence['profile_id']}/{evidence['provider']} "
        f"class {evidence['class']}, {chosen['state']}, load {chosen['load']}/{chosen['slots']}, "
        f"headroom {headroom if headroom is not None else 'unknown'}; {windows}; "
        f"snapshot age {f'{age:g}s' if age is not None else 'unknown'}"
    )
    bypassed = [f"{c['profile_id']}: {c['preference_bypassed']}"
                for c in evidence["candidates"] if c["preference_bypassed"]]
    if evidence["preference_unavailable"]:
        bypassed.append("no compatible preferred hosted candidate")
    preference = evidence.get("preference")
    if preference:
        asked = f"preferred {preference['target']} ({preference['mode']})"
        bypassed.append(
            f"{asked} honoured" if preference["honoured"]
            else f"{asked} not honoured ({preference['fallback_reason']})"
        )
    return reason + ("; bypassed " + ", ".join(bypassed[:2]) if bypassed else "")


# -- step 4 --------------------------------------------------------------------


def _risk_could_change_route(
    policy: RoutingPolicy, rule: _Rule, candidates: Sequence[Candidate]
) -> bool:
    """A classified risk could raise *rule*'s class or remove one of *candidates*."""
    for risk_rule in policy.risk.values():
        if policy.rank(risk_rule.min_class) > policy.rank(rule.class_id):
            return True
        if risk_rule.harnesses and any(
            not any(selector_matches(c.harness, s) for s in risk_rule.harnesses)
            for c in candidates
        ):
            return True
    return False


# -- step 7 --------------------------------------------------------------------


def _reason(rule: _Rule, pool: _Pool, selection: Selection) -> str:
    head = f"kind {rule.kind}"
    if rule.origin:
        head += f" ({rule.origin})"
    head += f", class {rule.class_id}"
    if rule.clamped_from:
        head += f" (clamped from {rule.clamped_from})"
    parts = [head, *rule.notes]
    chosen = selection.chosen
    if chosen.lane is not None:
        parts.append(f"lane {chosen.lane}")
    preferred = [s for s in selection.scores if s.tier == PREFERRED]
    has_fallback = any(c.tier == FALLBACK for c in pool.candidates)
    if preferred and has_fallback and not selection.took_preferred:
        busiest = ", ".join(f"{s.profile_id} {s.load}/{s.slots} busy" for s in preferred)
        parts.append(f"preferred tier full ({busiest}) → fell back")
    others = sorted(
        (s for s in selection.scores if s.profile_id != chosen.profile_id),
        key=lambda s: s.pressure,
    )[:2]
    decided = f"lowest pressure {chosen.profile_id} {selection.score.pressure:.2f}"
    if selection.took_preferred_target:
        decided = f"task preference {chosen.profile_id} {selection.score.pressure:.2f}"
    elif selection.took_preferred:
        decided = f"preferred {chosen.profile_id} {selection.score.pressure:.2f}"
    elif selection.took_hosted_preference:
        decided = f"hosted preference {chosen.profile_id} {selection.score.pressure:.2f}"
    if others:
        decided += " vs " + ", ".join(f"{s.profile_id} {s.pressure:.2f}" for s in others)
    parts.append(decided)
    return "; ".join(parts)[:600]


# -- the plan ------------------------------------------------------------------


def plan_route(
    task: TaskFacts,
    policy: RoutingPolicy,
    snapshot: Snapshot,
    *,
    policy_sha256: str,
    classification: Any = None,
) -> PlanResult:
    """``task_route_plan`` for a task the router still owes a route."""
    if task.benchmark_arms:
        # A benchmark arm must never inherit the ordinary class fallbacks:
        # they would turn an unavailable model into another arm's result.
        if len(task.benchmark_arms) != 1:
            return PlanResult("no_candidates", {
                "task_id": task.task_id, "reason": "benchmark_selector_ambiguous",
            })
        name = task.benchmark_arms[0]
        arm = policy.benchmark_arms.get(name)
        if arm is None:
            return PlanResult("no_candidates", {
                "task_id": task.task_id, "reason": "benchmark_arm_not_allowlisted",
                "benchmark_arm": name,
            })
        candidates = [
            _candidate(profile, arm.class_, tier=PREFERRED, lane=None, hold=True)
            for profile in _cells(snapshot, arm.class_, None)
            if profile.harness == arm.harness
        ]
        candidates, reason = _filter(
            candidates, task, policy, respect_reserved=False, local_gate=False,
        )
        if not candidates:
            return PlanResult("no_candidates", {
                "task_id": task.task_id, "reason": reason or "no_worker_candidates",
                "benchmark_arm": name,
            })
        if not any(launchable(c, snapshot) for c in candidates):
            return PlanResult("held", {
                "task_id": task.task_id, "benchmark_arm": name,
                "candidates": [c.as_dict() for c in candidates],
                "providers": sorted({c.provider for c in candidates}),
            })
        selection = reselect(candidates, snapshot, policy.balance)
        assert selection is not None
        return PlanResult("planned", {
            "task_id": task.task_id,
            "intelligence_class": selection.chosen.intelligence_class,
            "profile_id": selection.chosen.profile_id,
            "provider": selection.chosen.provider,
            "provider_intent": "pinned",
            "task_type": task.task_type or policy.default_kind,
            "lane": None,
            "rule": f"benchmark_arms.{name}",
            "benchmark_arm": name,
            "benchmark_class": arm.class_,
            "benchmark_harness": arm.harness,
            "requested_model": arm.requested_model,
            "observed_models": list(arm.observed_models),
            "candidates": [
                {**c.as_dict(), "launchable": launchable(c, snapshot)} for c in candidates
            ],
            "scores": [s.as_dict() for s in selection.scores],
            # An allowlisted arm pins the model, so a preference never moves it.
            "preference": preference_record(task, candidates, snapshot, selection),
            "reason": f"allowlisted benchmark arm {name}: {arm.class_}/{arm.harness}",
            "policy_sha256": policy_sha256,
            "classification": None,
            "balance": policy.balance.model_dump(mode="json"),
        })
    classified, failed, error = read_classification(classification, policy)
    known = classification is not None
    flags = (classified.flags if classified else frozenset()) if known else None
    risk, risk_assumed = _effective_risk(policy, classification, classified)
    rule = _rule(task, policy, classified, risk, risk_assumed=risk_assumed)
    unmatched = policy.unmatched_selectors(snapshot.harnesses) if snapshot.harnesses else []
    if unmatched:
        # Reported, never refused: OpenCode harnesses are operator-installed, so
        # the shipped lanes match nothing on an install without them.
        rule = replace(rule, notes=(
            *rule.notes, "lane selectors match no installed harness: " + ", ".join(unmatched),
        ))
    pool = _candidates(task, policy, snapshot, rule, flags, risk)
    eligible = pool.candidates
    pool, refused = _apply_preference(pool, task)

    if refused is not None:
        # A strict preference with no candidate at all: the task waits for its
        # target instead of being routed somewhere the filer did not ask for.
        return PlanResult("no_candidates", {
            "task_id": task.task_id,
            "reason": refused,
            "detail": (
                f"strict preference {prefer_target(task)}: no worker candidate serves it"
                + (f" in lane {rule.lane}" if rule.lane else "")
            ),
            # The pre-preference list, so ``kind`` still reads as profile or
            # harness for a name the planner could not place.
            "preference": preference_record(task, eligible, snapshot, None),
        })

    if not pool.candidates and not pool.potential:
        return PlanResult("no_candidates", {
            "task_id": task.task_id,
            "reason": pool.reason or "no_worker_candidates",
            "detail": f"{rule.rule}: no worker candidate at class {rule.class_id}"
            + (f" in lane {rule.lane}" if rule.lane else ""),
            "preference": preference_record(task, eligible, snapshot, None),
        })

    everything = [*pool.candidates, *pool.potential]
    if not any(launchable(c, snapshot) for c in everything):
        return PlanResult("held", {
            "task_id": task.task_id,
            "candidates": [c.as_dict() for c in pool.candidates],
            "providers": sorted({c.provider for c in everything}),
            "preference": preference_record(task, pool.candidates, snapshot, None),
        })

    if not known:
        questions: list[str] = []
        if not task.task_type and not rule.hint:
            questions += ["task_type", "intelligence_class"]
        wanted: set[str] = set()
        potential = [c for c in pool.potential if launchable(c, snapshot)]
        if rule.narrow and potential:
            lanes = {c.lane for c in potential}
            wanted |= {flag for name in lanes for flag in policy.lanes[name].requires}
            if any(policy.lanes[name].max_risk is not None for name in lanes):
                wanted.add("risk")
        if _risk_could_change_route(policy, rule, [*pool.candidates, *potential]):
            wanted.add("risk")
            wanted |= {
                flag for risk_rule in policy.risk.values() if risk_rule.relax is not None
                for flag in risk_rule.relax.requires
            }
        questions += sorted(wanted)
        if questions:
            return PlanResult("needs_classification", {
                "task_id": task.task_id,
                "title": task.title,
                "description": task.description,
                "task_type": task.task_type or "",
                "class_hint": task.class_hint or "",
                "questions": questions,
                "allowed_kinds": allowed_kinds(policy),
                "allowed_classes": list(policy.class_order),
            })

    selection = reselect(pool.candidates, snapshot, policy.balance)
    if selection is None:
        # Only the unclassified narrow tier was launchable, and it is not
        # needed: the general candidates are all on unlaunchable providers.
        return PlanResult("held", {
            "task_id": task.task_id,
            "candidates": [c.as_dict() for c in pool.candidates],
            "providers": sorted({c.provider for c in pool.candidates}),
            "preference": preference_record(task, pool.candidates, snapshot, None),
        })

    if classified is not None:
        classification_record: dict[str, Any] | None = classified.as_dict()
    elif failed:
        classification_record = {"failed": True, **({"error": error} if error else {})}
    else:
        classification_record = None
    candidates = [
        {**c.as_dict(), "launchable": launchable(c, snapshot)} for c in pool.candidates
    ]
    preference = preference_record(task, pool.candidates, snapshot, selection)
    evidence = selection_evidence(
        pool.candidates, selection, snapshot, prefer_harnesses=rule.prefer_harnesses,
        preference=preference,
    )
    return PlanResult("planned", {
        "task_id": task.task_id,
        "intelligence_class": selection.chosen.intelligence_class,
        "profile_id": selection.chosen.profile_id,
        "provider": selection.chosen.provider,
        "provider_intent": selection.provider_intent,
        "task_type": rule.kind,
        "lane": selection.chosen.lane,
        "rule": rule.rule,
        "candidates": candidates,
        "scores": [s.as_dict() for s in selection.scores],
        "reason": _reason(rule, pool, selection) + "; " + selection_reason(evidence),
        "decision": evidence,
        "prefer_harnesses": list(rule.prefer_harnesses),
        "preference": preference,
        "policy_sha256": policy_sha256,
        "class_clamped_from": rule.clamped_from,
        # Present only when a risk floor raised the class, so a plan without
        # one is the plan it was before risks existed.
        **({"class_raised_for_risk": {
            "from": rule.raised_from, "to": rule.class_id, "risk": rule.raised_for_risk,
            **({"assumed": True} if rule.risk_assumed else {}),
        }} if rule.raised_from is not None else {}),
        "classification": classification_record,
        "balance": policy.balance.model_dump(mode="json"),
    })


__all__ = [
    "FALLBACK",
    "PREFERRED",
    "PREFER_MODES",
    "PREFER_SOFT",
    "PREFER_STRICT",
    "ROUTABLE_SOURCES",
    "ROUTED_SOURCES",
    "UNLAUNCHABLE_STATES",
    "Candidate",
    "Classification",
    "PlanResult",
    "ProfileFacts",
    "ProviderFacts",
    "Score",
    "Selection",
    "Snapshot",
    "TaskFacts",
    "allowed_kinds",
    "is_candidate",
    "launchable",
    "plan_route",
    "prefer_mode",
    "prefer_target",
    "preference_record",
    "read_classification",
    "reselect",
    "score",
    "serves_preference",
    "worker_classes",
]
