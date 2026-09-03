"""Worker pools — sizing and convergence (swarm-work-model §11).

One cascade step per tick, right after ``_schedule``: measure supply and
demand per ``(project, profile)``, ask the pure :func:`~src.scheduler.size_pools`
what to do, then start or drain sessions to converge.  The step reads one
``count_ready_by_profile`` and one ``list_sessions`` per active project with
a pool profile — no per-task queries.

A pool profile is any :class:`~src.models.AgentProfile` with
``lifecycle == "pool"``.  Its tasks are never assigned by the push scheduler
(``Orchestrator._schedule`` and ``_is_session_routed`` both exclude them,
and ``AgentReconciler`` never creates a push agent row for one) — instead a
pool of long-lived ``lifecycle: pool`` sessions claims work in a loop via
``aq task claim``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from src.models import AgentProfile, Agent, AgentState, ProjectStatus, SessionRecord, Task, TaskStatus
from src.orchestrator.base_workspace import base_checkout_refusal
from src.scheduler import PoolKey, PoolSupply, size_pools
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
#: workspace pool does *not* quarantine — see ``_launch_pool_session``.
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
        """``(until, reason)`` for a key still inside its window, else ``(None, None)``."""
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
        every ``lifecycle: pool`` profile is a pool for every active project.
        Sizing stays per project at runtime (one ``PoolKey`` per
        project/profile, each still under that project's
        ``max_concurrent_agents``); only the configuration is global.

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

    async def _measure_pools(self, project_ids: set[str] | None = None):
        """Supply/demand/bounds snapshot for every active project with a pool profile.

        Returns ``(supply, demand, bounds, profiles_by_key, project_caps, projects)``
        — ``projects`` and ``profiles_by_key`` are what ``_reconcile_pools``
        needs to actually launch a session for a ``PoolKey`` the sizer picked.

        One ``list_profiles()`` for the whole tick (shared across every
        project's ``_pool_profiles`` lookup), one ``count_ready_by_profile``
        and one ``list_sessions`` per active project with a pool profile.
        """
        supply: dict[PoolKey, PoolSupply] = {}
        demand: dict[PoolKey, int] = {}
        bounds: dict[PoolKey, tuple[int, int | None]] = {}
        profiles_by_key: dict[PoolKey, object] = {}
        project_caps: dict[str, int | None] = {}
        projects: dict[str, object] = {}

        system_profiles = await self.db.list_profiles()

        for project in await self.db.list_projects():
            if project.status != ProjectStatus.ACTIVE:
                continue
            if project_ids is not None and project.id not in project_ids:
                continue
            pool_profiles = await self._pool_profiles(project.id, system_profiles=system_profiles)
            if not pool_profiles:
                continue

            projects[project.id] = project
            project_caps[project.id] = project.max_concurrent_agents
            ready_by_profile = await self.db.count_ready_by_profile(project.id)
            unrouted_ready = ready_by_profile.get(None, 0)
            default_profile_id = await self._effective_default_profile_id(project)
            sessions = await self.db.list_sessions(lifecycle="pool", project_id=project.id)
            sessions_by_profile: dict[str, list] = {}
            for s in sessions:
                sessions_by_profile.setdefault(s.profile_id, []).append(s)

            for profile_id, profile in pool_profiles.items():
                key = PoolKey(project.id, profile_id)
                profiles_by_key[key] = profile
                bounds[key] = (profile.min_active or 0, profile.max_active)
                ready = ready_by_profile.get(profile_id, 0)
                if default_profile_id == profile_id:
                    ready += unrouted_ready
                demand[key] = ready

                sup = PoolSupply()
                rows = sorted(
                    sessions_by_profile.get(profile_id, []),
                    key=lambda s: s.started_at or 0.0,
                )
                for s in rows:
                    if s.state not in _LIVE_STATES:
                        continue
                    if s.state == "starting":
                        sup.starting += 1
                    elif s.state == "draining" or s.desired_state == "stopped":
                        sup.draining += 1
                    elif s.task_id or s.claim_phase:
                        sup.running_busy += 1
                    else:
                        sup.running_idle += 1
                        sup.idle_session_ids.append(s.id)
                supply[key] = sup

        return supply, demand, bounds, profiles_by_key, project_caps, projects

    async def _reconcile_pools(self) -> None:
        """The pool cascade step: measure, size, converge.  No-op unless enabled."""
        if not (self.config.swarm.enabled and self.config.sessions.enabled):
            return

        (
            supply,
            demand,
            bounds,
            profiles_by_key,
            project_caps,
            projects,
        ) = await self._measure_pools()
        now = time.time()
        actions, self._pool_surplus_since = size_pools(
            supply=supply,
            demand=demand,
            bounds=bounds,
            project_caps=project_caps,
            # No config field for a pool-wide global cap exists yet; the
            # project cap (``max_concurrent_agents``) is the only bound in
            # effect until one is added.
            global_cap=None,
            surplus_since=self._pool_surplus_since,
            now=now,
            scale_down_grace=self.config.swarm.scale_down_grace,
            max_starts_per_tick=self.config.swarm.max_starts_per_tick,
            max_drains_per_tick=self.config.swarm.max_drains_per_tick,
        )

        for action in actions:
            executed = 0
            if action.kind == "start":
                until, _reason = self._pool_quarantine_state(
                    action.key.project_id, action.key.profile_id, now
                )
                if until:
                    continue
                for _ in range(action.count):
                    sid = await self._launch_pool_session(
                        projects[action.key.project_id], profiles_by_key[action.key]
                    )
                    if sid is None:
                        break
                    executed += 1
            else:
                for sid in action.session_ids:
                    await self.db.update_session(sid, desired_state="stopped")
                    executed += 1
            if executed:
                # Persist *and* emit.  The bus is in-process and its
                # WebSocket forward is live-only -- a client not connected at
                # the moment of the scale never sees it -- so without the
                # audit row a scaling decision left no trace any operator
                # surface could read after the fact: ``aq pool status`` shows
                # the current shape, never the fact that it changed or when.
                # ``aq system get-recent-events --event-type pool.scaled`` is
                # the answer to "why did a worker appear at 03:14?".
                await self.db.log_event(
                    "pool.scaled",
                    project_id=action.key.project_id,
                    payload=f"{action.kind} {executed} {action.key.profile_id}",
                )
                await self.bus.emit(
                    "pool.scaled",
                    {
                        "project_id": action.key.project_id,
                        "profile_id": action.key.profile_id,
                        "kind": action.kind,
                        "count": executed,
                    },
                )

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

        A starved pool (no workspace kind, no free workspace) is expected,
        not exceptional, and does not quarantine the key — the next tick's
        demand may simply find a workspace freed.  Every other failure
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
                          name=f"{profile.id}-{uuid.uuid4().hex[:4]}", profile_id=profile.id,
                          origin="pool")
            worker_profile = resolve_agent_profile(agent, profiles) or profile
            mismatch = task_agent_mismatch(
                requirement, agent, task_profile=profile, agent_profile=worker_profile,
                harness_registry=self.harness_registry, intelligence_classes=classes,
            )
            if mismatch:
                logger.info("pool %s/%s cannot start: %s", project.id, profile.id, mismatch)
                return None
            # Only the fallback grows the roster. A persisted deletion opts
            # out of automatic growth; compatible definitions were tried above.
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
                await _rollback("starved: no project-repo workspace kind", quarantine=False)
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
                await _rollback("starved: no free workspace", quarantine=False)
                return None

            work_dir = workspace.workspace_path

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
                    session_id=session_id, task_id=None, project_id=project.id
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
            await self.db.terminate_pool_session(session.id, reason=reason, task_status=task_status)
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
