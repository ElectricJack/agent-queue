"""Lazy agent supply — see
docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §4.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agents.routing import resolve_agent_profile, resolve_task_profile, task_agent_mismatch
from src.database import Database

logger = logging.getLogger(__name__)


async def reset_stale_busy_agent(
    db: Database, agent, *, bus=None, reset_state: bool = True
) -> None:
    """Reset a BUSY agent with no live session to IDLE, clearing its task pin.

    Import path: ``src.orchestrator.agent_reconciler.reset_stale_busy_agent``.

    ``agent`` may be an :class:`~src.models.Agent` instance or a bare agent
    id string, so this is callable from contexts (e.g. a doctor ``--fix``
    check) that never build a full ``Agent``. ``bus`` is optional — a caller
    with no event bus (e.g. an offline doctor check) can still perform the
    reset; when a bus is given, one ``agent.updated`` event is emitted after
    the write.

    ``reset_state=False`` clears the pointer alone and leaves the agent's
    state exactly as it was. Only a BUSY agent is *supposed* to become IDLE:
    production sets ERROR and RETIRED without clearing ``current_task_id``
    (``src/orchestrator/pools.py``), and RETIRED is what gates workspace
    reclaim, so a caller that sweeps agents in any state (the
    ``agents.dangling_current_task`` doctor fix) must not write IDLE over
    them. The reconciler's own rescue rule only ever sees BUSY agents and
    keeps the default.

    Never touches ``tasks.assigned_agent_id`` or any integration-owner row:
    a retained claim keeps its evidence on the task side, not the agent's
    transient pointer.
    """
    from src.models import AgentState

    agent_id = agent.id if hasattr(agent, "id") else agent
    fields = {"current_task_id": None}
    if reset_state:
        fields["state"] = AgentState.IDLE
    await db.update_agent(agent_id, **fields)
    if bus is not None:
        await bus.emit("agent.updated", {
            "event_type": "agent.updated", "agent_id": agent_id,
        })


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

    def __init__(
        self, db: Database, *, worktrees_enabled: bool = False, data_dir: str | None = None,
        bus=None,
    ):
        self._db = db
        self._warned_projects: dict[str, str] = {}
        # Rollout gate (worktree-execution §5).  While False the workspace
        # gate below counts inventory exactly as it does today.
        self._worktrees_enabled = worktrees_enabled
        self._data_dir = data_dir
        # Optional: announces agent.updated after each stale-BUSY reset.
        # None is fine (e.g. tests that don't care about the event).
        self._bus = bus

    async def reconcile(
        self, *, suppressed_profile_ids: frozenset[str] | None = None,
        suppressed_agent_ids: frozenset[str] | None = None,
        harness_registry=None, intelligence_classes: dict | None = None,
        ready_tasks=None, launching_agent_ids: set[str] | None = None,
    ) -> ReconcileReport:
        """Supply durable global workers without changing any existing definition."""
        import time
        import uuid
        from src.models import Agent, AgentState, ProjectStatus, TaskStatus

        report = ReconcileReport()
        # Workers and profiles whose provider is unavailable
        # (provider-failover D11): neither idle supply nor grown here.
        suppressed_profiles = suppressed_profile_ids or frozenset()
        suppressed_agents = suppressed_agent_ids or frozenset()
        projects = await self._db.list_projects()
        tasks = await self._db.list_tasks()
        agents = await self._db.list_agents()
        profiles = {p.id: p for p in await self._db.list_profiles()}
        from src.profiles.catalog import active_catalog_profile_ids, shipped_profile_catalog

        active_catalog_ids = (
            active_catalog_profile_ids(self._data_dir) if self._data_dir else None
        )
        catalog_ids = {profile.id for profile in shipped_profile_catalog()}
        live = await self._db.list_sessions(live_only=True)
        live_agents = {row.agent_id for row in live if row.agent_id}
        live_agents.update(launching_agent_ids or ())
        # Legacy task sessions may not have been linked before adoption.
        live_tasks = {row.task_id for row in live if row.task_id}
        by_task = {task.id: task for task in tasks}
        for agent in agents:
            if agent.state != AgentState.BUSY or agent.id in live_agents:
                continue
            if agent.current_task_id in live_tasks:
                continue
            task = by_task.get(agent.current_task_id)
            if task is not None and task.status in (
                TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS,
            ):
                continue
            # Pool launch reservations have no task yet; do not steal an
            # identity while its provider is still starting the process.
            if agent.last_heartbeat and time.time() - agent.last_heartbeat < 120:
                continue
            await reset_stale_busy_agent(self._db, agent, bus=self._bus)
            agent.state = AgentState.IDLE
            agent.current_task_id = None

        idle = [
            agent
            for agent in agents
            if agent.state == AgentState.IDLE
            and agent.current_task_id is None
            and agent.enabled
            and agent.role == "worker"
            and agent.profile_id not in suppressed_profiles
            and agent.id not in suppressed_agents
            and agent.id not in live_agents
            and agent.profile_id in profiles
        ]
        for project in projects:
            if project.status != ProjectStatus.ACTIVE:
                continue
            ready_source = ready_tasks if ready_tasks is not None else tasks
            ready = [
                task
                for task in ready_source
                if task.project_id == project.id and task.status == TaskStatus.READY
                and not task.is_blocked
            ]
            if not ready:
                continue
            # A global worker supplies only work that preserves its saved
            # execution identity. An idle triage worker cannot suppress supply
            # for an explicitly routed Codex/deep task.
            def supplied(task):
                profile = resolve_task_profile(task, project, profiles)
                if task.profile_id and profile is None:
                    return False
                return any(
                    task_agent_mismatch(
                        task, worker, task_profile=profile,
                        agent_profile=resolve_agent_profile(worker, profiles),
                        harness_registry=harness_registry,
                        intelligence_classes=intelligence_classes,
                    ) is None
                    for worker in idle
                )

            uncovered = [task for task in ready if not supplied(task)]
            if not uncovered:
                # Keep a missing project default missing when existing workers
                # can supply their own defaults.
                continue
            default_pid = project.default_profile_id
            if not default_pid and any(not task.profile_id for task in uncovered):
                default_pid = await self._backfill_project_default(project, profiles, report)
            needed = {
                (task.profile_id or default_pid, task.intelligence_class or ""): task
                for task in uncovered if task.profile_id or default_pid
            }
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
            for profile_id, task_class in sorted(needed):
                requested = needed[(profile_id, task_class)]
                profile = profiles.get(profile_id)
                if not profile or getattr(profile, "lifecycle", "task") == "pool":
                    continue
                if (
                    active_catalog_ids is not None
                    and profile_id in catalog_ids
                    and profile_id not in active_catalog_ids
                ):
                    report.skipped.append(
                        (project.id, f"profile {profile_id} is unavailable; rerun aq install after provider setup")
                    )
                    continue
                if profile_id in suppressed_profiles or profile.id in suppressed_profiles:
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
                    intelligence_class=task_class or None,
                )
                mismatch = task_agent_mismatch(
                    requested, agent, task_profile=profile, agent_profile=profile,
                    harness_registry=harness_registry,
                    intelligence_classes=intelligence_classes,
                )
                if mismatch:
                    report.skipped.append((project.id, mismatch))
                    continue
                # Create a fresh identity when existing workers cannot serve demand.
                # Deleted identities remain historical records, not scaling policy.
                if not await self._db.create_automatic_agent(agent):
                    report.skipped.append(
                        (project.id, "automatic worker creation declined")
                    )
                    break
                agents.append(agent)
                idle.append(agent)
                report.created.append((project.id, profile_id))
                remaining -= 1
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

        from src.profiles.catalog import active_catalog_profile_ids

        eligible = active_catalog_profile_ids(self._data_dir) if self._data_dir else None
        chosen = select_default_profile_id(profiles.values(), eligible_profile_ids=eligible)
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
        """Always True: no Runtime class decides workspace needs any more.

        Every agent runs as a tmux session, and a session's workspace need
        comes from the profile's own ``needs_workspace`` — not from a runtime
        class.  Kept as a seam so the caller reads the same way it did when
        the in-process Supervisor was the one exception.
        """
        return True

    def _warn_once(self, project_id: str, reason: str) -> None:
        if self._warned_projects.get(project_id) == reason:
            return
        self._warned_projects[project_id] = reason
        logger.warning("reconciler: project=%s has READY tasks but %s", project_id, reason)
