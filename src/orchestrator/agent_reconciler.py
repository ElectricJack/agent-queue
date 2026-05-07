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
        return ReconcileReport()
