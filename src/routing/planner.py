"""Deterministic route selection (mandatory-routing spec §6.4).

Every function here is a pure function of the task's facts, the policy
(:mod:`src.routing.policy`), an optional classification and one capacity
:class:`Snapshot`.  Nothing reads the database, the clock or a provider:
``task_route_plan`` builds the snapshot and calls :func:`plan_route`, and
``task_route_apply`` re-runs steps 3, 5 and 6 on a fresh snapshot through
:func:`reselect`.

The steps, as the spec numbers them:

1. **Kind and class** — the filer's hint beats the classification, which
   beats the kind's default; ``max_class`` clamps.
2. **Candidates** — worker candidates for the lane (or the class), minus
   reserved cells, excluded providers, providers other than the project's
   ``preferred_provider`` and, for a task needing a non-pool workspace, every
   pool profile.  A narrow lane's candidates form the preferred tier.
3. **Availability** — an unlaunchable provider is dropped from the choice
   but stays in ``candidates``; nothing launchable is ``held``.
4. **Classification needed?** — only when the answer could change the route.
5. **Load score** — ``pressure = (load + 1) / (slots × weight × usage ×
   availability)``.
6. **Choice** — a preferred-tier profile with a free slot, else the lowest
   pressure overall; ties go to candidate order.
7. **Reason** — one sentence naming what decided.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from src.models import TaskType
from src.profiles.catalog import worker_route
from src.routing.policy import CLASSIFICATION_FLAGS, Balance, Lane, RoutingPolicy
from src.routing.sources import LEGACY, OVERRIDE, ROLE, ROUTER, UNROUTED

#: A route the router must still write (§6.1).  ``legacy`` is re-routed.
ROUTABLE_SOURCES: frozenset[str] = frozenset({UNROUTED, LEGACY})
#: A route the router leaves alone: ``task_route_plan`` answers ``already_routed``.
ROUTED_SOURCES: frozenset[str] = frozenset({ROUTER, OVERRIDE, ROLE})
#: ``provider_availability`` states nothing may launch against (§6.4 step 3).
UNLAUNCHABLE_STATES: frozenset[str] = frozenset(
    {"exhausted", "unauthenticated", "failing", "disabled"}
)

PREFERRED = "preferred"
FALLBACK = "fallback"

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
    lifecycle: str = "task"
    default_class: str = ""
    classes: frozenset[str] = frozenset()
    slots: int = 0
    enabled: bool = True
    template: bool = False
    read_only: bool = False
    runtime: str = ""
    needs_workspace: bool = True


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
    #: Routed backlog per profile (``count_routed_backlog_by_profile``).
    backlog: Mapping[str, int] = field(default_factory=dict)
    #: Bounded observational context; never a capacity reservation or policy input.
    context: Mapping[str, Any] = field(default_factory=dict)

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
    #: The task needs a workspace kind other than ``project-repo``/``vault``,
    #: which keeps it away from pools (``_claim_preparation_predicates``).
    needs_task_lifecycle: bool = False
    #: ``benchmark:<arm>`` task labels; multiple selectors are an error.
    benchmark_arms: tuple[str, ...] = ()


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
    load: int
    usage_percent: float | None
    usage_factor: float
    avail_factor: float
    weight: float
    pressure: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Selection:
    """Steps 3, 5 and 6 over one candidate list."""

    chosen: Candidate
    scores: tuple[Score, ...]
    #: The preferred tier had a free slot and the choice came from it.
    took_preferred: bool

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "intelligence_class": self.intelligence_class,
            **{flag: flag in self.flags for flag in sorted(CLASSIFICATION_FLAGS)},
            "reason": self.reason,
        }


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
    and an answer outside ``allowed_kinds`` / ``allowed_classes`` are a failed
    classification (§6.5): the plan proceeds with the defaults and treats
    every ``requires`` flag as false.
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
    reason = raw.get("reason") or ""
    return (
        Classification(str(task_type), str(class_id), frozenset(flags), str(reason)[:400]),
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
    notes: tuple[str, ...]


def _rule(task: TaskFacts, policy: RoutingPolicy, classified: Classification | None) -> _Rule:
    """Step 1: the kind, the merged rule and the class."""
    notes: list[str] = []
    kind = task.task_type or (classified.task_type if classified else None) or policy.default_kind
    base = policy.kinds.get(kind)
    rule_name = f"kinds.{kind}"
    if base is None:
        notes.append(f"kind {kind} has no rule, used {policy.default_kind}")
        base = policy.kinds[policy.default_kind]
        rule_name = f"kinds.{policy.default_kind}"
    class_id, max_class, lane, narrow = base.class_, base.max_class, base.lane, base.narrow
    origin_name = (task.created_by_kind or "").strip() or None
    origin = policy.origins.get(origin_name) if origin_name else None
    if origin is not None:
        rule_name += f"+origins.{origin_name}"
        class_id = origin.class_ if origin.class_ is not None else class_id
        max_class = origin.max_class if origin.max_class is not None else max_class
        lane = origin.lane if origin.lane is not None else lane
        narrow = origin.narrow if origin.narrow is not None else narrow
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
    return _Rule(
        kind=kind, rule=rule_name, origin=origin_name, class_id=chosen, hint=hint,
        clamped_from=clamped_from, lane=lane, narrow=narrow, notes=tuple(notes),
    )


def _cells(
    snapshot: Snapshot,
    class_id: str,
    harnesses: frozenset[str] | None,
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[ProfileFacts]:
    return [
        profile for profile in snapshot.profiles
        if class_id in worker_classes(profile)
        and (harnesses is None or profile.harness in harnesses)
        and profile.harness not in exclude
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


def _filter(
    candidates: list[Candidate], task: TaskFacts, policy: RoutingPolicy,
    *, respect_reserved: bool = True,
) -> tuple[list[Candidate], str | None]:
    """Step 2's removals, returning the survivors and why the list emptied."""
    stages = (
        ("reserved", lambda c: not respect_reserved or not _reserved_away(policy, c)),
        ("excluded_providers", lambda c: c.provider not in task.exclude_providers),
        (
            "preferred_provider_unavailable",
            lambda c: not task.preferred_provider or c.provider == task.preferred_provider,
        ),
        ("workspace_requirement", lambda c: not task.needs_task_lifecycle or c.lifecycle != "pool"),
    )
    reason = None if candidates else "no_worker_candidates"
    for name, keep in stages:
        kept = [candidate for candidate in candidates if keep(candidate)]
        if candidates and not kept:
            reason = name
        candidates = kept
    return candidates, reason


