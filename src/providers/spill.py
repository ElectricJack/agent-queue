"""Capacity spill: move READY work off a pool that cannot serve it (provider-failover D24).

Provider failover (:mod:`src.providers.reroute`) moves queued work off a
provider that is *down*.  Nothing moved it off a pool that is merely *full*:
a pool session claims only tasks routed to its own profile, and pool demand
counts only those tasks, so a READY task on a saturated ``max_active: 1``
rung waited while a same-class rung on another provider sat idle.  Spill is
the second pass of the automatic ``provider_reroute`` sweep that closes that
gap (S1).  It is policy, not scheduling: it changes no bound, no admission
rule and no claim query, and every move is an ordinary, undoable re-route.

This module is the pure half, in the house style of :func:`plan_sweep`:

* :class:`CapacityView` is the pool arithmetic the planner reads, and
  :func:`capacity_view_from_measurement` folds one tick's
  ``PoolMeasurement`` into it with the same counting
  :func:`~src.scheduler.size_pools` and
  :func:`~src.scheduler.place_pool_actions` use, and
  :func:`with_incoming_moves` adds the failover pass's planned moves to it;
* :func:`plan_capacity_spill` turns candidates, a :class:`PlanContext` and a
  view into one :class:`~src.providers.reroute.Decision` per candidate under
  Decisions S3 to S6.

No I/O, no clock: *now* is an argument.  Whether spill runs at all
(``provider_failover.spill.enabled``, ``mode``, ``reroute.enabled``) is the
caller's decision; the planner only plans.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.providers.availability import AVAILABLE, UNAVAILABLE
from src.providers.intent import PINNED
from src.providers.reroute import (
    Candidate,
    Decision,
    PlanContext,
    Rung,
    _hold,
    _policy,
    _rung_rank,
    provider_order,
)

__all__ = [
    "SPILL_BATCH_PREFIX",
    "SPILL_HOLD_KINDS",
    "CapacityView",
    "PoolCapacity",
    "PoolProjectCapacity",
    "SpillCandidate",
    "capacity_view_from_measurement",
    "plan_capacity_spill",
    "spill_batch_id",
    "with_incoming_moves",
]

#: Why a READY task on a pool that cannot serve it is not spilling (S3-S6, S8).
#: ``class_policy_hold`` and ``reroute_limit_reached`` are failover's kinds,
#: reused because the same knobs cause them.
SPILL_HOLD_KINDS = (
    "spill_waiting",
    "spill_pinned",
    "class_policy_hold",
    "reroute_limit_reached",
    "spill_preferred_provider",
    "spill_no_target",
    "spill_sweep_limit",
)

#: Every spill move of one sweep shares ``spill-<UTC yyyymmddThhmm>`` (S7).
SPILL_BATCH_PREFIX = "spill"


def spill_batch_id(now: float) -> str:
    """The batch id of the sweep running at *now* (epoch seconds)."""
    return f"{SPILL_BATCH_PREFIX}-{time.strftime('%Y%m%dT%H%M', time.gmtime(now))}"


# -- the view the planner reads ---------------------------------------------------


@dataclass(frozen=True)
class PoolProjectCapacity:
    """One pool's sessions and READY work in one project, this tick."""

    idle: int = 0
    busy: int = 0
    starting: int = 0
    ready: int = 0

    @property
    def unserved(self) -> int:
        """READY work no idle or booting worker here will take (may be negative)."""
        return self.ready - self.idle - self.starting


@dataclass(frozen=True)
class PoolCapacity:
    """One ``lifecycle: pool`` profile, fleet-wide and per project.

    ``live`` is ``idle + busy + starting`` -- what :func:`size_pools` counts
    against ``max_active`` and the global cap; draining and unresponsive
    sessions are not supply there and are not here.  ``ready`` is the
    sizer's demand: claimable READY work routed to this profile (never
    unrouted work, mandatory routing §9.1).
    """

    profile_id: str
    class_id: str = ""
    provider: str = ""
    enabled: bool = True
    lifecycle: str = "pool"
    #: The sizer's upper bound; ``None`` is unbounded, a disabled or
    #: provider-suppressed pool reads ``0``.
    max_active: int | None = None
    live: int = 0
    idle: int = 0
    starting: int = 0
    ready: int = 0
    projects: Mapping[str, PoolProjectCapacity] = field(default_factory=dict)
    #: Projects whose ``(project, profile)`` key is inside its launch backoff.
    quarantined: frozenset[str] = frozenset()

    def project(self, project_id: str) -> PoolProjectCapacity:
        return self.projects.get(project_id) or PoolProjectCapacity()

    @property
    def unserved(self) -> int:
        """Fleet READY work this pool's idle and booting workers will not take."""
        return max(0, self.ready - self.idle - self.starting)


