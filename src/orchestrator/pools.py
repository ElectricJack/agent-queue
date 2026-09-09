"""Worker pools — sizing and convergence (swarm-work-model §11).

One cascade step per tick, right after ``_schedule``: measure supply and
demand per profile (aggregated over every active project), ask the pure
:func:`~src.scheduler.size_pools` how many workers that profile should have
fleet-wide, ask the equally pure :func:`~src.scheduler.place_pool_actions`
*which project* each authorised start or drain applies to, then start or
drain sessions to converge.  The step reads one ``count_ready_by_profile``,
one ``list_sessions`` and one ``count_available_workspaces`` per active
project with a pool profile — no per-task queries.

Sizing is global because the configuration always was: a profile is global,
so ``min_active``/``max_active`` are fleet-wide bounds rather than
per-project ones multiplied by the number of active projects.  Placement is
where projects come back in — a worker's workspace and token scope are
minted at launch and never move, so the project it lands in is decided once,
here.

A pool profile is any :class:`~src.models.AgentProfile` with
``lifecycle == "pool"``.  Its tasks are never assigned by the push scheduler
(``Orchestrator._schedule`` and ``_is_session_routed`` both exclude them,
and ``AgentReconciler`` never creates a push agent row for one) — instead a
pool of long-lived ``lifecycle: pool`` sessions claims work in a loop via
``aq task claim``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from dataclasses import dataclass, field

from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    ProjectStatus,
    SessionRecord,
    Task,
    TaskStatus,
)
from src.orchestrator.base_workspace import base_checkout_refusal
from src.scheduler import (
    PlacementCandidate,
    PoolKey,
    PoolProjectSupply,
    PoolSupply,
    place_pool_actions,
    rebalance_idle_pools,
    size_pools,
)
from src.sessions.spec import pool_session_name

logger = logging.getLogger(__name__)

#: Session states a pool row still counts as live supply under.  ``sleeping``
#: is a named-lifecycle state and never observed on a pool row; ``stopped``
#: and ``quarantined`` are terminal and excluded.
_LIVE_STATES = ("starting", "running", "draining")

#: How long a failed launch quarantines its ``(project_id, profile_id)`` key
#: (seconds).  A bad harness or a raised exception from acquisition/mint is
#: not going to self-heal between now and the next 5s tick, so retrying
#: immediately just creates and deletes an agent row every cycle.  A starved
#: workspace acquisition failure also backs off so other projects can launch.
LAUNCH_BACKOFF = 60.0


#: How much of a dead session's captured startup output to carry into the
#: quarantine reason.  Enough to show the actual error line, short enough to
#: sit in a log record and an ``aq pool status`` row.
_STDERR_EXCERPT_CHARS = 400


def read_stderr_excerpt(path: str | None) -> str:
    """Tail of the captured startup output at *path*, or ``""``.

    Read **once**, at the moment the launch failure quarantines the key, and
    carried in the quarantine reason from there on.  Re-reading it per tick
    (or logging it per tick) is what turned one dead harness into a wall of
    identical stack traces; the quarantine window is what makes once enough.
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    text = " ".join(text.split())
    if len(text) > _STDERR_EXCERPT_CHARS:
        text = "..." + text[-_STDERR_EXCERPT_CHARS:]
    return text


@dataclass
class PoolMeasurement:
    """One tick's observation of every pool, as both halves of the pipeline see it.

    ``supply``/``demand``/``bounds`` are the fleet-wide numbers
    :func:`~src.scheduler.size_pools` reads; ``candidates`` is the
    per-project view :func:`~src.scheduler.place_pool_actions` reads;
    ``profiles`` and ``projects`` are what the executor needs to actually
    launch a session for a decision the two of them made.

    A dataclass rather than the tuple this used to return: six positional
    values was already at the limit of what a caller can destructure without
    getting the order wrong, and placement adds a seventh.
    """

    supply: dict[PoolKey, PoolSupply] = field(default_factory=dict)
    demand: dict[PoolKey, int] = field(default_factory=dict)
    bounds: dict[PoolKey, tuple[int, int | None]] = field(default_factory=dict)
    profiles: dict[PoolKey, AgentProfile] = field(default_factory=dict)
    candidates: dict[PoolKey, list[PlacementCandidate]] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)