def _candidate(profile: ProfileFacts, class_id: str, *, tier: str, lane: str | None,
               hold: bool = False) -> Candidate:
    return Candidate(
        profile_id=profile.id, intelligence_class=class_id, harness=profile.harness,
        provider=profile.provider, lifecycle=profile.lifecycle, tier=tier, lane=lane, hold=hold,
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
) -> _Pool:
    """Step 2.  *flags* ``None`` means the classification is still unknown."""
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
        found, reason = _filter(found, task, policy)
        ordered = sorted(found, key=_order_key(policy, lane))
        preferred = [c for c in ordered if c.tier == PREFERRED]
        fallback = [c for c in ordered if c.tier == FALLBACK]
        return _Pool(tuple(preferred + fallback), (), reason)

    general = [
        _candidate(profile, rule.class_id, tier=FALLBACK, lane=None)
        for profile in _cells(
            snapshot, rule.class_id, None, exclude=policy.narrow_harnesses()
        )
    ]
    general, reason = _filter(general, task, policy)
    general.sort(key=_order_key(policy, None))

    preferred: list[Candidate] = []
    potential: list[Candidate] = []
    if rule.narrow:
        for name, lane in policy.narrow_lanes():
            mapped = (lane.classes or {}).get(rule.class_id)
            if mapped is None:
                continue
            lane_cells = [
                _candidate(profile, mapped, tier=PREFERRED, lane=name)
                for profile in _cells(snapshot, mapped, frozenset(lane.harnesses))
            ]
            lane_cells, _reason = _filter(lane_cells, task, policy)
            lane_cells.sort(key=_order_key(policy, lane))
            if flags is None:
                potential.extend(lane_cells)
            elif set(lane.requires) <= flags:
                preferred.extend(lane_cells)
    seen: set[tuple[str, str]] = set()
    ordered: list[Candidate] = []
    for candidate in preferred + general:
        key = (candidate.profile_id, candidate.intelligence_class)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return _Pool(tuple(ordered), tuple(potential), None if ordered else reason)


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
    usage_factor = _usage_factor(facts.usage_percent, balance)
    avail_factor = balance.degraded_factor if facts.state == "degraded" else 1.0
    weight = balance.weight(candidate.harness)
    pressure = (busy + backlog + 1) / (slots * weight * usage_factor * avail_factor)
    return Score(
        profile_id=candidate.profile_id, intelligence_class=candidate.intelligence_class,
        tier=candidate.tier, slots=slots, busy=busy, backlog=backlog, load=busy + backlog,
        usage_percent=facts.usage_percent, usage_factor=round(usage_factor, 4),
        avail_factor=avail_factor, weight=weight, pressure=round(pressure, 4),
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
    pool = free_preferred or scored
    _index, chosen, _score = min(pool, key=lambda entry: (entry[2].pressure, entry[0]))
    return Selection(
        chosen=chosen,
        scores=tuple(entry[2] for entry in scored),
        took_preferred=bool(free_preferred),
    )


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
    if selection.took_preferred:
        decided = f"preferred {chosen.profile_id} {selection.score.pressure:.2f}"
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
            for profile in _cells(snapshot, arm.class_, frozenset({arm.harness}))
        ]
        candidates, reason = _filter(candidates, task, policy, respect_reserved=False)
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
            "reason": f"allowlisted benchmark arm {name}: {arm.class_}/{arm.harness}",
            "policy_sha256": policy_sha256,
            "classification": None,
            "balance": policy.balance.model_dump(mode="json"),
        })
    classified, failed, error = read_classification(classification, policy)
    known = classification is not None
    flags = (classified.flags if classified else frozenset()) if known else None
    rule = _rule(task, policy, classified)
    pool = _candidates(task, policy, snapshot, rule, flags)

    if not pool.candidates and not pool.potential:
        return PlanResult("no_candidates", {
            "task_id": task.task_id,
            "reason": pool.reason or "no_worker_candidates",
            "detail": f"{rule.rule}: no worker candidate at class {rule.class_id}"
            + (f" in lane {rule.lane}" if rule.lane else ""),
        })

    everything = [*pool.candidates, *pool.potential]
    if not any(launchable(c, snapshot) for c in everything):
        return PlanResult("held", {
            "task_id": task.task_id,
            "candidates": [c.as_dict() for c in pool.candidates],
            "providers": sorted({c.provider for c in everything}),
        })

    if not known:
        questions: list[str] = []
        if not task.task_type and not rule.hint:
            questions += ["task_type", "intelligence_class"]
        if rule.narrow and any(launchable(c, snapshot) for c in pool.potential):
            lanes = {c.lane for c in pool.potential if launchable(c, snapshot)}
            wanted = {flag for name in lanes for flag in policy.lanes[name].requires}
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
        "reason": _reason(rule, pool, selection),
        "policy_sha256": policy_sha256,
        "class_clamped_from": rule.clamped_from,
        "classification": classification_record,
        "balance": policy.balance.model_dump(mode="json"),
    })


__all__ = [
    "FALLBACK",
    "PREFERRED",
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
    "read_classification",
    "reselect",
    "score",
    "worker_classes",
]