@dataclass(frozen=True)
class CapacityView:
    """Every pool's capacity plus the two limits pools share."""

    pools: Mapping[str, PoolCapacity]
    #: Project id -> how many more pool sessions it can host: free workspaces,
    #: bounded by ``max_concurrent_agents`` less its live pool sessions.  A
    #: project missing here (inactive, or no pool) has no room.
    project_room: Mapping[str, int] = field(default_factory=dict)
    #: Global pool cap less every live pool session; ``None`` is unbounded.
    global_headroom: int | None = None


def capacity_view_from_measurement(
    measurement: Any,
    global_cap: int | None,
    *,
    profile_providers: Mapping[str, str] | None = None,
) -> CapacityView:
    """Fold one ``PoolMeasurement`` (``Orchestrator._measure_pools``) into a view.

    *global_cap* is ``Orchestrator._pool_global_cap()``.  *profile_providers*
    maps profile id to its harness provider key (``PlanContext.
    profile_providers``); without it a pool's provider is its harness id,
    which is the key for every shipped harness.
    """
    from src.profiles.catalog import worker_route

    providers = profile_providers or {}
    supply = getattr(measurement, "supply", None) or {}
    bounds = getattr(measurement, "bounds", None) or {}
    demand = getattr(measurement, "demand", None) or {}
    placement = getattr(measurement, "candidates", None) or {}
    pools: dict[str, PoolCapacity] = {}
    room: dict[str, int] = {}
    used = sum(s.running_idle + s.running_busy + s.starting for s in supply.values())
    for key, profile in (getattr(measurement, "profiles", None) or {}).items():
        pid = key.profile_id
        sup = supply.get(key)
        live = idle = starting = 0
        if sup is not None:
            idle, starting = sup.running_idle, sup.starting
            live = sup.running_idle + sup.running_busy + sup.starting
        projects: dict[str, PoolProjectCapacity] = {}
        quarantined: set[str] = set()
        for cand in placement.get(key, []):
            local = sup.by_project.get(cand.project_id) if sup is not None else None
            p_idle = local.running_idle if local is not None else len(cand.idle_session_ids)
            p_busy = local.running_busy if local is not None else max(
                0, cand.live - p_idle - cand.starting
            )
            projects[cand.project_id] = PoolProjectCapacity(
                idle=p_idle, busy=p_busy, starting=cand.starting, ready=cand.ready
            )
            if cand.quarantined:
                quarantined.add(cand.project_id)
            if cand.project_id not in room:
                free = cand.workspace_capacity
                if cand.project_cap is not None:
                    free = min(free, cand.project_cap - cand.project_live_total)
                room[cand.project_id] = max(0, free)
        lifecycle = str(getattr(profile, "lifecycle", "pool") or "pool")
        route = worker_route(
            pid,
            harness=getattr(profile, "harness", ""),
            default_class=getattr(profile, "default_class", ""),
            lifecycle=lifecycle,
            template=bool(getattr(profile, "template", False)),
            read_only=bool(getattr(profile, "read_only", False)),
        )
        bound = bounds.get(key)
        pools[pid] = PoolCapacity(
            profile_id=pid,
            class_id=route[1] if route else str(getattr(profile, "default_class", "") or ""),
            provider=providers.get(pid) or str(getattr(profile, "harness", "") or ""),
            enabled=bool(getattr(profile, "enabled", True)),
            lifecycle=lifecycle,
            max_active=bound[1] if bound is not None else getattr(profile, "max_active", None),
            live=live,
            idle=idle,
            starting=starting,
            ready=int(demand.get(key, 0)),
            projects=projects,
            quarantined=frozenset(quarantined),
        )
    headroom = None if global_cap is None else max(0, int(global_cap) - used)
    return CapacityView(pools=pools, project_room=room, global_headroom=headroom)


