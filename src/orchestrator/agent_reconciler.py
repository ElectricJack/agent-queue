"""Lazy agent supply — see
docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §4.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Outcome of one AgentReconciler.reconcile() pass."""

    created: list[tuple[str, str]] = field(default_factory=list)  # [(project_id, profile_id)]
    reassigned: list[tuple[str, str, str]] = field(default_factory=list)  # [(agent_id, old, new)]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # [(project_id, reason)]
    # Projects whose NULL default_profile_id was backfilled this pass.
    defaults_backfilled: list[tuple[str, str]] = field(  # [(project_id, profile_id)]
        default_factory=list
    )


class AgentReconciler:
    """Lazy-creates agent rows so the scheduler always has idle slots
    when there's dispatchable work. Called once per orchestrator tick,
    before Scheduler.schedule(). Does not assign tasks — only ensures
    supply matches demand subject to project.max_concurrent_agents.
    """

    def __init__(self, db: Database, *, worktrees_enabled: bool = False):
        self._db = db
        self._warned_projects: dict[str, str] = {}
        # Rollout gate (worktree-execution §5).  While False the workspace
        # gate below counts inventory exactly as it does today.
        self._worktrees_enabled = worktrees_enabled

    async def reconcile(
        self, *, provider_cooldowns: dict[str, float] | None = None
    ) -> ReconcileReport:
        """Supply durable global workers without changing any existing definition."""
        import time
        import uuid
        from src.models import Agent, AgentState, ProjectStatus, TaskStatus

        report = ReconcileReport()
        cooldowns = provider_cooldowns or {}
        now = time.time()
        projects = await self._db.list_projects()
        tasks = await self._db.list_tasks()
        agents = await self._db.list_agents()
        profiles = {p.id: p for p in await self._db.list_profiles()}
        live = await self._db.list_sessions(live_only=True)
        live_agents = {row.agent_id for row in live if row.agent_id}
        # Legacy task sessions may not have been linked before adoption.
        live_tasks = {row.task_id for row in live if row.task_id}
        by_task = {task.id: task for task in tasks}
        for agent in agents:
            if agent.state != AgentState.BUSY or agent.id in live_agents:
                continue
            if agent.current_task_id in by_task or agent.current_task_id in live_tasks:
                continue
            # Pool launch reservations have no task yet; do not steal an
            # identity while its provider is still starting the process.
            if agent.last_heartbeat and time.time() - agent.last_heartbeat < 120:
                continue
            await self._db.update_agent(agent.id, state=AgentState.IDLE, current_task_id=None)
            agent.state = AgentState.IDLE
            agent.current_task_id = None

        idle = [
            agent
            for agent in agents
            if agent.state == AgentState.IDLE
            and agent.current_task_id is None
            and agent.enabled
            and agent.role == "worker"
            and cooldowns.get(agent.profile_id, 0) <= now
            and agent.id not in live_agents
            and agent.profile_id in profiles
        ]
        supplied = bool(idle)
        for project in projects:
            if project.status != ProjectStatus.ACTIVE:
                continue
            ready = [
                task
                for task in tasks
                if task.project_id == project.id and task.status == TaskStatus.READY
            ]
            if not ready:
                continue
            # An existing global worker already supplies this demand. Keep a
            # missing project default missing so its saved profile can apply.
            if supplied:
                continue
            default_pid = project.default_profile_id
            if not default_pid and any(not task.profile_id for task in ready):
                default_pid = await self._backfill_project_default(project, profiles, report)
            needed = {task.profile_id or default_pid for task in ready}
            needed.discard(None)
            if not needed:
                reason = "no resolvable profile_id (no usable agent profiles are registered)"
                self._warn_once(project.id, reason)
                report.skipped.append((project.id, reason))
                continue
            busy = sum(
                1
                for agent in agents
                if agent.current_task_id in by_task
                and by_task[agent.current_task_id].project_id == project.id
            )
            remaining = max(0, project.max_concurrent_agents - busy)
            for profile_id in sorted(needed):
                profile = profiles.get(f"project:{project.id}:{profile_id}") or profiles.get(
                    profile_id
                )
                if not profile or getattr(profile, "lifecycle", "task") == "pool":
                    continue
                if max(cooldowns.get(profile_id, 0), cooldowns.get(profile.id, 0)) > now:
                    continue
                # The named supervisor is seeded separately; no per-project
                # or task-demand duplicates of that global identity.
                if profile_id == "supervisor" or profile.runtime == "supervisor":
                    continue
                if remaining <= 0:
                    break
                if self._runtime_requires_workspace(profile):
                    count = await self._db.count_available_workspaces(
                        project.id,
                        worktree_slot_cap=(
                            project.max_concurrent_agents if self._worktrees_enabled else None
                        ),
                    )
                    if not count:
                        report.skipped.append(
                            (project.id, f"no available workspace for {profile_id}")
                        )
                        continue
                agent = Agent(
                    id=f"agent-{uuid.uuid4().hex[:12]}",
                    name=f"{profile_id}-{len(agents) + 1}",
                    profile_id=profile_id,
                )
                # A deletion means the user sized this global roster. Keep
                # reusing its workers, but never grow it back automatically;
                # untouched registries retain their normal lazy bootstrap.
                if not await self._db.create_automatic_agent(agent):
                    report.skipped.append(
                        (project.id, "roster was manually sized; add an agent explicitly")
                    )
                    break
                agents.append(agent)
                report.created.append((project.id, profile_id))
                remaining -= 1
                supplied = True
        return report

    async def _backfill_project_default(
        self, project, profiles: dict, report: ReconcileReport
    ) -> str | None:
        """Pick and persist a ``default_profile_id`` for *project*.

        Called only when the project has READY tasks that carry no
        explicit ``profile_id`` and the project has no default of its
        own.  Persisting (rather than resolving on the fly each tick)
        matters for two reasons: the choice stays stable across daemon
        restarts, and ``Orchestrator._resolve_profile`` reads the same
        column — so the profile the task actually executes under matches
        the one its agent row was created for.

        Returns the chosen profile id, or ``None`` when no profile is
        eligible (empty/unsynced ``agent_profiles`` table).
        """
        from src.profiles.default_selection import select_default_profile_id

        chosen = select_default_profile_id(profiles)
        if not chosen:
            return None
        try:
            await self._db.update_project(project.id, default_profile_id=chosen)
        except Exception:
            # A failed write must not take down the tick; we simply retry
            # next pass.  Returning the id anyway would desync the DB
            # from the agent rows we are about to create.
            logger.exception(
                "reconciler: failed to backfill default_profile_id=%s for project=%s",
                chosen,
                project.id,
            )
            return None
        project.default_profile_id = chosen
        report.defaults_backfilled.append((project.id, chosen))
        logger.info(
            "reconciler: project=%s had READY tasks with no resolvable profile_id; "
            "backfilled default_profile_id=%s",
            project.id,
            chosen,
        )
        return chosen

    def _runtime_requires_workspace(self, profile) -> bool:
        """Look up runtime.requires_workspace via the profile's runtime name.

        Maps the runtime-name string to the ClassVar on the runtime class
        directly. This avoids depending on a populated RuntimeRegistry
        (the daemon-wide registry only includes the supervisor singleton
        if one is wired, but the supervisor runtime class itself always
        knows its workspace requirement).

        Defensive: unknown profile / runtime → assume requires workspace
        (the safer default), so we don't over-create agents that can't
        actually dispatch.
        """
        if profile is None:
            return True
        try:
            from src.runtimes.supervisor import Supervisor

            # Supervisor is the only remaining Runtime; every other profile
            # runs as a tmux session and its workspace need comes from the
            # profile's own ``needs_workspace``, not from a runtime class.
            if profile.runtime == Supervisor.name:
                return Supervisor.requires_workspace
            return True
        except Exception:
            return True

    def _warn_once(self, project_id: str, reason: str) -> None:
        if self._warned_projects.get(project_id) == reason:
            return
        self._warned_projects[project_id] = reason
        logger.warning("reconciler: project=%s has READY tasks but %s", project_id, reason)
