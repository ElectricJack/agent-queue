"""Recover routing wakeups after a triage run closes or the daemon restarts."""

from __future__ import annotations

import logging

from src.database.queries.triage_queries import triage_reconciliation_projects
from src.playbooks.routing import uses_default_triage

logger = logging.getLogger(__name__)


class TriageMixin:
    async def _reconcile_triage_tasks(self) -> None:
        if not self.config.playbooks.enabled:
            return
        handler = getattr(self, "_command_handler", None)
        if handler is None:
            return
        existing, pending = await triage_reconciliation_projects(self.db)
        manager = getattr(self, "playbook_manager", None)
        for project_id in sorted(pending):
            if manager is not None:
                if not uses_default_triage(manager, project_id):
                    continue
            elif project_id not in existing:
                # Without a configured policy, only previously opted-in tasks
                # can be recovered; do not invent triage for a custom project.
                continue
            # Persisted gates survive a missed event or close-before-drain race.
            # The ensure helper checks holds/live sessions and the seen-ID set.
            result = await handler._cmd_ensure_task(
                {
                    "project_id": project_id,
                    "dedup_key": "triage-open",
                    "profile_id": "triage",
                    "title": "Triage unrouted tasks",
                    "description": "Route pending tasks, then close this task.",
                    "priority": 1,
                }
            )
            if not result.get("success"):
                logger.warning(
                    "Could not reconcile triage for %s: %s", project_id, result.get("error")
                )
