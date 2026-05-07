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


class AgentReconciler:
    """Lazy-creates agent rows so the scheduler always has idle slots
    when there's dispatchable work. Called once per orchestrator tick,
    before Scheduler.schedule(). Does not assign tasks — only ensures
    supply matches demand subject to project.max_concurrent_agents.
    """

    def __init__(self, db: Database):
        self._db = db
        self._warned_projects: dict[str, str] = {}

    async def reconcile(self) -> ReconcileReport:
        import uuid
        import time as _t

        from src.models import Agent, AgentState, ProjectStatus, TaskStatus

        report = ReconcileReport()

        projects = await self._db.list_projects()
        tasks = await self._db.list_tasks()
        agents = await self._db.list_agents()
        workspaces = await self._db.list_workspaces()

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

        for project in projects:
            if project.status != ProjectStatus.ACTIVE:
                continue
            ready = ready_by_project.get(project.id, [])
            if not ready:
                continue
            # Resolve unique profile_ids needed.
            needed_profiles: set[str] = set()
            for t in ready:
                pid = t.profile_id or project.default_profile_id
                if pid:
                    needed_profiles.add(pid)
            if not needed_profiles:
                self._warn_once(project.id, "no resolvable profile_id")
                report.skipped.append((project.id, "no resolvable profile_id"))
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

        return report

    def _warn_once(self, project_id: str, reason: str) -> None:
        if self._warned_projects.get(project_id) == reason:
            return
        self._warned_projects[project_id] = reason
        logger.warning(
            "reconciler: project=%s has READY tasks but %s", project_id, reason
        )