def with_incoming_moves(view: CapacityView, decisions: Iterable[Decision]) -> CapacityView:
    """*view* with every planned move in *decisions* counted as READY on its target.

    The sweep plans its failover pass and its spill pass before it writes
    anything, so the one measurement it takes does not yet see the work
    failover is about to move.  Folding those moves in keeps a spill target
    from offering the same capacity twice (S5) and makes a ``dry_run`` plan
    the live one.  Returns *view* itself when nothing moves in.
    """
    incoming: dict[str, dict[str, int]] = {}
    for decision in decisions:
        target = decision.to_profile_id
        if decision.action != "move" or not target or target not in view.pools:
            continue
        per_project = incoming.setdefault(target, {})
        per_project[decision.project_id] = per_project.get(decision.project_id, 0) + 1
    if not incoming:
        return view
    pools = dict(view.pools)
    for profile_id, per_project in incoming.items():
        pool = pools[profile_id]
        projects = dict(pool.projects)
        for project_id, count in per_project.items():
            local = pool.project(project_id)
            projects[project_id] = replace(local, ready=local.ready + count)
        pools[profile_id] = replace(
            pool, ready=pool.ready + sum(per_project.values()), projects=projects
        )
    return replace(view, pools=pools)


# -- the planner --------------------------------------------------------------------


@dataclass(frozen=True)
class SpillCandidate(Candidate):
    """A READY task on a pool rung, with the age the threshold reads (S3)."""

    #: ``tasks.updated_at``: the task has waited since then.
    updated_at: float = 0.0


def _cannot_start(pool: PoolCapacity, project_id: str, view: CapacityView) -> str:
    """Why *pool* cannot start another worker for *project_id* (S4), or ``""``.

    Reads the view as measured: this sweep's own planned starts never make a
    source look blocked.
    """
    if pool.max_active is not None and pool.live >= pool.max_active:
        return f"{pool.live}/{pool.max_active} live, {pool.idle} idle"
    if view.global_headroom is not None and view.global_headroom <= 0:
        return "the fleet is at its global pool cap"
    if project_id in pool.quarantined:
        return f"quarantined in {project_id}"
    if view.project_room.get(project_id, 0) <= 0:
        return f"{project_id} has no room for another worker"
    return ""


class _Headroom:
    """S5's target headroom, consumed as the sweep plans moves."""

    def __init__(self, view: CapacityView) -> None:
        self.view = view
        self.global_left = view.global_headroom
        self.room = dict(view.project_room)
        self.starts: dict[str, int] = {}
        self.idle_taken: dict[tuple[str, str], int] = {}

    def free_now(self, pool: PoolCapacity, project_id: str) -> int:
        """Idle workers in the project with no READY work of their own."""
        proj = pool.project(project_id)
        taken = self.idle_taken.get((pool.profile_id, project_id), 0)
        return max(0, proj.idle - proj.ready) - taken

    def free_starts(self, pool: PoolCapacity, project_id: str) -> int:
        """Starts the pool can still make for the project, net of its own demand."""
        if self.room.get(project_id, 0) <= 0:
            return 0
        limits: list[int] = []
        if pool.max_active is not None:
            limits.append(pool.max_active - pool.live - self.starts.get(pool.profile_id, 0))
        if self.global_left is not None:
            limits.append(self.global_left)
        if not limits:
            return self.room[project_id]
        return max(0, min(limits) - pool.unserved)

    def available(self, pool: PoolCapacity, project_id: str) -> int:
        return self.free_now(pool, project_id) + self.free_starts(pool, project_id)

    def take(self, pool: PoolCapacity, project_id: str) -> None:
        if self.free_now(pool, project_id) > 0:
            key = (pool.profile_id, project_id)
            self.idle_taken[key] = self.idle_taken.get(key, 0) + 1
            return
        self.starts[pool.profile_id] = self.starts.get(pool.profile_id, 0) + 1
        if self.global_left is not None:
            self.global_left -= 1
        self.room[project_id] = self.room.get(project_id, 0) - 1


