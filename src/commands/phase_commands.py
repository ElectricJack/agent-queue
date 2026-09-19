"""Phases — ordered containers that gate implicitly (graph-visibility A1).

A phase is nothing new: it is an ordinary container task carrying a
``task_metadata`` key ``phase`` (``{"order": int, "label": str}``) plus one
``blocks`` edge onto the previous sibling phase.  Everything that makes the
ordering work already exists — the persisted ``is_blocked`` projection keeps
the later phase DEFINED, and a DEFINED parent withholds every descendant
through the ``parent-child`` rule — so the claim frontier needs no
phase-specific clause and no per-task edge is ever written between the work
in phase *N* and the work in phase *N+1*.

Work joins a phase with the ordinary ``aq task create --parent <phase-id>``;
a phase that has already settled refuses it through the existing
``hierarchy.container_closed``.
"""

from __future__ import annotations

import logging

from src.database.queries.hierarchy_queries import PHASE_KEY
from src.models import DepType, TaskStatus

logger = logging.getLogger(__name__)

__all__ = ["PHASE_KEY", "PhaseCommandsMixin"]


class PhaseCommandsMixin:
    """``phase_create`` / ``phase_list`` — operator and planner surfaces."""

    async def _phase_siblings(self, project_id: str, parent_id: str | None) -> list[tuple]:
        """``(task, phase_meta)`` for every phase directly under *parent_id*.

        Ordered by the recorded ``order``, then by id so a hand-written
        duplicate order is still deterministic.  ``parent_id`` of ``None``
        means the project root.
        """
        siblings = [
            task
            for task in await self.db.list_tasks(project_id=project_id)
            if (task.parent_task_id or None) == parent_id
        ]
        flagged = await self.db.task_ids_with_meta([task.id for task in siblings], PHASE_KEY)
        rows = []
        for task in siblings:
            if task.id not in flagged:
                continue
            meta = await self.db.get_task_meta(task.id, PHASE_KEY)
            rows.append((task, meta if isinstance(meta, dict) else {}))
        rows.sort(key=lambda row: (int(row[1].get("order") or 0), row[0].id))
        return rows

    async def _phase_scope(self, args: dict) -> tuple[str, str | None, dict | None]:
        """Validate ``project_id`` / ``parent_id``.  Returns the refusal last."""
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            return "", None, {
                "success": False,
                "code": "phase.project_required",
                "error": "project_id is required; name the project (aq project list).",
            }
        if await self.db.get_project(project_id) is None:
            return "", None, {
                "success": False,
                "code": "phase.project_not_found",
                "error": f"No project '{project_id}'; list them with 'aq project list'.",
            }
        parent_id = str(args.get("parent_id") or "").strip() or None
        if parent_id is not None:
            parent = await self.db.get_task(parent_id)
            if parent is None or parent.project_id != project_id:
                return "", None, {
                    "success": False,
                    "code": "phase.parent_not_found",
                    "error": (
                        f"No task '{parent_id}' in project '{project_id}'; "
                        "check it with 'aq task show'."
                    ),
                }
        return project_id, parent_id, None

    async def _cmd_phase_create(self, args: dict) -> dict:
        """Create the next phase under a project root or an epic.

        The new phase is born DEFINED and is flagged a container *at
        creation*: an unflagged childless phase is a claimable READY task the
        moment the promotion cascade sees it.
        """
        project_id, parent_id, refusal = await self._phase_scope(args)
        if refusal is not None:
            return refusal
        title = str(args.get("title") or "").strip()
        if not title:
            return {
                "success": False,
                "code": "phase.title_required",
                "error": "title is required; name the phase (aq task phase-create --title ...).",
            }
        label = str(args.get("label") or "").strip() or title

        siblings = await self._phase_siblings(project_id, parent_id)
        order = max((int(meta.get("order") or 0) for _, meta in siblings), default=0) + 1
        previous = siblings[-1][0].id if siblings else None

        create_args: dict = {
            "project_id": project_id,
            "title": title,
            "description": label,
            "task_type": "plan",
            # A phase gates; it is never dispatched.  Starting DEFINED keeps it
            # off the frontier until the cascade releases it as a container.
            "_initial_status": TaskStatus.DEFINED.value,
        }
        if parent_id is not None:
            create_args["parent_id"] = parent_id
        if previous is not None:
            create_args["depends_on"] = [
                {
                    "task_id": previous,
                    "dep_type": DepType.BLOCKS.value,
                    "reason": "phase order",
                }
            ]

        created = await self._cmd_create_task(create_args)
        task_id = created.get("created")
        if created.get("error") or not task_id:
            return {
                "success": False,
                "code": created.get("code") or "phase.create_failed",
                "error": created.get("error") or "the phase task could not be created",
            }

        async with self.db.immediate() as conn:
            await self.db.mark_container(task_id, conn=conn)
        await self.db.set_task_meta(task_id, PHASE_KEY, {"order": order, "label": label})

        return {
            "success": True,
            "phase": {
                "id": task_id,
                "order": order,
                "label": label,
                "parent_id": parent_id,
                "blocked_by": previous,
            },
        }

    async def _cmd_phase_list(self, args: dict) -> dict:
        """Every phase under a project root (or under ``parent_id``), in order."""
        project_id, parent_id, refusal = await self._phase_scope(args)
        if refusal is not None:
            return refusal

        phases = []
        for task, meta in await self._phase_siblings(project_id, parent_id):
            summary = await self.db.get_children_summary(task.id) or {}
            phases.append({
                "id": task.id,
                "title": task.title,
                "label": meta.get("label") or task.title,
                "order": int(meta.get("order") or 0),
                "status": task.status.value,
                "is_blocked": bool(task.is_blocked),
                "total": int(summary.get("total") or 0),
                "done": int(summary.get("done") or 0),
            })
        return {"success": True, "phases": phases}
