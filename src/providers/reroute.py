"""The re-route engine: move queued work off an unavailable provider (provider-failover D11-D17).

Moving a task is **policy** (D11), so nothing here runs on its own: the
``provider_reroute`` command drives :meth:`ProviderRerouteService.sweep`, and
the shipped ``provider-failover`` playbook calls that command on
``provider.state_changed`` and every five minutes.  If the playbook is not
active, tasks hold with a visible reason and nothing moves -- the safe failure.

Two halves, in the house style:

* :func:`plan_sweep` is **pure**: candidates plus a :class:`PlanContext`
  snapshot in, one :class:`Decision` per candidate out.  It owns every rule
  in D12 (same class, next provider, or hold), D14 (only an ``available``
  provider is a target; no bound changes) and D15 (the trickle and the
  per-sweep / per-task limits), and is what ``dry_run`` returns verbatim.
* :class:`ProviderRerouteService` gathers the snapshot, applies a plan under
  the ``update_task_routing`` guard (a claim that wins after the read is never
  retargeted), records ``task_reroutes`` / ``rerouted_from`` / a task comment
  (D17), emits ``task.rerouted`` and ``provider.reroute_batch`` and sends one
  notice per batch per project (D19).  It also answers the two derived
  questions other code asks: why a task is held (D18) and which profile a
  project's default resolves to while its provider is down (D13).

The class never changes on an automatic move.  Intent never changes on any
move (D16).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from src.providers.availability import AVAILABLE, DEGRADED, UNAVAILABLE
from src.providers.intent import CLASS_ONLY, PINNED, effective_intent

logger = logging.getLogger(__name__)

__all__ = [
    "FAILOVER_PLAYBOOK_ID",
    "HOLD_KINDS",
    "Candidate",
    "Decision",
    "PlanContext",
    "ProviderRerouteService",
    "Rung",
    "batch_id_for",
    "plan_sweep",
    "provider_order",
]

FAILOVER_PLAYBOOK_ID = "provider-failover"

#: Why a queued task on an unavailable provider is not moving (D18).
#: ``priority_policy_hold`` extends the spec's list for ``reroute.max_priority_value``.
HOLD_KINDS = (
    "provider_pinned",
    "class_policy_hold",
    "no_equivalent_rung",
    "no_available_target",
    "awaiting_failover_capacity",
    "reroute_limit_reached",
    "all_providers_unavailable",
    "failover_inactive",
    "priority_policy_hold",
)

#: Where the system-authored re-route comments and notices come from.
AUTHOR_ID = "system:provider-failover"
NOTICE_FROM = ("system", "playbook:provider-failover")
#: How long the "is the playbook active" answer is cached.
_ACTIVE_TTL_SECONDS = 60.0
#: Automatic moves: ``prb-<provider>-<generation>`` (one outage, one batch).
#: Operator-forced moves get their own ``prf-`` batch so they undo together.
AUTO_BATCH_PREFIX = "prb"
FORCED_BATCH_PREFIX = "prf"


def batch_id_for(provider: str, generation: int) -> str:
    """The batch every automatic move during one outage belongs to (D19)."""
    return f"{AUTO_BATCH_PREFIX}-{provider}-{int(generation)}"


# -- the snapshot the planner reads ---------------------------------------------


@dataclass(frozen=True)
class Rung:
    """A worker profile that could stand in for another on its ``(harness, class)``."""

    profile_id: str
    harness: str
    class_id: str
    provider: str
    lifecycle: str = "task"
    enabled: bool = True
    capacity: int = 1


@dataclass(frozen=True)
class Candidate:
    """A queued task the sweep may move, as the planner sees it."""

    task_id: str
    project_id: str
    profile_id: str
    priority: int = 100
    created_at: float = 0.0
    status: str = "READY"
    title: str = ""
    intelligence_class: str | None = None
    intent: str = CLASS_ONLY
    #: ``task_metadata['provider_pause']`` when the task was paused by a
    #: provider failure (D17); such a task returns to READY once its
    #: provider has tripped.
    provider_pause: Mapping[str, Any] | None = None
    #: PAUSED on an automatic backoff with no recorded cause: touched only
    #: with ``include_paused`` (D13, the tasks paused before this shipped).
    legacy_pause: bool = False


@dataclass
class Decision:
    """What the sweep does with one candidate."""

    task_id: str
    project_id: str
    from_profile_id: str
    from_provider: str
    provider_state: str
    action: str  # "move" | "hold" | "skip"
    kind: str | None = None
    to_profile_id: str | None = None
    to_provider: str | None = None
    #: Set only when a forced cross-class move changes the class.
    to_class: str | None = None
    ahead: int | None = None
    detail: str = ""
    resume: bool = False
    intelligence_class: str | None = None
    intent: str = CLASS_ONLY
    priority: int = 100
    title: str = ""
    status: str = ""
    provider_generation: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanContext:
    """Everything :func:`plan_sweep` reads, gathered once per sweep."""

    #: Every worker profile with a class slice for its harness, by id.
    rungs: Mapping[str, Rung]
    #: Every profile id -> the provider key it launches against.
    profile_providers: Mapping[str, str]
    #: Provider key -> effective state; a missing key is ``available``.
    states: Mapping[str, str]
    #: Provider key -> generation (keys the batch).
    generations: Mapping[str, int] = field(default_factory=dict)
    #: Moved-and-not-yet-started tasks per target profile (D15).
    backlog: Mapping[str, int] = field(default_factory=dict)
    #: ``task_reroute_stats`` per task id.
    stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Project id -> the provider of its raw default profile.
    default_providers: Mapping[str, str] = field(default_factory=dict)
    #: Providers some enabled profile launches against (``llm`` excluded).
    session_providers: frozenset[str] = frozenset()
    config: Any = None
    now: float = 0.0

    def state(self, provider: str) -> str:
        return self.states.get(provider, AVAILABLE) if provider else AVAILABLE

    def unavailable(self, provider: str) -> bool:
        return self.state(provider) in UNAVAILABLE


def _worker_provider_order() -> tuple[str, ...]:
    from src.profiles.catalog import WORKER_PROVIDERS

    return tuple(harness for harness, _title, _vendor in WORKER_PROVIDERS)


def provider_order(ctx: PlanContext, project_id: str | None) -> list[str]:
    """Failover target preference (D12): ``order``, else the project default's
    provider first, then ``WORKER_PROVIDERS`` order, then any other provider."""
    configured = list(getattr(ctx.config, "order", None) or [])
    if configured:
        seed = configured
    else:
        seed = [ctx.default_providers.get(project_id or "", "")] + list(_worker_provider_order())
    extra = sorted({rung.provider for rung in ctx.rungs.values()} - set(seed))
    order: list[str] = []
    for provider in [*seed, *extra]:
        if provider and provider not in order:
            order.append(provider)
    return order


def _rung_rank(rung: Rung) -> tuple:
    from src.profiles.catalog import rung_profile_id

    return (
        rung.profile_id != rung_profile_id(rung.class_id, rung.harness),
        rung.lifecycle != "pool",
        rung.profile_id,
    )


def equivalent_rung(
    ctx: PlanContext,
    class_id: str,
    provider: str,
    *,
    allow_degraded: bool = False,
) -> tuple[Rung | None, bool]:
    """``(target, exists)`` for *class_id* on *provider*.

    ``exists`` says an enabled rung is there at all; ``target`` is that rung
    only when its provider may receive failover traffic -- ``available``, or
    ``degraded`` when allowed (D14).  A disabled pool is never a target.
    """
    rungs = [
        rung
        for rung in ctx.rungs.values()
        if rung.enabled and rung.class_id == class_id and rung.provider == provider
    ]
    if not rungs:
        return None, False
    state = ctx.state(provider)
    if state == AVAILABLE or (allow_degraded and state == DEGRADED):
        return min(rungs, key=_rung_rank), True
    return None, True


def _hold(decision: Decision, kind: str, detail: str = "", ahead: int | None = None) -> None:
    decision.action = "hold"
    decision.kind = kind
    decision.detail = detail
    decision.ahead = ahead


def _policy(ctx: PlanContext, class_id: str) -> str:
    classes = getattr(ctx.config, "classes", None) or {}
    return str(classes.get(class_id) or getattr(ctx.config, "default_policy", "same_class"))


def plan_sweep(
    candidates: Sequence[Candidate],
    ctx: PlanContext,
    *,
    force: bool = False,
    to_profile: str | None = None,
    include_paused: bool = False,
    explicit: bool = False,
) -> list[Decision]:
    """One decision per candidate, in claim order (``priority, created_at``).

    *force* (an operator's ``--force``) may move a pinned task, target a
    degraded provider, change the class with *to_profile*, and skips the
    trickle and the per-task limits.  *explicit* means the operator named the
    tasks, so one whose provider is launchable is moved only with
    *to_profile* and otherwise reported ``skip``.
    """
    cfg = ctx.config
    reroute = getattr(cfg, "reroute", None)
    allow_degraded = bool(getattr(reroute, "allow_degraded_target", False)) or force
    max_per_sweep = int(getattr(reroute, "max_per_sweep", 10) or 10)
    factor = float(getattr(reroute, "target_backlog_factor", 1.0) or 1.0)
    cooldown = float(getattr(reroute, "task_cooldown_seconds", 1800) or 0)
    max_auto = int(getattr(reroute, "max_auto_per_task", 2) or 2)
    max_priority = getattr(reroute, "max_priority_value", None)
    everything_down = bool(ctx.session_providers) and all(
        ctx.unavailable(p) for p in ctx.session_providers
    )
    backlog = dict(ctx.backlog)
    waiting: dict[str, int] = {}
    moves = 0
    decisions: list[Decision] = []
    ordered = sorted(candidates, key=lambda c: (c.priority, c.created_at, c.task_id))
    for cand in ordered:
        provider = ctx.profile_providers.get(cand.profile_id, "")
        state = ctx.state(provider)
        rung = ctx.rungs.get(cand.profile_id)
        class_id = (cand.intelligence_class or "").strip() or (rung.class_id if rung else "")
        decision = Decision(
            task_id=cand.task_id,
            project_id=cand.project_id,
            from_profile_id=cand.profile_id,
            from_provider=provider,
            provider_state=state,
            action="hold",
            intelligence_class=class_id or None,
            intent=cand.intent,
            priority=cand.priority,
            title=cand.title,
            status=cand.status,
            provider_generation=ctx.generations.get(provider),
        )
        decisions.append(decision)
        down = state in UNAVAILABLE
        if cand.legacy_pause and not include_paused:
            decision.action = "skip"
            decision.detail = "paused before provider failover recorded causes; use include_paused"
            continue
        if not down and not (explicit and to_profile):
            decision.action = "skip"
            decision.detail = f"provider {provider or '-'} is {state}"
            continue
        # A provider pause (or, with include_paused, a legacy one) has
        # nothing left to wait out once the provider has tripped (D13).
        decision.resume = down and (cand.provider_pause is not None or cand.legacy_pause)

        if cand.intent == PINNED and not force:
            _hold(decision, "provider_pinned", "pinned to its provider by a human")
            continue
        if not class_id:
            _hold(decision, "no_equivalent_rung", "the task has no intelligence class to match")
            continue
        if _policy(ctx, class_id) == "hold" and not force:
            _hold(decision, "class_policy_hold", f"provider_failover.classes holds {class_id}")
            continue
        if everything_down:
            _hold(decision, "all_providers_unavailable", "every session provider is unavailable")
            continue
        if rung is None and not to_profile:
            _hold(decision, "no_equivalent_rung", f"{cand.profile_id} is not a worker rung")
            continue
        stats = ctx.stats.get(cand.task_id) or {}
        if not force:
            if int(stats.get("auto_count") or 0) >= max_auto:
                _hold(decision, 
                    "reroute_limit_reached",
                    f"moved automatically {stats.get('auto_count')} times; a human decides now",
                )
                continue
            last = stats.get("last_auto_at")
            if last is not None and cooldown and ctx.now - float(last) < cooldown:
                _hold(decision, 
                    "reroute_limit_reached",
                    f"moved automatically {int(ctx.now - float(last))} s ago "
                    f"(cooldown {int(cooldown)} s)",
                )
                continue
            if max_priority is not None and cand.priority > int(max_priority):
                _hold(decision, 
                    "priority_policy_hold",
                    f"priority {cand.priority} is above reroute.max_priority_value {max_priority}",
                )
                continue

        target: Rung | None = None
        if to_profile:
            chosen = ctx.rungs.get(to_profile)
            if chosen is None or not chosen.enabled:
                _hold(decision, "no_available_target", f"{to_profile} is not an enabled worker rung")
                continue
            if chosen.class_id != class_id and not force:
                _hold(decision, 
                    "no_equivalent_rung",
                    f"{to_profile} runs {chosen.class_id}, not {class_id}; "
                    "a cross-class move needs force",
                )
                continue
            target_state = ctx.state(chosen.provider)
            if not (target_state == AVAILABLE or (allow_degraded and target_state == DEGRADED)):
                _hold(decision, "no_available_target", f"provider {chosen.provider} is {target_state}")
                continue
            if chosen.profile_id == cand.profile_id:
                decision.action = "skip"
                decision.detail = f"already on {to_profile}"
                continue
            target = chosen
        else:
            left = set(stats.get("left_providers") or ())
            any_rung = False
            for other in provider_order(ctx, cand.project_id):
                if other == provider or other in left:
                    continue
                found, exists = equivalent_rung(
                    ctx, class_id, other, allow_degraded=allow_degraded
                )
                any_rung = any_rung or exists
                if found is not None:
                    target = found
                    break
            if target is None:
                if any_rung:
                    _hold(decision, 
                        "no_available_target",
                        f"every other provider with a {class_id} rung is unavailable or degraded",
                    )
                else:
                    _hold(decision, 
                        "no_equivalent_rung",
                        f"no other provider has an enabled {class_id} rung",
                    )
                continue

        if not force:
            limit = max(1, math.ceil(factor * max(1, target.capacity)))
            if backlog.get(target.profile_id, 0) >= limit or moves >= max_per_sweep:
                ahead = waiting.get(target.profile_id, 0)
                waiting[target.profile_id] = ahead + 1
                reason = (
                    f"{target.profile_id} already has {backlog.get(target.profile_id, 0)} "
                    f"moved task(s) queued (limit {limit})"
                    if backlog.get(target.profile_id, 0) >= limit
                    else f"this sweep reached reroute.max_per_sweep ({max_per_sweep})"
                )
                _hold(decision, "awaiting_failover_capacity", reason, ahead)
                decision.to_profile_id = target.profile_id
                decision.to_provider = target.provider
                continue
        decision.action = "move"
        decision.kind = None
        decision.to_profile_id = target.profile_id
        decision.to_provider = target.provider
        if target.class_id != class_id:
            decision.to_class = target.class_id
        backlog[target.profile_id] = backlog.get(target.profile_id, 0) + 1
        moves += 1
    return decisions


# -- the service ------------------------------------------------------------------


def _class_has_slice(cls: Any, profile: Any, harness_registry: Any) -> bool:
    """Does *cls* name a model for *profile*'s harness (vendor slice or id slice)?"""
    from src.commands.routing_commands import _class_mapping, profile_provider

    vendor = profile_provider(profile, harness_registry)
    if vendor:
        return _class_mapping(cls, profile, vendor) is not None
    harness_id = str(getattr(profile, "harness", "") or "")
    slice_ = cls.mapping.get(harness_id) if harness_id else None
    return isinstance(slice_, dict) and bool(str(slice_.get("model") or "").strip())


def _json_meta(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class ProviderRerouteService:
    """Plans and applies re-route sweeps; answers holds and derived defaults."""

    def __init__(
        self,
        *,
        db_getter: Callable[[], Any],
        availability: Any,
        config_getter: Callable[[], Any],
        harness_registry: Any = None,
        classes_getter: Callable[[], Mapping[str, Any]] | None = None,
        bus: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db_getter = db_getter
        self.availability = availability
        self._config_getter = config_getter
        self.harness_registry = harness_registry
        self._classes_getter = classes_getter or dict
        self._bus = bus
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active_cache: tuple[float, bool] | None = None

    @property
    def db(self) -> Any:
        return self._db_getter()

    @property
    def config(self) -> Any:
        from src.config import ProviderFailoverConfig

        return getattr(self._config_getter(), "provider_failover", None) or ProviderFailoverConfig()

    def now(self) -> float:
        return float(self._clock())

    # -- is failover live? -------------------------------------------------------

    async def playbook_active(self) -> bool:
        """Is the ``provider-failover`` playbook activated (cached briefly)?"""
        now = self.now()
        cached = self._active_cache
        if cached is not None and now - cached[0] < _ACTIVE_TTL_SECONDS:
            return cached[1]
        active = False
        try:
            rows = await self.db.list_playbook_activations()
            active = any(
                row.get("playbook_id") == FAILOVER_PLAYBOOK_ID
                and row.get("enabled")
                and row.get("active_artifact_sha256")
                for row in rows
            )
        except Exception:  # an unreadable table reads as "not activated"
            logger.debug("provider reroute: activations unreadable", exc_info=True)
        self._active_cache = (now, active)
        return active

    async def failover_inactive_reason(self) -> str | None:
        """Why automatic moves are off, or ``None`` when they are live (D18)."""
        cfg = self.config
        if getattr(cfg, "mode", "enforce") != "enforce":
            return f"provider_failover.mode is {cfg.mode}"
        if not cfg.reroute.enabled:
            return "provider_failover.reroute.enabled is false"
        if not await self.playbook_active():
            return f"the {FAILOVER_PLAYBOOK_ID} playbook is not active"
        return None

    # -- snapshot ---------------------------------------------------------------

    def _classes(self) -> Mapping[str, Any]:
        try:
            classes = self._classes_getter()
        except Exception:  # a class registry mid-reload reads as empty
            logger.debug("provider reroute: classes unreadable", exc_info=True)
            classes = {}
        return classes or {}

    async def context(self, *, profiles: Sequence[Any] | None = None) -> PlanContext:
        """Read everything the planner needs once."""
        from src.profiles.catalog import worker_route

        db = self.db
        availability = self.availability
        profiles = list(profiles if profiles is not None else await db.list_profiles())
        try:
            agents = await db.list_agents()
        except Exception:
            logger.debug("provider reroute: agents unreadable", exc_info=True)
            agents = []
        classes = self._classes()
        profile_providers: dict[str, str] = {}
        rungs: dict[str, Rung] = {}
        session_providers: set[str] = set()
        for profile in profiles:
            if getattr(profile, "template", False) or ":" in str(profile.id):
                continue
            provider = availability.provider_for_profile(profile)
            profile_providers[profile.id] = provider
            enabled = bool(getattr(profile, "enabled", True))
            if enabled and provider and provider != "llm":
                session_providers.add(provider)
            route = worker_route(
                profile.id,
                harness=getattr(profile, "harness", ""),
                default_class=getattr(profile, "default_class", ""),
                lifecycle=getattr(profile, "lifecycle", "task"),
                template=bool(getattr(profile, "template", False)),
                read_only=bool(getattr(profile, "read_only", False)),
            )
            if route is None:
                continue
            harness, class_id = route
            cls = classes.get(class_id)
            if cls is None or not _class_has_slice(cls, profile, self.harness_registry):
                continue
            lifecycle = str(getattr(profile, "lifecycle", "task") or "task")
            if lifecycle == "pool":
                capacity = int(getattr(profile, "max_active", 0) or 0)
            else:
                capacity = sum(
                    1
                    for agent in agents
                    if agent.profile_id == profile.id
                    and agent.enabled
                    and getattr(agent, "deleted_at", None) is None
                )
            rungs[profile.id] = Rung(
                profile_id=profile.id,
                harness=harness,
                class_id=class_id,
                provider=provider,
                lifecycle=lifecycle,
                enabled=enabled,
                capacity=max(1, capacity),
            )
        now = self.now()
        states: dict[str, str] = {}
        generations: dict[str, int] = {}
        for provider in set(profile_providers.values()) | set(availability.rows()):
            if not provider:
                continue
            states[provider] = availability.effective_state(provider, now)
            row = availability.row(provider)
            generations[provider] = int(getattr(row, "generation", 0) or 0)
        default_providers: dict[str, str] = {}
        try:
            for project in await db.list_projects():
                default = getattr(project, "default_profile_id", None)
                if default and default in profile_providers:
                    default_providers[project.id] = profile_providers[default]
        except Exception:
            logger.debug("provider reroute: projects unreadable", exc_info=True)
        try:
            backlog = await db.count_rerouted_queued_by_profile()
        except Exception:
            logger.debug("provider reroute: backlog unreadable", exc_info=True)
            backlog = {}
        return PlanContext(
            rungs=rungs,
            profile_providers=profile_providers,
            states=states,
            generations=generations,
            backlog=backlog,
            default_providers=default_providers,
            session_providers=frozenset(session_providers),
            config=self.config,
            now=now,
        )

    async def _candidates(
        self,
        ctx: PlanContext,
        *,
        providers: Iterable[str] | None,
        task_ids: Sequence[str] | None,
    ) -> list[Candidate]:
        wanted = set(providers) if providers is not None else None
        if task_ids is not None:
            profile_ids: list[str] = []
        else:
            profile_ids = [
                pid
                for pid, provider in ctx.profile_providers.items()
                if (wanted is None or provider in wanted) and ctx.unavailable(provider)
            ]
            if not profile_ids:
                return []
        rows = await self.db.list_reroute_candidates(profile_ids, task_ids=task_ids)
        rows = [row for row in rows if row.get("profile_id")]
        ids = [row["id"] for row in rows]
        pauses = await self.db.get_task_meta_bulk(ids, "provider_pause") if ids else {}
        out: list[Candidate] = []
        for row in rows:
            pause = _json_meta(pauses.get(row["id"])) if row["id"] in pauses else None
            paused = row.get("status") == "PAUSED"
            if paused and row.get("resume_after") is None:
                # An operator hold never moves (``resume_task`` owns it).
                continue
            if paused and not isinstance(pause, Mapping):
                pause = None
            intent = effective_intent(
                type("Row", (), {"provider_intent": row.get("provider_intent"),
                                  "profile_id": row.get("profile_id")})()
            )
            out.append(
                Candidate(
                    task_id=row["id"],
                    project_id=row["project_id"],
                    profile_id=row["profile_id"],
                    priority=int(row.get("priority") or 100),
                    created_at=float(row.get("created_at") or 0.0),
                    status=str(row.get("status") or ""),
                    title=str(row.get("title") or ""),
                    intelligence_class=row.get("intelligence_class"),
                    intent=intent,
                    provider_pause=pause if paused else None,
                    legacy_pause=paused and pause is None,
                )
            )
        return out

    # -- the sweep ---------------------------------------------------------------

    async def sweep(
        self,
        *,
        provider: str | None = None,
        task_ids: Sequence[str] | None = None,
        to_profile: str | None = None,
        include_paused: bool = False,
        dry_run: bool = False,
        force: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Plan one sweep under D12-D16 and, unless *dry_run*, apply it.

        Outcomes: ``rerouted`` (moved at least one task), ``held`` (nothing
        moved, something held), ``idle`` (nothing unavailable, or nothing
        queued on it) and ``disabled`` (mode is not ``enforce`` or re-routing
        is off -- the plan is still returned, applied never).  An operator
        call naming tasks is honoured whatever the mode.
        """
        cfg = self.config
        explicit = task_ids is not None
        operator = explicit and (force or bool(to_profile))
        forced_batch = f"{FORCED_BATCH_PREFIX}-{uuid.uuid4().hex[:12]}" if operator else None
        disabled_reason = None
        if not operator:
            if cfg.mode == "off":
                disabled_reason = "provider_failover.mode is off"
            elif cfg.mode != "enforce":
                disabled_reason = f"provider_failover.mode is {cfg.mode} (plan only)"
            elif not cfg.reroute.enabled:
                disabled_reason = "provider_failover.reroute.enabled is false"
        async with self._lock:
            ctx = await self.context()
            providers = [provider] if provider else None
            candidates = await self._candidates(ctx, providers=providers, task_ids=task_ids)
            if candidates:
                ctx.stats = await self.db.task_reroute_stats([c.task_id for c in candidates])
            decisions = plan_sweep(
                candidates,
                ctx,
                force=force,
                to_profile=to_profile,
                include_paused=include_paused,
                explicit=explicit,
            )
            apply = not dry_run and disabled_reason is None and cfg.mode != "off"
            moved: list[Decision] = []
            lost: list[Decision] = []
            resumed: list[str] = []
            if apply:
                for decision in decisions:
                    if decision.resume and await self._resume(decision):
                        resumed.append(decision.task_id)
                    if decision.action != "move":
                        continue
                    if await self._move(decision, actor=actor, forced_batch=forced_batch):
                        moved.append(decision)
                    else:
                        lost.append(decision)
        held = [d for d in decisions if d.action == "hold"]
        unavailable = sorted(p for p, s in ctx.states.items() if s in UNAVAILABLE and p != "llm")
        if disabled_reason is not None:
            outcome = "disabled"
        elif moved or (not apply and any(d.action == "move" for d in decisions)):
            outcome = "rerouted"
        elif held:
            outcome = "held"
        else:
            outcome = "idle"
        notices: list[str] = []
        if apply and (moved or held):
            notices = await self._announce(moved, held, forced_batch=forced_batch)
        held_by_kind: dict[str, int] = {}
        for decision in held:
            held_by_kind[decision.kind or "-"] = held_by_kind.get(decision.kind or "-", 0) + 1
        return {
            "success": True,
            "outcome": outcome,
            "dry_run": bool(dry_run),
            "applied": apply,
            "disabled_reason": disabled_reason,
            "unavailable_providers": unavailable,
            "moved": [d.to_dict() for d in (moved if apply else
                                            [d for d in decisions if d.action == "move"])],
            "held": [d.to_dict() for d in held],
            "held_by_kind": held_by_kind,
            "resumed": resumed,
            "lost": [d.task_id for d in lost],
            "skipped": [d.to_dict() for d in decisions if d.action == "skip"],
            "batch_ids": sorted({self._batch(d, forced_batch) for d in moved}),
            "notices": notices,
        }

    @staticmethod
    def _batch(decision: Decision, forced_batch: str | None) -> str:
        if forced_batch:
            return forced_batch
        return batch_id_for(decision.from_provider, decision.provider_generation or 0)

    async def _resume(self, decision: Decision) -> bool:
        """``PAUSED -> READY`` for a provider-paused task whose provider tripped (D13)."""
        from src.models import TaskStatus

        try:
            await self.db.transition_task(
                decision.task_id,
                TaskStatus.READY,
                context="provider_failover_resume",
                assigned_agent_id=None,
                resume_after=None,
            )
        except Exception:
            logger.warning("provider reroute: could not resume %s", decision.task_id, exc_info=True)
            return False
        try:
            await self.db.delete_task_meta(decision.task_id, "provider_pause")
        except Exception:
            logger.debug("provider reroute: provider_pause not cleared", exc_info=True)
        decision.status = "READY"
        return True

    async def _move(
        self,
        decision: Decision,
        *,
        actor: str,
        forced_batch: str | None,
    ) -> bool:
        from src.database.queries.task_reroute_queries import AUTOMATIC_REASON

        batch = self._batch(decision, forced_batch)
        reason_code = "operator_forced" if forced_batch else AUTOMATIC_REASON
        record = {
            "project_id": decision.project_id,
            "from_profile_id": decision.from_profile_id,
            "to_profile_id": decision.to_profile_id,
            "from_provider": decision.from_provider,
            "to_provider": decision.to_provider or "",
            "intelligence_class": decision.intelligence_class,
            "reason_code": reason_code,
            "provider_state": decision.provider_state,
            "provider_generation": decision.provider_generation,
            "batch_id": batch,
            "actor": actor,
        }
        kwargs: dict[str, Any] = {}
        if decision.to_class:
            kwargs["intelligence_class"] = decision.to_class
        try:
            reroute_id = await self.db.apply_task_reroute(
                decision.task_id,
                expected_profile_id=decision.from_profile_id,
                to_profile_id=str(decision.to_profile_id),
                record=record,
                **kwargs,
            )
        except Exception:
            logger.warning("provider reroute: could not move %s", decision.task_id, exc_info=True)
            return False
        if reroute_id is None:
            decision.detail = "a worker took the task (or it changed) before the move landed"
            return False
        if decision.to_provider != decision.from_provider:
            # The carried conversation id belongs to the old CLI (bold-rapids.4
            # carries it so a same-provider relaunch resumes); a harness with
            # no transcript reader would hand it to the new CLI unchecked.  The
            # branch and the hand-off note are what move with the task.
            try:
                await self.db.delete_task_meta(decision.task_id, "session_resume_key")
            except Exception:
                logger.debug("provider reroute: resume key not cleared", exc_info=True)
        await self._comment(decision, batch, reason_code, actor)
        await self._emit(
            "task.rerouted",
            {
                "task_id": decision.task_id,
                "project_id": decision.project_id,
                "title": decision.title,
                "from_profile_id": decision.from_profile_id,
                "to_profile_id": decision.to_profile_id,
                "from_provider": decision.from_provider,
                "to_provider": decision.to_provider,
                "reason_code": reason_code,
                "batch_id": batch,
                "actor": actor,
            },
        )
        return True

    async def _comment(self, decision: Decision, batch: str, reason_code: str, actor: str) -> None:
        if reason_code == "operator_forced":
            body = (
                f"Re-routed from `{decision.from_profile_id}` to `{decision.to_profile_id}` "
                f"by {actor} (forced; batch `{batch}`)."
            )
        else:
            since = ""
            row = self.availability.row(decision.from_provider)
            if row is not None:
                started = row.effective_since(self.now())
                if started:
                    since = " since " + time.strftime("%H:%M", time.localtime(float(started)))
            body = (
                f"Re-routed from `{decision.from_profile_id}` to `{decision.to_profile_id}`: "
                f"provider {decision.from_provider} {decision.provider_state}{since} "
                f"(batch `{batch}`). Undo: `aq provider reroute-undo --task-id {decision.task_id}`."
            )
        if decision.to_class:
            body += f" Intelligence class changed to `{decision.to_class}`."
        try:
            await self.db.add_task_comment(
                decision.task_id, body, author_kind="supervisor", author_id=AUTHOR_ID
            )
        except Exception:
            logger.debug("provider reroute: comment failed", exc_info=True)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._bus is not None:
            try:
                await self._bus.emit(event_type, dict(payload))
            except Exception:
                logger.debug("%s emit failed", event_type, exc_info=True)
        try:
            await self.db.log_event(event_type, payload=json.dumps(payload, default=str))
        except Exception:
            logger.debug("%s log_event failed", event_type, exc_info=True)

    async def _announce(
        self,
        moved: Sequence[Decision],
        held: Sequence[Decision],
        *,
        forced_batch: str | None,
    ) -> list[str]:
        """One ``provider.reroute_batch`` per batch that moved or newly held, and
        one supervisor notice per batch per project (D19)."""
        by_batch: dict[str, dict[str, list[Decision]]] = {}
        for decision in moved:
            batch = self._batch(decision, forced_batch)
            by_batch.setdefault(batch, {"moved": [], "held": []})["moved"].append(decision)
        if not forced_batch:
            for decision in held:
                if not decision.from_provider:
                    continue
                batch = batch_id_for(decision.from_provider, decision.provider_generation or 0)
                by_batch.setdefault(batch, {"moved": [], "held": []})["held"].append(decision)
        sent: list[str] = []
        for batch, parts in sorted(by_batch.items()):
            projects: dict[str, dict[str, list[Decision]]] = {}
            for key in ("moved", "held"):
                for decision in parts[key]:
                    projects.setdefault(decision.project_id, {"moved": [], "held": []})[key].append(
                        decision
                    )
            new_projects: list[str] = []
            for project_id, lists in sorted(projects.items()):
                message_id = await self._notify_project(batch, project_id, lists)
                if message_id:
                    sent.append(message_id)
                    new_projects.append(project_id)
            if parts["moved"] or new_projects:
                sample = (parts["moved"] or parts["held"])[0]
                held_kinds: dict[str, int] = {}
                for decision in parts["held"]:
                    held_kinds[decision.kind or "-"] = held_kinds.get(decision.kind or "-", 0) + 1
                targets: dict[str, int] = {}
                for decision in parts["moved"]:
                    targets[str(decision.to_profile_id)] = (
                        targets.get(str(decision.to_profile_id), 0) + 1
                    )
                await self._emit(
                    "provider.reroute_batch",
                    {
                        "batch_id": batch,
                        "provider": sample.from_provider,
                        "generation": sample.provider_generation,
                        "moved": len(parts["moved"]),
                        "held": held_kinds,
                        "targets": targets,
                        "projects": sorted(projects),
                    },
                )
        return sent

    async def _notify_project(
        self,
        batch: str,
        project_id: str,
        lists: Mapping[str, Sequence[Decision]],
    ) -> str | None:
        """The per-project supervisor notice for *batch*, once (D19)."""
        cfg = self.config
        if not getattr(cfg.notify, "supervisor", True):
            return None
        messages_cfg = getattr(self._config_getter(), "messages", None)
        if messages_cfg is not None and not getattr(messages_cfg, "enabled", True):
            return None
        thread_id = f"reroute:{batch}:{project_id}"
        to_id = f"supervisor-{project_id}"
        try:
            if await self.db.reroute_notice_sent(thread_id, "session", to_id):
                return None
        except Exception:
            logger.debug("provider reroute: notice check failed", exc_info=True)
            return None
        moved, held = list(lists.get("moved") or ()), list(lists.get("held") or ())
        sample = (moved or held)[0]
        lines = [
            (
                f"Provider {sample.from_provider} is {sample.provider_state}; "
                f"re-route batch `{batch}` in project {project_id}."
            ),
        ]
        if moved:
            lines.append(f"Moved ({len(moved)}):")
            lines += [
                f"- {d.task_id}: {d.from_profile_id} -> {d.to_profile_id}" for d in moved[:25]
            ]
            if len(moved) > 25:
                lines.append(f"- ... and {len(moved) - 25} more")
        if held:
            lines.append(f"Held ({len(held)}):")
            lines += [f"- {d.task_id}: {d.kind} ({d.from_profile_id})" for d in held[:25]]
            if len(held) > 25:
                lines.append(f"- ... and {len(held) - 25} more")
        lines.append(
            "Commands: `aq provider status`, `aq provider reroute --dry-run`, "
            f"`aq provider reroute-undo --batch-id {batch}`, `aq task explain <id>`."
        )
        from_kind, from_id = NOTICE_FROM
        try:
            message = await self.db.create_message(
                project_id=project_id,
                from_kind=from_kind,
                from_id=from_id,
                to_kind="session",
                to_id=to_id,
                subject=f"Provider failover: {len(moved)} moved, {len(held)} held",
                body="\n".join(lines),
                thread_id=thread_id,
            )
        except Exception:
            logger.warning("provider reroute: notice to %s failed", to_id, exc_info=True)
            return None
        return getattr(message, "id", None)

    # -- undo -------------------------------------------------------------------

    async def undo(
        self,
        *,
        batch_id: str | None = None,
        task_ids: Sequence[str] | None = None,
        force: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Return tasks to ``rerouted_from`` (D16): by batch or by task.

        Refused per task while the original provider is still unavailable,
        unless *force*; always refused for a running or claimed task.
        """
        db = self.db
        wanted: list[str] = list(task_ids or [])
        if batch_id:
            rows = await db.list_task_reroutes(batch_id=batch_id, limit=10_000)
            wanted += [row["task_id"] for row in rows if row.get("undone_at") is None]
        wanted = list(dict.fromkeys(wanted))
        if not wanted:
            return {
                "success": False,
                "error": "nothing to undo: name a batch with un-undone moves or tasks",
            }
        profiles = {p.id: p for p in await db.list_profiles()}
        latest = await db.latest_task_reroutes(wanted)
        undone: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        now = self.now()
        async with self._lock:
            for task_id in wanted:
                task = await db.get_task(task_id)
                if task is None:
                    refused.append({"task_id": task_id, "reason": "task not found"})
                    continue
                if not task.rerouted_from:
                    refused.append({"task_id": task_id, "reason": "not re-routed"})
                    continue
                home = profiles.get(task.rerouted_from)
                if home is None:
                    refused.append(
                        {"task_id": task_id, "reason": f"profile {task.rerouted_from} is gone"}
                    )
                    continue
                home_provider = self.availability.provider_for_profile(home)
                if self.availability.is_unavailable(home_provider, now) and not force:
                    refused.append(
                        {
                            "task_id": task_id,
                            "reason": (
                                f"provider {home_provider} is still "
                                f"{self.availability.effective_state(home_provider, now)}; "
                                "pass force to undo anyway"
                            ),
                        }
                    )
                    continue
                current_provider = self.availability.provider_for_profile(
                    profiles.get(task.profile_id)
                ) if task.profile_id in profiles else ""
                last = latest.get(task_id) or {}
                kwargs: dict[str, Any] = {}
                previous_class = last.get("intelligence_class")
                if previous_class and previous_class != task.intelligence_class:
                    kwargs["intelligence_class"] = previous_class
                result = await db.undo_task_reroute(
                    task_id,
                    record={
                        "from_provider": current_provider,
                        "to_provider": home_provider,
                        "intelligence_class": task.intelligence_class,
                        "provider_state": self.availability.effective_state(home_provider, now),
                        "batch_id": batch_id or last.get("batch_id"),
                        "actor": actor,
                    },
                    **kwargs,
                )
                if result is None:
                    refused.append(
                        {
                            "task_id": task_id,
                            "reason": "running or claimed; stop the task before changing its route",
                        }
                    )
                    continue
                undone.append({"task_id": task_id, **result})
                body = (
                    f"Re-route undone by {actor}: back from `{result['from_profile_id']}` "
                    f"to `{result['to_profile_id']}`."
                )
                try:
                    await db.add_task_comment(
                        task_id, body, author_kind="supervisor", author_id=AUTHOR_ID
                    )
                except Exception:
                    logger.debug("provider reroute: undo comment failed", exc_info=True)
                await self._emit(
                    "task.rerouted",
                    {
                        "task_id": task_id,
                        "project_id": task.project_id,
                        "title": task.title,
                        "from_profile_id": result["from_profile_id"],
                        "to_profile_id": result["to_profile_id"],
                        "from_provider": current_provider,
                        "to_provider": home_provider,
                        "reason_code": "operator_undo",
                        "batch_id": batch_id or last.get("batch_id"),
                        "actor": actor,
                    },
                )
        return {
            "success": bool(undone) or not refused,
            "outcome": "undone" if undone else "refused",
            "undone": undone,
            "refused": refused,
            **({"error": "; ".join(f"{r['task_id']}: {r['reason']}" for r in refused)}
               if refused and not undone else {}),
        }

    # -- derived answers ----------------------------------------------------------

    async def hold_kind(self, task: Any) -> dict[str, Any] | None:
        """``{"kind", "ahead", "detail", "to_profile_id"}`` for a held task (D18).

        Runs the planner over the task's provider queue so ``ahead`` and the
        trickle agree with what the next sweep would do.  ``None`` when the
        task would not be held (its provider is launchable).
        """
        if not getattr(task, "profile_id", None):
            return None
        ctx = await self.context()
        provider = ctx.profile_providers.get(task.profile_id, "")
        if not ctx.unavailable(provider):
            return None
        candidates = await self._candidates(ctx, providers=[provider], task_ids=None)
        if not any(c.task_id == task.id for c in candidates):
            candidates.append(
                Candidate(
                    task_id=task.id,
                    project_id=task.project_id,
                    profile_id=task.profile_id,
                    priority=int(getattr(task, "priority", 100) or 100),
                    created_at=float(getattr(task, "created_at", 0.0) or 0.0),
                    status=str(getattr(getattr(task, "status", None), "value", "") or ""),
                    title=str(getattr(task, "title", "") or ""),
                    intelligence_class=getattr(task, "intelligence_class", None),
                    intent=effective_intent(task),
                )
            )
        ctx.stats = await self.db.task_reroute_stats([c.task_id for c in candidates])
        decisions = plan_sweep(candidates, ctx, include_paused=True)
        mine = next((d for d in decisions if d.task_id == task.id), None)
        if mine is None or mine.action == "skip":
            return None
        inactive = await self.failover_inactive_reason()
        if mine.action == "move" or mine.kind == "awaiting_failover_capacity":
            if inactive is not None:
                return {"kind": "failover_inactive", "ahead": None, "detail": inactive,
                        "to_profile_id": mine.to_profile_id}
            if mine.action == "move":
                return {
                    "kind": "awaiting_failover_capacity",
                    "ahead": 0,
                    "detail": f"the next sweep moves it to {mine.to_profile_id}",
                    "to_profile_id": mine.to_profile_id,
                }
        return {
            "kind": mine.kind,
            "ahead": mine.ahead,
            "detail": mine.detail,
            "to_profile_id": mine.to_profile_id,
        }

    def resolve_default_profile_id(
        self,
        default_profile_id: str | None,
        profiles: Mapping[str, Any],
        *,
        project_id: str | None = None,
    ) -> str | None:
        """The project default while its provider is down: its equivalent rung (D13).

        Derived per call and never persisted, so recovery needs no undo.  A
        default whose provider is launchable, that is not a worker rung, or
        that has no equivalent on an ``available`` provider is returned as is
        (its tasks then hold).  Synchronous and I/O-free: *profiles* is a
        snapshot the caller already has.
        """
        from src.profiles.catalog import rung_profile_id, worker_route

        availability = self.availability
        if not default_profile_id or not availability.enforcing:
            return default_profile_id
        profile = profiles.get(default_profile_id)
        if profile is None:
            return default_profile_id
        provider = availability.provider_for_profile(profile)
        if not availability.is_unavailable(provider):
            return default_profile_id
        route = worker_route(
            profile.id,
            harness=getattr(profile, "harness", ""),
            default_class=getattr(profile, "default_class", ""),
            lifecycle=getattr(profile, "lifecycle", "task"),
            template=bool(getattr(profile, "template", False)),
            read_only=bool(getattr(profile, "read_only", False)),
        )
        if route is None:
            return default_profile_id
        _harness, class_id = route
        classes = self._classes()
        cls = classes.get(class_id)
        order = list(getattr(self.config, "order", None) or []) or list(_worker_provider_order())
        best: tuple | None = None
        for other in profiles.values():
            if getattr(other, "template", False) or not getattr(other, "enabled", True):
                continue
            other_route = worker_route(
                other.id,
                harness=getattr(other, "harness", ""),
                default_class=getattr(other, "default_class", ""),
                lifecycle=getattr(other, "lifecycle", "task"),
                template=bool(getattr(other, "template", False)),
                read_only=bool(getattr(other, "read_only", False)),
            )
            if other_route is None or other_route[1] != class_id:
                continue
            other_provider = availability.provider_for_profile(other)
            if other_provider == provider:
                continue
            if availability.effective_state(other_provider) != AVAILABLE:
                continue
            if cls is not None and not _class_has_slice(cls, other, self.harness_registry):
                continue
            rank = (
                order.index(other_provider) if other_provider in order else len(order),
                other.id != rung_profile_id(class_id, other_route[0]),
                str(getattr(other, "lifecycle", "task")) != "pool",
                other.id,
            )
            if best is None or rank < best[0]:
                best = (rank, other.id)
        return best[1] if best is not None else default_profile_id