def _target_rungs(
    ctx: PlanContext,
    view: CapacityView,
    *,
    class_id: str,
    source_id: str,
    project_id: str,
    providers: Sequence[str],
    route_candidates: Sequence[tuple[str, str]] = (),
) -> list[Rung]:
    """S5's eligible targets in preference order, before headroom.

    A task the router routed spills only to its own candidates, in the
    router's order (mandatory routing §6.8), so a spill never breaks a lane:
    an integration repair never reaches OpenCode, art design never leaves
    Codex.  Each target carries the candidate's class.  A task without
    candidates (a legacy route) keeps the same-class search.
    """
    out: list[Rung] = []
    if route_candidates:
        allowed = set(providers)
        for profile_id, candidate_class in route_candidates:
            rung = ctx.rungs.get(profile_id)
            if (
                rung is None
                or profile_id == source_id
                or not rung.enabled
                or rung.lifecycle != "pool"
                or rung.provider not in allowed
                or ctx.state(rung.provider) != AVAILABLE
                or (candidate_class and rung.class_id != candidate_class)
            ):
                continue
            pool = view.pools.get(profile_id)
            if pool is None or not pool.enabled or pool.lifecycle != "pool":
                continue
            if project_id in pool.quarantined:
                continue
            out.append(rung)
        return out
    for provider in providers:
        if ctx.state(provider) != AVAILABLE:  # never degraded, whatever failover allows
            continue
        rungs = sorted(
            (
                rung
                for rung in ctx.rungs.values()
                if rung.provider == provider
                and rung.class_id == class_id
                and rung.enabled
                and rung.lifecycle == "pool"
                and rung.profile_id != source_id
            ),
            key=_rung_rank,
        )
        for rung in rungs:
            pool = view.pools.get(rung.profile_id)
            if pool is None or not pool.enabled or pool.lifecycle != "pool":
                continue
            if project_id in pool.quarantined:
                continue
            out.append(rung)
    return out


