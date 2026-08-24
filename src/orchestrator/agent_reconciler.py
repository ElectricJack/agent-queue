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

    async def reconcile(self) -> ReconcileReport:
        import uuid

        from src.models import Agent, AgentState, ProjectStatus, TaskStatus

        report = ReconcileReport()

        projects = await self._db.list_projects()
        tasks = await self._db.list_tasks()
        agents = await self._db.list_agents()
        workspaces = await self._db.list_workspaces()
        profiles = {p.id: p for p in await self._db.list_profiles()}

        # Sweep orphan BUSY agents (state=BUSY but task missing) → reset to IDLE.
        for a in agents:
            if a.state != AgentState.BUSY:
                continue
            ok = False
            if a.current_task_id:
                t = await self._db.get_task(a.current_task_id)
                ok = t is not None
            if not ok:
                await self._db.update_agent(
                    a.id, state=AgentState.IDLE, current_task_id=None
                )
                a.state = AgentState.IDLE
                a.current_task_id = None
                logger.warning("reconciler: reset orphan BUSY agent %s", a.id)

        # Per-agent project attribution: BUSY agents via current_task_id;
        # IDLE agents via the workspace lock.
        ws_owner: dict[str, str] = {}  # agent_id -> project_id
        for w in workspaces:
            if w.locked_by_agent_id:
                ws_owner[w.locked_by_agent_id] = w.project_id

        agents_by_project: dict[str, list] = {}
        unassigned_idle: list = []
        for a in agents:
            pid = None
            if a.current_task_id:
                t = await self._db.get_task(a.current_task_id)
                if t:
                    pid = t.project_id
            if pid is None:
                pid = ws_owner.get(a.id)
            if pid is None:
                if a.state == AgentState.IDLE:
                    unassigned_idle.append(a)
                continue
            agents_by_project.setdefault(pid, []).append(a)

        # Group READY tasks by project.
        ready_by_project: dict[str, list] = {}
        for t in tasks:
            if t.status == TaskStatus.READY:
                ready_by_project.setdefault(t.project_id, []).append(t)

        # Track per-tick reassignment cap: one reassignment per agent.
        reassigned_this_tick: set[str] = set()

        for project in projects:
            if project.status != ProjectStatus.ACTIVE:
                continue
            ready = ready_by_project.get(project.id, [])
            if not ready:
                continue
            # Resolve unique profile_ids needed.  Cascade per task:
            # task.profile_id → project.default_profile_id → system
            # default.  The last rung is what keeps a project that was
            # never given a default (the common case — playbook- and
            # supervisor-created tasks carry no profile_id) from
            # stalling with READY work and no agents.  It is persisted
            # so Orchestrator._resolve_profile agrees with the profile
            # the agent was actually built for.
            default_pid = project.default_profile_id
            if not default_pid and any(not t.profile_id for t in ready):
                default_pid = await self._backfill_project_default(
                    project, profiles, report
                )

            needed_profiles: set[str] = set()
            for t in ready:
                pid = t.profile_id or default_pid
                if pid:
                    needed_profiles.add(pid)
            if not needed_profiles:
                # Only reachable when the agent_profiles table itself has
                # nothing usable — the system default cannot be picked from
                # an empty set.  Say so, because "set a default_profile_id"
                # is not the fix here; syncing profiles from the vault is.
                reason = (
                    "no resolvable profile_id (no usable agent profiles are "
                    "registered — check vault/agent-types sync)"
                )
                self._warn_once(project.id, reason)
                report.skipped.append((project.id, reason))
                continue

            project_agents = agents_by_project.get(project.id, [])
            existing_profiles = {
                a.profile_id for a in project_agents if a.state == AgentState.IDLE
            }

            for needed in needed_profiles:
                if needed in existing_profiles:
                    continue

                # Try create.
                if len(project_agents) < project.max_concurrent_agents:
                    # Workspace gate (only applies to NEW agent creation —
                    # reassignment reuses an existing agent's workspace lock):
                    # if the runtime requires a workspace, ensure the project
                    # has at least one available (unlocked + enabled).
                    requires_ws = self._runtime_requires_workspace(profiles.get(needed))
                    if requires_ws:
                        # Under worktree mode the gate must measure acquirable
                        # *capacity*, not unlocked rows (worktree-execution
                        # §6.7): a project with one base and zero pre-made
                        # slots has no unlocked row but a full cap of
                        # capacity, because slots are created lazily.  Without
                        # this the reconciler would never create the first
                        # agent and the project would never start.
                        avail = await self._db.count_available_workspaces(
                            project.id,
                            worktree_slot_cap=(
                                project.max_concurrent_agents
                                if self._worktrees_enabled
                                else None
                            ),
                        )
                        if avail == 0:
                            report.skipped.append(
                                (project.id, f"no available workspace for {needed}")
                            )
                            continue

                    # Adopt one unassigned-idle agent if available; else create.
                    if unassigned_idle:
                        adopted = unassigned_idle.pop(0)
                        await self._db.update_agent(adopted.id, profile_id=needed)
                        report.reassigned.append(
                            (adopted.id, adopted.profile_id, needed)
                        )
                        adopted.profile_id = needed
                        project_agents.append(adopted)
                        existing_profiles.add(needed)
                        continue
                    agent = Agent(
                        id=f"agent-{uuid.uuid4().hex[:12]}",
                        name=f"{needed}-{len(agents) + 1}",
                        profile_id=needed,
                        state=AgentState.IDLE,
                    )
                    await self._db.create_agent(agent)
                    agents.append(agent)
                    project_agents.append(agent)
                    existing_profiles.add(needed)
                    report.created.append((project.id, needed))
                    continue

                # At cap — try to reassign an idle, mismatched-profile agent.
                # Prefer agents whose profile_id is no longer in agent_profiles.
                idle_in_project = [
                    a for a in project_agents
                    if a.state == AgentState.IDLE
                    and a.profile_id != needed
                    and a.id not in reassigned_this_tick
                ]
                if not idle_in_project:
                    continue
                orphans = [a for a in idle_in_project if a.profile_id not in profiles]
                target = orphans[0] if orphans else idle_in_project[0]
                old = target.profile_id
                await self._db.update_agent(target.id, profile_id=needed)
                target.profile_id = needed
                reassigned_this_tick.add(target.id)
                report.reassigned.append((target.id, old, needed))
                # Update existing_profiles tracker.
                existing_profiles.discard(old)
                existing_profiles.add(needed)

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
            from src.runtimes.acpx import ACPXRuntime
            from src.runtimes.claude_sdk import ClaudeSDKRuntime
            from src.runtimes.supervisor import Supervisor

            runtime_classes = {
                ACPXRuntime.name: ACPXRuntime,
                ClaudeSDKRuntime.name: ClaudeSDKRuntime,
                Supervisor.name: Supervisor,
            }
            cls = runtime_classes.get(profile.runtime)
            if cls is None:
                return True
            return cls.requires_workspace
        except Exception:
            return True

    def _warn_once(self, project_id: str, reason: str) -> None:
        if self._warned_projects.get(project_id) == reason:
            return
        self._warned_projects[project_id] = reason
        logger.warning(
            "reconciler: project=%s has READY tasks but %s", project_id, reason
        )
