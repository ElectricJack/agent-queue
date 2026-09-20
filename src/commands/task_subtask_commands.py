"""The four subtask commands — durable checklist rows inside one task.

Wired exactly like task comments (``task_comment_commands.py``): the same
scope fence for reads (``_assert_task_in_scope``) and the same claim-epoch
write fence (``_task_findings_write_fence``, imported from
``task_comment_commands`` and reused, never copied). A subtask is never
scheduled or claimed on its own -- see
``src/database/queries/task_subtask_queries.py``.
"""

from __future__ import annotations

import logging

from src.database.queries.task_subtask_queries import (
    MAX_SUBTASK_CONTEXT,
    MAX_SUBTASK_TITLE,
    MAX_SUBTASKS_PER_CALL,
    MAX_SUBTASKS_PER_TASK,
    SUBTASK_STATUSES,
)

logger = logging.getLogger(__name__)

#: Re-exported from ``src/database/queries/task_subtask_queries.py``, where
#: they live beside the table whose check constraints they mirror. Kept
#: importable from here because these commands were their original home and
#: callers (and tests) still reach for them by this path.
__all__ = [
    "MAX_SUBTASK_CONTEXT",
    "MAX_SUBTASK_TITLE",
    "MAX_SUBTASKS_PER_CALL",
    "MAX_SUBTASKS_PER_TASK",
    "TaskSubtaskCommandsMixin",
]


class TaskSubtaskCommandsMixin:
    """``task_subtask_add`` / ``task_subtasks`` / ``task_subtask_get`` / ``task_subtask_update``."""

    async def _resolve_subtask_task(self, args: dict) -> tuple[object | None, dict | None]:
        """``(task, error)`` -- ``task_id`` from args, else the session's held task.

        Mirrors ``_cmd_task_close``'s ``args.get("task_id") or await
        self._scoped_held_task_id()`` so a pool worker never has to name the
        task it already holds.
        """
        task_id = args.get("task_id") or await self._scoped_held_task_id()
        if not task_id:
            return None, {
                "success": False,
                "error": "no task_id and the session holds no task",
            }
        task = await self.db.get_task(task_id)
        if task is None:
            return None, {
                "success": False,
                "code": "subtasks.not_found",
                "error": f"Task '{task_id}' not found",
            }
        return task, None

    async def _subtask_counts(self, task_id: str) -> tuple[int, int]:
        counts = await self.db.count_task_subtasks([task_id])
        return counts.get(task_id, (0, 0))

    async def _emit_task_subtasks_updated(self, task, total: int, settled: int) -> None:
        # Titles/context are agent-authored free text; only counts and
        # identifiers are safe to publish on the shared invalidation bus.
        payload = {
            "task_id": task.id,
            "project_id": task.project_id,
            "total": total,
            "settled": settled,
        }
        try:
            await self.orchestrator.bus.emit("task.subtasks_updated", payload)
        except Exception:
            logger.warning(
                "Could not publish task.subtasks_updated for %s", task.id, exc_info=True
            )

    def _validate_subtask_item(self, item) -> tuple[dict | None, str | None]:
        """``(cleaned, error)`` for one ``{"title", "context"?}`` entry."""
        if not isinstance(item, dict):
            return None, "each subtask must be an object with a title"
        title = item.get("title")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_SUBTASK_TITLE:
            return None, f"each subtask title must be 1 to {MAX_SUBTASK_TITLE} characters"
        context = item.get("context", "")
        if not isinstance(context, str) or len(context) > MAX_SUBTASK_CONTEXT:
            return (
                None,
                f"subtask context must be a string of at most {MAX_SUBTASK_CONTEXT} characters",
            )
        return {"title": title.strip(), "context": context}, None

    async def _cmd_task_subtask_add(self, args: dict) -> dict:
        task, error = await self._resolve_subtask_task(args)
        if error:
            return error

        items = args.get("subtasks")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_SUBTASKS_PER_CALL:
            return {
                "success": False,
                "error": f"subtasks must be a list of 1 to {MAX_SUBTASKS_PER_CALL} items",
            }
        cleaned = []
        for item in items:
            row, item_error = self._validate_subtask_item(item)
            if item_error:
                return {"success": False, "error": item_error}
            cleaned.append(row)

        fence, error = await self._task_findings_write_fence(task, args)
        if error:
            return error

        try:
            rows = await self.db.add_task_subtasks(task.id, task.project_id, cleaned)
        except ValueError as exc:
            if str(exc) == "subtask_limit":
                return {
                    "success": False,
                    "code": "subtasks.limit",
                    "error": (
                        f"adding {len(cleaned)} subtask(s) would exceed the "
                        f"{MAX_SUBTASKS_PER_TASK} per-task limit"
                    ),
                }
            raise

        total, settled = await self._subtask_counts(task.id)
        await self._emit_task_subtasks_updated(task, total, settled)
        return {"success": True, "task_id": task.id, "subtasks": rows}

    async def _cmd_task_subtasks(self, args: dict) -> dict:
        task, error = await self._resolve_subtask_task(args)
        if error:
            return error
        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        rows = await self.db.list_task_subtasks(task.id)
        total, settled = await self._subtask_counts(task.id)
        return {
            "success": True,
            "task_id": task.id,
            "subtasks": rows,
            "total": total,
            "settled": settled,
        }

    async def _cmd_task_subtask_get(self, args: dict) -> dict:
        task, error = await self._resolve_subtask_task(args)
        if error:
            return error
        ordinal = args.get("ordinal")
        if type(ordinal) is not int or ordinal < 1:
            return {"success": False, "error": "ordinal must be a positive integer"}
        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        row = await self.db.get_task_subtask(task.id, ordinal)
        if row is None:
            return {
                "success": False,
                "code": "subtasks.not_found",
                "error": f"No subtask #{ordinal} on task '{task.id}'",
            }
        return {"success": True, "subtask": row}

    async def _cmd_task_subtask_update(self, args: dict) -> dict:
        task, error = await self._resolve_subtask_task(args)
        if error:
            return error
        ordinal = args.get("ordinal")
        if type(ordinal) is not int or ordinal < 1:
            return {"success": False, "error": "ordinal must be a positive integer"}
        status = args.get("status")
        if status is not None and status not in SUBTASK_STATUSES:
            return {
                "success": False,
                "code": "subtasks.invalid_status",
                "error": f"status must be one of {', '.join(SUBTASK_STATUSES)}",
            }
        note = args.get("note")
        if note is not None and not isinstance(note, str):
            return {"success": False, "error": "note must be a string"}
        if status is None and note is None:
            return {"success": False, "error": "status or note is required"}

        fence, error = await self._task_findings_write_fence(task, args)
        if error:
            return error

        row = await self.db.update_task_subtask(task.id, ordinal, status=status, note=note)
        if row is None:
            return {
                "success": False,
                "code": "subtasks.not_found",
                "error": f"No subtask #{ordinal} on task '{task.id}'",
            }
        total, settled = await self._subtask_counts(task.id)
        await self._emit_task_subtasks_updated(task, total, settled)
        return {"success": True, "subtask": row, "total": total, "settled": settled}