class PoolsMixin:
    """Worker-pool sizing and convergence, mixed into ``Orchestrator``."""

    def _quarantine_pool(self, project_id: str, profile_id: str, reason: str) -> float:
        """Stop starting into ``(project_id, profile_id)`` for :data:`LAUNCH_BACKOFF`.

        Records *reason* alongside the deadline so ``aq pool status`` can say
        **why** a pool is not growing — a bare timestamp left an operator
        looking at a stalled pool with nothing to act on — and logs it once,
        here, rather than from each tick that skips the key.
        """
        until = time.time() + LAUNCH_BACKOFF
        self._pool_quarantine[(project_id, profile_id)] = until
        reasons = getattr(self, "_pool_quarantine_reason", None)
        if reasons is None:
            reasons = self._pool_quarantine_reason = {}
        reasons[(project_id, profile_id)] = reason
        logger.warning(
            "pool %s/%s quarantined for %.0fs: %s", project_id, profile_id, LAUNCH_BACKOFF, reason
        )
        return until

    def _pool_quarantine_state(self, project_id: str, profile_id: str, now: float):
        """``(until, reason)`` for a key still inside its window, else ``(None, None)``.

        Quarantine stays keyed ``(project_id, profile_id)`` even though the
        pool itself no longer is: every reason a launch quarantines a key —
        an unknown harness for that project, a startup death in that
        checkout, a base-checkout refusal against that repo — is specific to
        one project, so collapsing it to the profile would let one broken
        project disable a healthy fleet.
        """
        key = (project_id, profile_id)
        until = self._pool_quarantine.get(key)
        if not until or until <= now:
            return None, None
        return until, (getattr(self, "_pool_quarantine_reason", None) or {}).get(key)

    async def _pool_profiles(
        self, project_id: str, *, system_profiles: list[AgentProfile] | None = None
    ) -> dict[str, AgentProfile]:
        """Pool profiles available to *project_id*, keyed by agent-type id.

        Profiles are global — a durable worker is shared between projects — so
        every ``lifecycle: pool`` profile is a pool every active project can
        receive a worker for.  Sizing is global too — one ``PoolKey`` per
        profile — and a project's ``max_concurrent_agents`` bounds only how
        much of that one fleet it may hold, as a placement input.

        *system_profiles* lets a caller iterating many projects in one tick
        (``_measure_pools``, ``Orchestrator._schedule``) pass a single
        pre-fetched ``list_profiles()`` instead of paying that query again
        per project.
        """
        all_profiles = (
            system_profiles if system_profiles is not None else await self.db.list_profiles()
        )
        return {
            p.id: p
            for p in all_profiles
            if ":" not in p.id and getattr(p, "lifecycle", "task") == "pool"
        }

    async def _pool_profile_ids(
        self, project_id: str, *, system_profiles: list[AgentProfile] | None = None
    ) -> set[str]:
        return set(await self._pool_profiles(project_id, system_profiles=system_profiles))

    async def _measure_pools(self, project_ids: set[str] | None = None) -> PoolMeasurement:
        """One :class:`PoolMeasurement` for every pool profile, this tick.

        The loop is still per project — one ``count_ready_by_profile``, one
        ``list_sessions`` and one ``count_available_workspaces`` per active
        project with a pool profile is the cheapest shape the observation
        has — but each project now folds into the *aggregate* key rather
        than minting one of its own, and also records its own breakdown
        (``PoolSupply.by_project``) and its standing as a placement
        candidate.

        The ``count_available_workspaces`` call is the one addition, and it
        is not new work: ``_launch_pool_session`` already pays it as its
        first act.  Asking a tick earlier is what lets a project with no
        free workspace be skipped *before* it consumes start budget instead
        of after it has wasted a launch.

        One ``list_profiles()`` serves the whole tick, shared across every
        project's ``_pool_profiles`` lookup.
        """
        measurement = PoolMeasurement()
        #: ``(started_at, session_id)`` per key, so the aggregate idle list
        #: can be ordered oldest-first across projects rather than by the
        #: order the project loop happened to visit them in.
        idle_ages: dict[PoolKey, list[tuple[float, str]]] = {}
        now = time.time()
        worktrees_enabled = self._worktrees_enabled()

        system_profiles = await self.db.list_profiles()

        for project in await self.db.list_projects():
            if project.status != ProjectStatus.ACTIVE:
                continue
            if project_ids is not None and project.id not in project_ids:
                continue
            pool_profiles = await self._pool_profiles(project.id, system_profiles=system_profiles)
            if not pool_profiles:
                continue

            measurement.projects[project.id] = project
            ready_by_profile = await self.db.count_ready_by_profile(project.id)
            unrouted_ready = ready_by_profile.get(None, 0)
            default_profile_id = await self._effective_default_profile_id(project)
            workspace_capacity = await self.db.count_available_workspaces(
                project.id,
                worktree_slot_cap=(
                    self._project_slot_cap(project) if worktrees_enabled else None
                ),
            )
            sessions = await self.db.list_sessions(lifecycle="pool", project_id=project.id)
            sessions_by_profile: dict[str, list] = {}
            for s in sessions:
                sessions_by_profile.setdefault(s.profile_id, []).append(s)

            # Every live pool session in this project, whatever profile it
            # belongs to: the project cap bounds the project, not one pool.
            # Draining rows are excluded, matching what counts as supply.
            project_live_total = sum(
                1
                for s in sessions
                if s.state in ("starting", "running") and s.desired_state != "stopped"
            )

            for profile_id, profile in pool_profiles.items():
                key = PoolKey(profile_id)
                measurement.profiles[key] = profile
                ready = ready_by_profile.get(profile_id, 0)
                if default_profile_id == profile_id:
                    ready += unrouted_ready
                measurement.demand[key] = measurement.demand.get(key, 0) + ready

                local = PoolProjectSupply()
                rows = sorted(
                    sessions_by_profile.get(profile_id, []),
                    key=lambda s: s.started_at or 0.0,
                )
                for s in rows:
                    if s.state not in _LIVE_STATES:
                        continue
                    if s.state == "starting":
                        local.starting += 1
                    elif s.state == "draining" or s.desired_state == "stopped":
                        local.draining += 1
                    elif s.task_id or s.claim_phase:
                        local.running_busy += 1
                    else:
                        local.running_idle += 1
                        local.idle_session_ids.append(s.id)
                        idle_ages.setdefault(key, []).append((s.started_at or 0.0, s.id))

                sup = measurement.supply.setdefault(key, PoolSupply())
                sup.running_idle += local.running_idle
                sup.running_busy += local.running_busy
                sup.starting += local.starting
                sup.draining += local.draining
                sup.idle_session_ids.extend(local.idle_session_ids)
                sup.by_project[project.id] = local

                until, _reason = self._pool_quarantine_state(project.id, profile_id, now)
                measurement.candidates.setdefault(key, []).append(
                    PlacementCandidate(
                        project_id=project.id,
                        ready=ready,
                        live=local.running_idle + local.running_busy + local.starting,
                        project_live_total=project_live_total,
                        project_cap=project.max_concurrent_agents,
                        workspace_capacity=workspace_capacity,
                        quarantined=until is not None,
                        # Read defensively: ``min_per_project`` is a Phase 2
                        # profile field and defaults to "no reservation".
                        warm_floor=getattr(profile, "min_per_project", 0) or 0,
                        idle_session_ids=tuple(local.idle_session_ids),
                        starting=local.starting,
                    )
                )

        for key, sup in measurement.supply.items():
            # ``size_pools`` reports the fleet's oldest idle sessions on a
            # drain action, so the aggregate list is ordered across projects
            # by age.  Placement re-selects from the same pool of sessions;
            # this only keeps the sizer's own output reproducible.
            sup.idle_session_ids = [sid for _age, sid in sorted(idle_ages.get(key, []))]
            profile = measurement.profiles[key]
            if not getattr(profile, "enabled", True):
                # Disabled by an operator: size the pool to zero.  The sizer
                # floors ``desired`` at ``busy + starting``, so a worker
                # mid-task keeps its session and only idle workers drain —
                # the same semantics as scaling max down to 0.
                measurement.bounds[key] = (0, 0)
                continue
            # A ``min_per_project`` the global ``min_active`` cannot fund
            # raises the effective floor rather than being silently ignored
            # — but only for projects that could actually host the worker,
            # so a quarantined or workspace-starved project does not hold a
            # reservation open against the rest of the fleet.
            reserved = sum(
                cand.warm_floor
                for cand in measurement.candidates.get(key, [])
                if not cand.quarantined and cand.workspace_capacity > 0
            )
            measurement.bounds[key] = (max(profile.min_active or 0, reserved), profile.max_active)

        return measurement

    def _pool_global_cap(self) -> int | None:
        """The box-wide ceiling on live pool sessions, across every pool.

        ``swarm.global_max_active`` when an operator has set one, otherwise
        ``resources.max_concurrent_agents`` — the same number that bounds
        how many agents the box runs at once, which is exactly the quantity
        an unbounded fleet would otherwise blow past.  Read defensively
        because ``global_max_active`` is a Phase 2 config field.
        """
        explicit = getattr(self.config.swarm, "global_max_active", None)
        # ``is None``, never a falsy test: ``or`` on an int would silently
        # promote a configured ``0`` to the resource cap -- the opposite of
        # what it asks for.  ``SwarmConfig.validate`` rejects 0 today, so this
        # is defence against that validation being relaxed, not a live bug.
        if explicit is None:
            return self.config.resources.max_concurrent_agents
        return explicit

    async def _reconcile_pools(self) -> None:
        """The pool cascade step: measure, size, place, converge.  No-op unless enabled."""
        if not (self.config.swarm.enabled and self.config.sessions.enabled):
            return

        measurement = await self._measure_pools()
        now = time.time()
        await self._announce_bounds_rescoped(measurement)
        actions, self._pool_surplus_since = size_pools(
            supply=measurement.supply,
            demand=measurement.demand,
            bounds=measurement.bounds,
            global_cap=self._pool_global_cap(),
            surplus_since=self._pool_surplus_since,
            now=now,
            scale_down_grace=self.config.swarm.scale_down_grace,
            max_starts_per_tick=self.config.swarm.max_starts_per_tick,
            max_drains_per_tick=self.config.swarm.max_drains_per_tick,
        )
        starts, drains, starvations = place_pool_actions(
            actions=actions, candidates=measurement.candidates
        )
        relocations, self._pool_rebalance_since = rebalance_idle_pools(
            candidates=measurement.candidates,
            starts=starts,
            drains=drains,
            surplus_since=getattr(self, "_pool_rebalance_since", {}),
            now=now,
            grace=self.config.swarm.scale_down_grace,
            max_drains=self.config.swarm.max_drains_per_tick,
        )
        drains.extend(relocations)

        await self._report_pool_starvation(starvations)

        for start in starts:
            executed = 0
            for _ in range(start.count):
                sid = await self._launch_pool_session(
                    measurement.projects[start.project_id],
                    measurement.profiles[start.key],
                )
                if sid is None:
                    break
                executed += 1
            await self._emit_pool_scaled(
                start.key, start.project_id, "start", executed, reason=start.reason
            )

        for drain in drains:
            executed = 0
            for sid in drain.session_ids:
                await self.db.update_session(sid, desired_state="stopped")
                executed += 1
            await self._emit_pool_scaled(drain.key, drain.project_id, "drain", executed)

    async def _report_pool_starvation(self, starvations: list) -> None:
        """Warn about starts no project could take — once per condition, not per tick.

        A pool whose every project is quarantined or out of workspaces stays
        that way for as long as the backoff window (or the operator) takes,
        and this step runs every 5 seconds.  Logging unconditionally would
        rebuild exactly the wall of identical warnings the quarantine window
        exists to prevent, so a starvation is announced when it appears or
        when its blocking reasons change, and is silent while it merely
        persists.  The condition clearing is worth a line of its own: it is
        the answer to "when did the fleet start growing again?".

        A starvation whose every reason is a live quarantine is logged at
        debug, not warning: ``_quarantine_pool`` has already said the same
        thing about each project, with the captured startup output attached,
        and one wall of warnings per broken harness is enough.  Everything
        else — no workspace, at cap, no candidate project — has no other
        voice and is genuinely invisible without this line.

        The same gate rate-limits the ``pool.placement_starved`` event
        (spec §6.3): announced when the condition appears or its blocking
        reasons change, never once per 5s tick.  A per-tick event would be
        worse than a per-tick log line — it is persisted, so an overnight
        starvation would write ~17k audit rows saying the same thing.

        Also the only observer of *when* a starvation began.  Doctor is a
        point-in-time read and cannot know how long a condition has held, so
        ``pools.placement_starved`` (spec §5) reads the first-seen timestamps
        recorded here.  ``_pool_starvation_state`` is in-memory, exactly like
        ``_pool_quarantine``: a starvation is a live scheduling condition, not
        a fact about the world, and a daemon restart has genuinely stopped
        observing it.  ``_pool_starvation_observing_since`` records when this
        daemon started watching, so the check can say so rather than implying
        a five-minute-old daemon has seen five minutes of history.
        """
        seen = getattr(self, "_pool_starvation_signature", None)
        if seen is None:
            seen = self._pool_starvation_signature = {}
        state = getattr(self, "_pool_starvation_state", None)
        if state is None:
            state = self._pool_starvation_state = {}
        now = time.time()
        if getattr(self, "_pool_starvation_observing_since", None) is None:
            self._pool_starvation_observing_since = now
        current: dict[PoolKey, str] = {}
        for starvation in starvations:
            profile_id = starvation.key.profile_id
            reason_map = {pid: why for pid, why in starvation.reasons.items() if pid}
            detail = ", ".join(f"{pid}: {why}" for pid, why in sorted(reason_map.items()))
            current[starvation.key] = detail
            entry = state.get(profile_id)
            # ``since`` survives a change of blocking reason: a pool that goes
            # from "quarantined" to "at cap" without ever placing a start has
            # been starved throughout, and resetting the clock on each change
            # would hide exactly the long starvations the check exists for.
            since = entry["since"] if entry else now
            state[profile_id] = {
                "since": since,
                "wanted": starvation.wanted,
                "reasons": reason_map,
            }
            if seen.get(starvation.key) == detail:
                continue
            reasons = set(starvation.reasons.values())
            log = logger.debug if reasons == {"quarantined"} else logger.warning
            log(
                "pool %s: %d start(s) authorised, no eligible project (%s)",
                profile_id,
                starvation.wanted,
                detail or "no active project runs this profile",
            )
            await self._emit_placement_starved(profile_id, starvation.wanted, reason_map)
        for key in seen.keys() - current.keys():
            logger.info("pool %s: placement recovered, starts are landing again", key.profile_id)
            state.pop(key.profile_id, None)
        self._pool_starvation_signature = current

    async def _emit_placement_starved(
        self, profile_id: str, wanted: int, reasons: dict[str, str]
    ) -> None:
        """Persist *and* emit ``pool.placement_starved`` for one profile.

        Persist as well as emit for the same reason ``pool.scaled`` does: the
        bus is in-process and its WebSocket forward is live-only, so a client
        that was not connected at 03:14 has no way to learn that the fleet
        wanted to grow and had nowhere to put a worker.  ``aq system
        get-recent-events --event-type pool.placement_starved`` is that
        answer.  The payload is JSON because the blocking reasons are a
        per-project mapping and the audit column is a single Text field.
        """
        payload = {
            "profile_id": profile_id,
            "wanted": wanted,
            "reasons": dict(sorted(reasons.items())),
        }
        await self.db.log_event(
            "pool.placement_starved",
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        await self.bus.emit("pool.placement_starved", payload)

    async def _announce_bounds_rescoped(self, measurement: PoolMeasurement) -> None:
        """One ``pool.bounds_rescoped`` per pool profile, once per daemon lifetime.

        ``min_active``/``max_active`` used to be copied into one pool key per
        active project, so a profile with ``max_active: 4`` and five active
        projects really could run twenty workers.  They are fleet-wide now,
        and on a multi-project install that is a real, intended, and large
        reduction: unannounced it reads as a throughput regression at
        whatever hour the daemon happened to restart.  So the fleet says out
        loud what its ceiling used to be and what it is now — the same
        reasoning that put ``pool.scaled`` on the bus (spec §4).

        Once per profile per daemon lifetime, not once per tick: the numbers
        only change when the profile or the project roster does, and a
        five-second heartbeat of "your bounds are still global" is noise.
        The guard is in-memory, so a restart re-announces — which is correct,
        since the announcement is addressed to whoever is reading the log now
        and a restart is exactly when an operator asks the question.

        ``pools.global_bounds_migration`` (spec §5) reads the *first* such
        event per profile out of the audit log and treats the ``max_active``
        recorded in it as the pre-decision ceiling, so this payload is also
        the durable half of that check's self-resolution.
        """
        announced = getattr(self, "_pool_bounds_rescoped_announced", None)
        if announced is None:
            announced = self._pool_bounds_rescoped_announced = set()
        for key, profile in sorted(measurement.profiles.items(), key=lambda kv: kv[0].profile_id):
            if key.profile_id in announced:
                continue
            announced.add(key.profile_id)
            eligible = len(measurement.candidates.get(key, ()))
            max_active = profile.max_active
            min_active = profile.min_active or 0
            payload = {
                "profile_id": key.profile_id,
                "eligible_projects": eligible,
                # Post-rescoping: the profile's own bounds, fleet-wide.
                "effective_max_active": max_active,
                "effective_min_active": min_active,
                # Pre-rescoping: the same bounds once per eligible project.
                "previous_effective_max_active": (
                    None if max_active is None else max_active * eligible
                ),
                "previous_effective_min_active": min_active * eligible,
            }
            await self.db.log_event(
                "pool.bounds_rescoped",
                payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )
            await self.bus.emit("pool.bounds_rescoped", payload)

    async def _emit_pool_scaled(
        self, key: PoolKey, project_id: str, kind: str, executed: int, *, reason: str | None = None
    ) -> None:
        """Record and announce that *executed* workers started or drained.

        Persist *and* emit.  The bus is in-process and its WebSocket forward
        is live-only -- a client not connected at the moment of the scale
        never sees it -- so without the audit row a scaling decision left no
        trace any operator surface could read after the fact: ``aq pool
        status`` shows the current shape, never the fact that it changed or
        when.  ``aq system get-recent-events --event-type pool.scaled`` is
        the answer to "why did a worker appear at 03:14?", and
        ``placement_reason`` is the answer to "why *there*?".
        """
        if not executed:
            return
        await self.db.log_event(
            "pool.scaled",
            project_id=project_id,
            payload=f"{kind} {executed} {key.profile_id}",
        )
        payload = {
            "project_id": project_id,
            "profile_id": key.profile_id,
            "kind": kind,
            "count": executed,
        }
        if reason is not None:
            payload["placement_reason"] = reason
        await self.bus.emit("pool.scaled", payload)

    async def _launch_pool_session(self, project, profile) -> str | None:
        """Start one pool worker session for *profile* in *project*.

        Mirrors ``ExecutionMixin._launch_session_for_task`` step for step —
        same harness/provider/token/error handling — but there is no task:
        the agent row is created first, a ``project-repo`` workspace is
        acquired and locked to the *agent* (not a task), and the session
        bootstraps into a claim loop instead of one task's prompt.

        Everything from the moment the agent row exists onward runs inside
        one ``try``/``except``: any failure — acquisition, spec build,
        launch, or the session-row write — rolls all the way back (release
        the workspace, delete the agent, revoke the token if one was
        minted).  Returns the new session id, or ``None`` on any failure.

        A failed workspace acquisition backs off the project/profile key.
        Advertised lazy capacity can fail to materialize; retrying the same
        project every tick would starve other projects of the start budget.
        Every other failure
        (bad harness, launch crash, a raised exception from acquisition or
        the token mint) quarantines ``(project_id, profile_id)`` for
        :data:`LAUNCH_BACKOFF` seconds so a persistently broken pool does
        not create and immediately delete an agent row every tick.
        """
        from src.sessions.provider import SessionDiedDuringStartup, SessionHandle

        from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
        from src.agents.routing import resolve_agent_profile, task_agent_mismatch

        # Don't manufacture a durable definition when no execution workspace
        # could be acquired. Worktree slots still count as lazy capacity.
        available = await self.db.count_available_workspaces(
            project.id,
            worktree_slot_cap=(self._project_slot_cap(project) if self._worktrees_enabled() else None),
        )
        if not available:
            return None
        provider_name = self.config.sessions.provider
        try:
            provider = self.session_providers.create(provider_name, self.config)
        except ValueError as exc:
            self._quarantine_pool(project.id, profile.id, f"session provider unavailable: {exc}")
            return None

        # Reserve the identity before any await that starts a process. A live
        # session owns its worker even while it has no currently claimed task.
        profiles = {item.id: item for item in await self.db.list_profiles()}
        requirement = Task(
            id="", project_id=project.id, title="", description="", profile_id=profile.id,
            intelligence_class=profile.default_class,
        )
        classes = self.session_spec_builder._intelligence_classes
        candidates = await self.db.list_agents(state=AgentState.IDLE)
        candidates.sort(key=lambda candidate: (candidate.profile_id != profile.id, candidate.created_at))
        agent = None
        worker_profile = None
        for candidate in candidates:
            if not candidate.enabled or candidate.role != "worker":
                continue
            own_profile = resolve_agent_profile(candidate, profiles)
            if task_agent_mismatch(
                requirement, candidate, task_profile=profile, agent_profile=own_profile,
                harness_registry=self.harness_registry, intelligence_classes=classes,
            ):
                continue
            if await self.db.reserve_idle_agent(candidate.id):
                agent = candidate
                worker_profile = own_profile
                break
        if agent is None:
            agent = Agent(id=f"agent-{uuid.uuid4().hex[:12]}",
                          name=f"{profile.id}-{uuid.uuid4().hex[:4]}", profile_id=profile.id)
            worker_profile = resolve_agent_profile(agent, profiles) or profile
            mismatch = task_agent_mismatch(
                requirement, agent, task_profile=profile, agent_profile=worker_profile,
                harness_registry=self.harness_registry, intelligence_classes=classes,
            )
            if mismatch:
                logger.info("pool %s/%s cannot start: %s", project.id, profile.id, mismatch)
                return None
            # Only the fallback grows the roster; compatible definitions were
            # tried above. Deleted identities do not constrain pool capacity.
            if not await self.db.create_automatic_agent(agent):
                return None
            if not await self.db.reserve_idle_agent(agent.id):
                return None
        profile = apply_agent_overrides(profile, agent, agent_profile=worker_profile)
        harness_name = getattr(profile, "harness", "") or ""
        harness = self.harness_registry.get(harness_name, project.id)
        if harness is None:
            await self.db.update_agent(agent.id, state=AgentState.IDLE, current_task_id=None)
            self._quarantine_pool(project.id, profile.id, f"unknown harness {harness_name!r}")
            return None

        token_store = getattr(self, "token_store", None)
        # Claude accepts only canonical UUIDs for ``--session-id``. Keep the
        # durable/session-token identity separate from the readable provider
        # name used to address this pool worker.
        session_id = str(uuid.uuid4())
        session_name = pool_session_name(profile.id, project.id, uuid.uuid4().hex[:8])
        minted_token = False

        async def _rollback(reason: str, *, quarantine: bool) -> None:
            if not quarantine:
                # A starved pool is expected; ``_quarantine_pool`` does the
                # logging for the failures that are not.
                logger.warning("pool %s/%s: %s", project.id, profile.id, reason)
            await self.db.release_workspaces_for_agent(agent.id)
            await self.db.update_agent(agent.id, state=AgentState.IDLE, current_task_id=None)
            if minted_token and token_store is not None:
                try:
                    await token_store.revoke_session(session_id)
                except Exception:
                    logger.debug("pool %s/%s: token revoke failed", project.id, profile.id)
            if quarantine:
                self._quarantine_pool(project.id, profile.id, reason)

        try:
            kind = await self.db.resolve_workspace_kind(project.id, "project-repo")
            if kind is None:
                await _rollback("starved: no project-repo workspace kind", quarantine=True)
                return None

            worktrees_enabled = self._worktrees_enabled()
            fresh_slot: str | None = None
            if worktrees_enabled and kind.is_git_repo:
                # Prefer the slot this launch just paid for, so a concurrent
                # dispatch cannot take it out from under us (the pool launch
                # has the same growth-then-lose race task dispatch had).
                growth = await self._ensure_worktree_slots(project, kind.id)
                fresh_slot = growth.created.get(kind.id)

            workspace = await self.db.acquire_one_unlocked(
                project_id=project.id,
                kind_id=kind.id,
                mode=kind.default_lock_mode,
                locked_by_task_id=None,
                locked_by_agent_id=agent.id,
                prefer_workspace_id=fresh_slot,
                kind_mode=(kind.mode if worktrees_enabled and kind.is_git_repo else None),
                worktree_slot_cap=(self._project_slot_cap(project) if worktrees_enabled else None),
            )
            if workspace is None:
                await _rollback("starved: no free workspace", quarantine=True)
                return None

            work_dir = workspace.workspace_path

            # An exclusive-clone pool bypasses slot setup, but may later write
            # `.aq/claim.json`; install the same managed excludes before its
            # session receives the checkout.
            if kind.is_git_repo:
                await self._ensure_control_files_excluded(work_dir)

            # Same guard as the task-launch path: a pool session may not run
            # in the base checkout (see :mod:`src.orchestrator.base_workspace`).
            refusal = await base_checkout_refusal(
                self.db, work_dir, profile, project_id=project.id
            )
            if refusal:
                # Quarantine rather than starve: unlike "no free workspace"
                # this repeats identically every cycle until an operator
                # fixes the kind's slots or the profile's opt-in.
                await _rollback(refusal, quarantine=True)
                return None

            instance_token = uuid.uuid4().hex

            if token_store is not None:
                api_token = await token_store.mint(
                    session_id=session_id,
                    session_instance_token=instance_token,
                    task_id=None,
                    project_id=project.id,
                )
                minted_token = True
            else:
                api_token = uuid.uuid4().hex

            spec = self.session_spec_builder.build_pool_spec(
                profile=profile,
                project=project,
                agent_id=agent.id,
                harness=harness,
                work_dir=work_dir,
                session_id=session_id,
                session_name=session_name,
                instance_token=instance_token,
                epoch=self.daemon_epoch,
                api_token=api_token,
                workspace_source_type=workspace.source_type,
            )

            launched_at = time.time()
            try:
                await provider.start(spec)
            except SessionDiedDuringStartup as exc:
                excerpt = read_stderr_excerpt(exc.start_stderr_path)
                await _rollback(
                    f"session died during startup: {exc}"
                    + (f" | startup output: {excerpt}" if excerpt else ""),
                    quarantine=True,
                )
                return None

            now = time.time()
            try:
                await self.db.create_session(
                    SessionRecord(
                        id=session_id,
                        project_id=project.id,
                        profile_id=profile.id,
                        harness=harness.id,
                        provider=provider.name,
                        name=spec.session_name,
                        lifecycle="pool",
                        work_dir=work_dir,
                        epoch=self.daemon_epoch,
                        instance_token=instance_token,
                        started_at=launched_at,
                        session_key=session_id if harness.session_id_flag else None,
                        task_id=None,
                        state="running",
                        agent_id=agent.id,
                        **resolve_launch_settings(profile, harness, self.session_spec_builder),
                        last_activity=now,
                        hooks_provisioned=spec.hooks_provisioned,
                    ),
                    release_agent_reservation=True,
                )
            except Exception as exc:
                logger.error(
                    "pool %s/%s: session row insert failed",
                    project.id,
                    profile.id,
                    exc_info=True,
                )
                try:
                    await provider.stop(
                        SessionHandle(
                            name=spec.session_name,
                            provider=provider.name,
                            instance_token=instance_token,
                        ),
                        grace=2.0,
                    )
                except Exception:
                    logger.error(
                        "pool %s/%s: could not stop the orphan session %s",
                        project.id,
                        profile.id,
                        spec.session_name,
                        exc_info=True,
                    )
                    await self.db.update_agent(agent.id, state=AgentState.ERROR)
                    return None
                await _rollback(
                    f"session started but its row could not be written: {exc}", quarantine=True
                )
                return None
        except Exception as exc:
            await _rollback(f"launch failed: {exc}", quarantine=True)
            return None

        logger.info(
            "pool %s/%s: session %s started (%s/%s) in %s",
            project.id,
            profile.id,
            spec.session_name,
            provider.name,
            harness.id,
            work_dir,
        )
        await self.bus.emit(
            "pool.session_started",
            {
                "project_id": project.id,
                "profile_id": profile.id,
                "session_id": session_id,
                "name": spec.session_name,
                "state": "running",
            },
        )
        return session_id

    def _pool_teardown_lock(self, session_id):
        locks = getattr(self, "_pool_teardown_locks", None)
        if locks is None:
            locks = self._pool_teardown_locks = {}
        return locks.setdefault(session_id, asyncio.Lock())

    async def _terminate_pool_session(
        self, session, *, reason: str, task_status=TaskStatus.READY
    ) -> None:
        """Serialize teardown so late callers cannot clear a reused worker."""
        async with self._pool_teardown_lock(session.id):
            await self._terminate_pool_session_locked(
                session, reason=reason, task_status=task_status
            )

    async def _terminate_pool_session_locked(
        self, session, *, reason: str, task_status=TaskStatus.READY
    ) -> None:
        """Stop the process before making its durable worker or workspace reusable.

        The agent row is marked ``RETIRED`` **first**, up front, and only
        cleared back to ``IDLE`` at the very bottom once ``provider.stop``
        has actually confirmed the process is gone.  The two writes look
        contradictory read in isolation; they are the safe ordering.  Between
        them sits the early ``return`` on an unconfirmed stop, and that is
        the whole point: a worker whose process may still be alive stays
        ``RETIRED`` and is never handed to a second session, while a
        confirmed-stopped one goes back to the pool ``_launch_pool_session``
        draws its candidates from (``list_agents(state=IDLE)``).

        That reuse is what bounds the roster.  Retiring unconditionally would
        add one ``agents`` row per pool session — one per *task* under
        ``fresh_context_per_task`` — with no sweep able to reclaim them:
        ``soft_delete_agent`` cannot, because ``create_automatic_agent``
        refuses to grow the roster while any worker tombstone exists, and a
        hard delete drops history the task ledger still points at.  See
        swarm-work-model §11.2.1 and ``src/doctor/pool_checks``, which
        polices the rows that fall outside this loop.
        """
        # Callers may hold an old in-memory row after its worker has already
        # been reserved for a new launch. Completed teardown is idempotent.
        current = await self.db.get_session(session.id)
        if current is None or current.state == "stopped":
            return
        session = current
        await self.db.update_session(session.id, desired_state="stopped")
        other_live = [row for row in await self.db.list_sessions(agent_id=session.agent_id, live_only=True)
                      if row.id != session.id] if session.agent_id else []
        if session.agent_id and not other_live:
            await self.db.update_agent(session.agent_id, state=AgentState.RETIRED)
        token_store = getattr(self, "token_store", None)
        if token_store is not None:
            try:
                await token_store.revoke_session(session.id)
            except Exception:
                logger.warning("pool session %s: token revoke failed", session.id)
        if session.state != "stopped":
            from src.sessions.provider import SessionHandle
            try:
                provider = self.session_providers.create(session.provider, self.config)
                await provider.stop(SessionHandle(name=session.name, provider=session.provider,
                                                  instance_token=session.instance_token), grace=2.0)
            except Exception:
                logger.warning("pool session %s: provider stop unconfirmed; retaining resources", session.id, exc_info=True)
                return
        # Repeated teardown of old history must not release a newer session's
        # workspace or overwrite the shared worker's current assignment.
        task = await self.db.get_task(session.task_id) if session.task_id else None
        manually_paused = task and task.status == TaskStatus.PAUSED and task.resume_after is None
        if manually_paused:
            # Manual cleanup owns release and checkpoints after this confirmed stop.
            await self.db.update_session(
                session.id, state="stopped", desired_state="stopped", end_reason=reason,
            )
            return
        other_live = [row for row in await self.db.list_sessions(agent_id=session.agent_id, live_only=True)
                      if row.id != session.id] if session.agent_id else []
        agent = await self.db.get_agent(session.agent_id) if session.agent_id else None
        still_owned = agent is None or agent.current_task_id in (None, session.task_id)
        if not other_live and still_owned:
            release = await self.db.terminate_pool_session(
                session.id, reason=reason, task_status=task_status
            )
            # An attached or pending integration owner retains this exact
            # session/workspace binding.  It is not safe to mark the worker
            # reusable or remove its claim file until the owner handoff has
            # durably completed.
            if not release.released:
                # Ownership may retain the claim until a guarded handoff,
                # but that must not hide a confirmed process exit. Recovery
                # needs this stopped state while the attachment stays locked.
                try:
                    handle = SessionHandle(
                        name=session.name, provider=session.provider,
                        instance_token=session.instance_token,
                    )
                    if await provider.confirm_stopped(handle):
                        await self.db.update_session_instance(
                            session.id, session.instance_token,
                            require_desired_state="stopped", state="stopped",
                            end_reason=reason,
                        )
                except Exception:
                    logger.warning(
                        "pool session %s: retained-owner stop proof unavailable",
                        session.id, exc_info=True,
                    )
                return
            from src.claim_file import remove_claim_file
            try:
                remove_claim_file(session.work_dir)
            except Exception:
                logger.debug("pool session %s: claim file removal failed", session.id)
        if session.state not in ("stopped", "quarantined"):
            await self.db.update_session(
                session.id, state="stopped", desired_state="stopped", end_reason=reason,
            )
        if session.agent_id and not other_live and still_owned:
            await self.db.update_agent(session.agent_id, state=AgentState.IDLE, current_task_id=None)
        await self.bus.emit(
            "pool.session_drained",
            {
                "project_id": session.project_id,
                "profile_id": session.profile_id,
                "session_id": session.id,
                "name": session.name,
                "reason": reason,
            },
        )
