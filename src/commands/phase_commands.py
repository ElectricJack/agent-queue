"""Phases — ordered containers that gate implicitly (graph-visibility A1).

A phase is nothing new: it is an ordinary container task carrying a
``task_metadata`` key ``phase`` (``{"order": int, "label": str}``) plus a
``blocks`` edge onto every earlier sibling phase that has not COMPLETED —
not only the immediate predecessor, so deleting an abandoned middle phase
leaves the remaining gates standing.  Everything that makes the
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

from src.database.queries.hierarchy_queries import PHASE_KEY, HierarchyError
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

        Placement is always **explicit**.  A non-elevated session (a planner
        holds a task like any other worker) goes down ``create_task``'s
        worker-filing path, where an *omitted* parent means "a child of the
        task I am holding" (swarm-work-model §12), not "the project root".
        Asking for the root by omission would therefore have filed the phase
        under the planner's own task while the order and the ``blocks`` edge
        were computed against the root — so no ``parent_id`` is sent as
        ``root: True``, and the ordering is then computed against the parent
        the database actually recorded, never against the one requested.
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

        create_args: dict = {
            "project_id": project_id,
            "title": title,
            "description": label,
            "task_type": "plan",
            # A phase gates; it is never dispatched.  Starting DEFINED keeps it
            # off the frontier until the cascade releases it as a container.
            "_initial_status": TaskStatus.DEFINED.value,
            # The worker-filing path refuses a filing with no stated reason.
            "reason": "phase container",
        }
        if parent_id is not None:
            create_args["parent_id"] = parent_id
        else:
            create_args["root"] = True

        created = await self._cmd_create_task(create_args)
        task_id = created.get("created")
        if created.get("error") or not task_id:
            # Hand back the filing path's own refusal — ``idle_session_cannot_file``,
            # ``filing_quota_exceeded``, ``reason_required``, ``hierarchy.*``, a
            # parent-scope error — rather than flattening every one of them into
            # a code that tells the caller nothing about what to do next.  Not
            # every refusal upstream carries a ``code``; one is invented here
            # only when there is genuinely nothing to pass on.
            refusal = dict(created)
            refusal["success"] = False
            if not refusal.get("code") and not refusal.get("error"):
                refusal["code"] = "phase.create_failed"
                refusal["error"] = "the phase task could not be created"
            return refusal

        # Order and the gate edge follow the *written* placement.  The filing
        # path may legitimately land the task somewhere other than requested
        # (a naming-depth cap, a repair writer's re-selected parent), and a
        # phase ordered against a parent it does not live under would gate
        # nothing.
        created_task = await self.db.get_task(task_id)
        actual_parent = created_task.parent_task_id if created_task is not None else parent_id
        siblings = await self._phase_siblings(project_id, actual_parent)
        order = max((int(meta.get("order") or 0) for _, meta in siblings), default=0) + 1
        previous = siblings[-1][0].id if siblings else None
        # A gate onto EVERY earlier phase that has not completed, not only the
        # immediate predecessor.  With one edge per phase, deleting an
        # abandoned middle phase dropped the only edge its successor had and
        # released it while an earlier phase was still open (final review I4).
        # Redundant-looking edges are the point: they are what survives a
        # deletion.  A COMPLETED phase gates nothing, so it is left out — the
        # projection would resolve such an edge immediately anyway.
        gates = [
            task.id for task, _ in siblings if task.status is not TaskStatus.COMPLETED
        ]

        # One transaction: a crash between the flag and the metadata would
        # leave a claimable unflagged phase behind.
        async with self.db.immediate() as conn:
            await self.db.mark_container(task_id, conn=conn)
            await self.db._upsert_meta(
                task_id, PHASE_KEY, {"order": order, "label": label}, conn=conn
            )

        for gate in gates:
            try:
                await self.db.add_dependency(
                    task_id, gate, DepType.BLOCKS.value, description="phase order"
                )
            except HierarchyError as exc:
                return {
                    "success": False,
                    "code": f"hierarchy.{exc.code}",
                    "error": (
                        f"hierarchy.{exc.code}: {exc.detail} (phase '{task_id}' was created "
                        f"but is not gated behind '{gate}'; add the edge with 'aq task deps')"
                    ),
                }

        return {
            "success": True,
            "phase": {
                "id": task_id,
                "order": order,
                "label": label,
                "parent_id": actual_parent,
                # The immediate predecessor, unchanged: what a caller shows as
                # "this comes after". ``blocked_by_all`` is the full gate set.
                "blocked_by": previous,
                "blocked_by_all": gates,
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