def plan_capacity_spill(
    candidates: Sequence[SpillCandidate],
    ctx: PlanContext,
    view: CapacityView,
    *,
    preferred_providers: Mapping[str, str] | None = None,
    now: float,
) -> list[Decision]:
    """One decision per candidate, in claim order (``priority, created_at, task_id``).

    ``skip`` means the source pool can serve the task (or it is not spill's
    to move); ``hold`` carries one of :data:`SPILL_HOLD_KINDS`; ``move``
    names a same-class pool with headroom in the task's project.  A move's
    ``detail`` says what saturated the source, for the task comment (S7).
    *preferred_providers* maps project id to ``projects.preferred_provider``
    (S6).  Reads ``ctx.config`` for ``spill``, ``reroute`` and ``classes``,
    and ``ctx.stats`` for the per-task limits.
    """
    cfg = ctx.config
    spill = getattr(cfg, "spill", None)
    reroute = getattr(cfg, "reroute", None)
    after = float(getattr(spill, "after_seconds", 300) or 0)
    max_moves = int(getattr(spill, "max_per_sweep", 5) or 5)
    cooldown = float(getattr(reroute, "task_cooldown_seconds", 1800) or 0)
    max_auto = int(getattr(reroute, "max_auto_per_task", 2) or 2)
    preferred_by_project = preferred_providers or {}
    headroom = _Headroom(view)
    planned: dict[tuple[str, str], int] = {}
    waiting: dict[str, int] = {}
    moves = 0
    decisions: list[Decision] = []
    for cand in sorted(candidates, key=lambda c: (c.priority, c.created_at, c.task_id)):
        source_id, project_id = cand.profile_id, cand.project_id
        rung = ctx.rungs.get(source_id)
        pool = view.pools.get(source_id)
        provider = (
            ctx.profile_providers.get(source_id)
            or (rung.provider if rung else "")
            or (pool.provider if pool else "")
        )
        state = ctx.state(provider)
        class_id = (cand.intelligence_class or "").strip() or (rung.class_id if rung else "")
        decision = Decision(
            task_id=cand.task_id,
            project_id=project_id,
            from_profile_id=source_id,
            from_provider=provider,
            provider_state=state,
            action="skip",
            intelligence_class=class_id or None,
            intent=cand.intent,
            priority=cand.priority,
            title=cand.title,
            status=cand.status,
            provider_generation=ctx.generations.get(provider),
        )
        decisions.append(decision)

        # -- S3/S4: is this spill's to move at all? ------------------------------
        if rung is None or pool is None or rung.lifecycle != "pool" or pool.lifecycle != "pool":
            decision.detail = f"{source_id} is not a worker pool rung"
            continue
        if cand.status != "READY":
            decision.detail = f"status is {cand.status}, not READY"
            continue
        if state in UNAVAILABLE:
            decision.detail = f"provider {provider} is {state}: failover moves it, not spill"
            continue
        if project_id not in pool.projects:
            decision.detail = f"{project_id} has no pool measurement (not an active project)"
            continue
        if not (rung.enabled and pool.enabled):
            why = "disabled"
        else:
            proj = pool.project(project_id)
            served = (
                f"{source_id} serves it in {project_id}: {proj.idle} idle and "
                f"{proj.starting} starting for {proj.ready} ready"
            )
            if proj.unserved <= 0:
                decision.detail = served
                continue
            why = _cannot_start(pool, project_id, view)
            if not why:
                decision.detail = f"{source_id} can start another worker in {project_id}"
                continue
            # The first ``unserved`` in claim order are planned; the rest are
            # what the idle and booting workers here will take.
            key = (source_id, project_id)
            if planned.get(key, 0) >= proj.unserved:
                decision.detail = served
                continue
            planned[key] = planned.get(key, 0) + 1

        # -- holds ---------------------------------------------------------------
        if cand.intent == PINNED:
            _hold(decision, "spill_pinned", "pinned to its provider by a human")
            continue
        if not class_id:
            _hold(decision, "spill_no_target", "the task has no intelligence class to match")
            continue
        if _policy(ctx, class_id) == "hold":
            _hold(decision, "class_policy_hold", f"provider_failover.classes holds {class_id}")
            continue
        stats = ctx.stats.get(cand.task_id) or {}
        if int(stats.get("auto_count") or 0) >= max_auto:
            _hold(
                decision,
                "reroute_limit_reached",
                f"moved automatically {stats.get('auto_count')} times; a human decides now",
            )
            continue
        last = stats.get("last_auto_at")
        if last is not None and cooldown and now - float(last) < cooldown:
            _hold(
                decision,
                "reroute_limit_reached",
                f"moved automatically {int(now - float(last))} s ago (cooldown {int(cooldown)} s)",
            )
            continue
        age = now - float(cand.updated_at)
        if age < after:
            _hold(
                decision,
                "spill_waiting",
                f"{source_id} cannot serve it ({why}); eligible in {math.ceil(after - age)} s",
            )
            continue

        # -- S5/S6: a same-class pool with headroom -----------------------------------
        preferred = str(preferred_by_project.get(project_id) or "")
        order = [preferred] if preferred else provider_order(ctx, project_id)
        target: Rung | None = None
        for option in _target_rungs(
            ctx, view, class_id=class_id, source_id=source_id,
            project_id=project_id, providers=order,
            route_candidates=cand.route_candidates,
        ):
            if headroom.available(view.pools[option.profile_id], project_id) > 0:
                target = option
                break
        if target is None:
            if preferred and provider == preferred:
                _hold(
                    decision,
                    "spill_preferred_provider",
                    f"{project_id} prefers {preferred}; no other {class_id} pool on it has "
                    "room, and spill never moves work off the preferred provider",
                )
            else:
                where = f"on {preferred} (the project's preferred provider)" if preferred else (
                    "on an available provider"
                )
                what = "route candidate pool" if cand.route_candidates else f"{class_id} pool"
                _hold(
                    decision,
                    "spill_no_target",
                    f"no other {what} {where} has room in {project_id}",
                )
            continue
        if moves >= max_moves:
            ahead = waiting.get(target.profile_id, 0)
            waiting[target.profile_id] = ahead + 1
            _hold(
                decision,
                "spill_sweep_limit",
                f"this sweep reached provider_failover.spill.max_per_sweep ({max_moves})",
                ahead,
            )
            decision.to_profile_id = target.profile_id
            decision.to_provider = target.provider
            continue
        headroom.take(view.pools[target.profile_id], project_id)
        moves += 1
        decision.action = "move"
        decision.kind = None
        decision.to_profile_id = target.profile_id
        decision.to_provider = target.provider
        if target.class_id and target.class_id != class_id:
            decision.to_class = target.class_id
        decision.detail = f"{source_id} had no free capacity for {int(age // 60)} min: {why}"
    return decisions
