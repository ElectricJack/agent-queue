"""Surface commands mixin — the agent-facing context and schema surface.

Phase S0 (docs/specs/implementation/aq-surface.md §9) fills in the output
contract slice: ``get_schema`` (backs ``aq schema``) and the ``task_show`` /
``task_set`` pair (back ``aq task show|set|details``).  ``prime`` and
``task_handoff`` (Phase S1), ``task_close`` / ``task_heartbeat`` /
``ask_human`` (unscheduled in this spec's phase checklist beyond the §3
inventory table) are not implemented here yet.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.  ``CommandHandler.execute`` returns this
dict verbatim (no implicit ``"success"`` key is injected at that layer); the
wire-level ``{"ok": bool, "result"|"error"}`` shape is added by
``/api/execute`` (``src/api/execute.py``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SurfaceCommandsMixin:
    """Surface command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # get_schema — backs `aq schema` (design §4.3)
    # ------------------------------------------------------------------

    async def _cmd_get_schema(self, args: dict) -> dict:
        """Return the system's enum catalog so agents never guess magic strings.

        Introspects the enums that exist in the codebase today.  Enums owned
        by subsystems that haven't landed yet (gate lifecycle beyond type/
        status, outcome/failure_class/work_outcome, session states — see
        design §4.3) are intentionally omitted rather than hard-coded here;
        they will appear automatically once those subsystems add their
        constants and this method is extended to read them.
        """
        from src.database.tables import GATE_STATUSES, GATE_TYPES, TASK_DEP_TYPES
        from src.models import TaskStatus, TaskType

        return {
            "schema_version": 1,
            "enums": {
                "task_status": [s.value for s in TaskStatus],
                "task_type": [t.value for t in TaskType],
                "dependency_type": list(TASK_DEP_TYPES),
                "gate_type": list(GATE_TYPES),
                "gate_status": list(GATE_STATUSES),
            },
        }

    # ------------------------------------------------------------------
    # task_show — backs `aq task show|details` (design §3.1)
    # ------------------------------------------------------------------

    async def _cmd_task_show(self, args: dict) -> dict:
        """Full task detail in one round trip: fields + deps + context + labels.

        Composes the existing ``_cmd_get_task`` (fields, dependency
        visualization, subtasks) with ``task_context`` rows and
        ``task_labels``.  Gate/work-state sections called for in the full
        spec table (§3) are added once work-graph's query layer for those
        substrate-only tables (``gates``, ``task_gates``) lands.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        info = await self._cmd_get_task({"task_id": task_id})
        if "error" in info:
            return info

        info["context"] = await self.db.get_task_contexts(task_id)
        info["labels"] = await self.db.get_task_labels(task_id)
        return info

    # ------------------------------------------------------------------
    # task_set — backs `aq task set` (design §3.1)
    # ------------------------------------------------------------------

    async def _cmd_task_set(self, args: dict) -> dict:
        """Work-state contract writes. Never performs status transitions.

        Supported fields: ``branch``, ``pr_url``, ``work_dir``, ``note``,
        ``labels_add``, ``labels_remove``, ``meta``.

        ``work_dir`` is recorded as task metadata (``task_metadata`` key
        ``"work_dir"``) rather than a proper workspace binding — the
        workspaces-v2 / work-graph "work-state" model this field ultimately
        belongs to (design §3.1) hasn't landed a task-facing write path yet.
        This is a placeholder home for the value, not the final shape.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        fields_changed: list[str] = []

        updates: dict = {}
        if "branch" in args:
            updates["branch_name"] = args["branch"]
        if "pr_url" in args:
            updates["pr_url"] = args["pr_url"]
        if updates:
            await self.db.update_task(task_id, **updates)
            fields_changed.extend(updates.keys())

        if args.get("note"):
            await self.db.add_task_context(task_id, type="note", label="note", content=args["note"])
            fields_changed.append("note")

        # Labels are the work-graph's sanctioned free-text tag surface
        # (design §6); ``hold:<who>`` is the reserved convention that
        # withholds a task from the ready frontier.
        for label in args.get("labels_add") or []:
            await self.db.add_task_label(task_id, label)
            await self.db.log_event(
                "label.added", project_id=task.project_id, task_id=task_id, payload=label
            )
            fields_changed.append(f"+label:{label}")

        for label in args.get("labels_remove") or []:
            await self.db.remove_task_label(task_id, label)
            await self.db.log_event(
                "label.removed", project_id=task.project_id, task_id=task_id, payload=label
            )
            fields_changed.append(f"-label:{label}")

        if "work_dir" in args:
            await self.db.set_task_meta(task_id, "work_dir", args["work_dir"])
            fields_changed.append("work_dir")

        meta = args.get("meta") or {}
        for key, value in meta.items():
            await self.db.set_task_meta(task_id, key, value)
            fields_changed.append(f"meta:{key}")

        if not fields_changed:
            return {
                "error": (
                    "No fields to update. Provide branch, pr_url, work_dir, note, "
                    "labels_add, labels_remove, or meta."
                )
            }

        result = await self._cmd_task_show({"task_id": task_id})
        result["fields_changed"] = fields_changed
        return result
