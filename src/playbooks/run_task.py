"""Best-effort projection of playbook runs into the task graph."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.models import TaskStatus

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler

logger = logging.getLogger(__name__)

_RUN_STATUS_TO_TASK_STATUS = {
    "running": TaskStatus.IN_PROGRESS,
    "paused": TaskStatus.PAUSED,
    "completed": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "timed_out": TaskStatus.FAILED,
    "cancelled": TaskStatus.FAILED,
}


def playbook_status_to_task_status(status: str) -> TaskStatus:
    """Return the task-graph status representing a playbook run status."""
    try:
        return _RUN_STATUS_TO_TASK_STATUS[status]
    except KeyError as exc:
        raise ValueError(f"Unsupported playbook run status: {status}") from exc


async def sync_playbook_run_task(
    handler: CommandHandler,
    *,
    project_id: str | None,
    playbook_id: str,
    run_id: str,
    status: str,
) -> str | None:
    """Ensure and update the presentation task for a project-scoped run.

    Projection failures are logged and never escape into playbook execution.
    Looking through every task status before ``ensure_task`` also prevents a
    repeated terminal notification from creating a second row: ordinary
    reusable deduplication intentionally ignores terminal tasks.
    """
    if not project_id:
        return None

    dedup_key = f"playbook-run:{run_id}"
    try:
        task_status = playbook_status_to_task_status(status)
        existing = next(
            (
                task
                for task in await handler.db.list_tasks(project_id=project_id)
                if task.dedup_key == dedup_key
            ),
            None,
        )
        if existing is None:
            ensured = await handler.execute(
                "ensure_task",
                {
                    "project_id": project_id,
                    "title": f"Playbook run: {playbook_id}",
                    "description": f"Playbook {playbook_id} run {run_id}",
                    "dedup_key": dedup_key,
                    "initial_status": task_status.value,
                },
            )
            if not isinstance(ensured, dict):
                raise RuntimeError("ensure_task returned a non-object response")
            if ensured.get("success") is False or ensured.get("error"):
                raise RuntimeError(ensured.get("error") or "ensure_task failed")
            task_id = ensured.get("task_id")
            if not task_id:
                raise RuntimeError("ensure_task returned no task_id")
            existing = await handler.db.get_task(str(task_id))
        else:
            task_id = existing.id

        if existing is None:
            raise RuntimeError(f"playbook run task '{task_id}' was not persisted")
        if existing.status != task_status:
            await handler.db.transition_task(
                str(task_id),
                task_status,
                context="playbook_run_projection",
                force=True,
                _manual_pause_control=True,
            )
            updated = await handler.db.get_task(str(task_id))
            emit = getattr(handler, "_emit_task_graph_change", None)
            if updated is not None and emit is not None:
                await emit("task.updated", updated)
        return str(task_id)
    except Exception:
        logger.warning(
            "Could not sync task projection for playbook %s run %s (%s)",
            playbook_id,
            run_id,
            status,
            exc_info=True,
        )
        return None
