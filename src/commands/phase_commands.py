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

import json
import logging
from dataclasses import dataclass

from sqlalchemy import and_, select

from src.database.queries.hierarchy_queries import PHASE_KEY, HierarchyError
from src.database.tables import task_metadata, tasks
from src.models import DepType, TaskStatus

logger = logging.getLogger(__name__)

__all__ = ["PHASE_KEY", "PhaseCommandsMixin"]


@dataclass(frozen=True)
class _PhaseSibling:
    """The narrow task/metadata projection phase commands actually need."""

    id: str
    title: str
    status: str
    is_blocked: bool
    meta: dict


class PhaseCommandsMixin:
    """``phase_create`` / ``phase_list`` — operator and planner surfaces."""

    async def _phase_siblings(
        self, project_id: str, parent_id: str | None, *, conn=None
    ) -> list[_PhaseSibling]:
        """Return the narrow projection for phases directly under *parent_id*.

        Ordered by the recorded ``order``, then by id so a hand-written
        duplicate order is still deterministic.  ``parent_id`` of ``None``
        means the project root.  This is one scope-filtered join rather than
        hydrating every project task and issuing one metadata query per phase.
        """
        stmt = (
            select(
                tasks.c.id,
                tasks.c.title,
                tasks.c.status,
                tasks.c.is_blocked,
                task_metadata.c.value,
            )
            .select_from(
                tasks.join(
                    task_metadata,
                    and_(
                        task_metadata.c.task_id == tasks.c.id,
                        task_metadata.c.key == PHASE_KEY,
                    ),
                )
            )
            .where(tasks.c.project_id == project_id)
        )
        stmt = stmt.where(
            tasks.c.parent_task_id == parent_id
            if parent_id is not None
            else tasks.c.parent_task_id.is_(None)
        )
        if conn is None:
            async with self.db._engine.connect() as read_conn:
                raw_rows = (await read_conn.execute(stmt)).mappings().all()
        else:
            raw_rows = (await conn.execute(stmt)).mappings().all()
        rows = []
        for row in raw_rows:
            try:
                meta = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                meta = {}
            rows.append(
                _PhaseSibling(
                    id=row["id"],
                    title=row["title"],
                    status=row["status"],
                    is_blocked=bool(row["is_blocked"]),
                    meta=meta if isinstance(meta, dict) else {},
                )
            )
        rows.sort(key=lambda row: (int(row.meta.get("order") or 0), row.id))
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
        # Before anything is filed: in hierarchy/train a phase container owns
        # its children's delivery branch (see ``phase_mode_refusal``).  The
        # graph door refuses the same thing with the same code.  ``phase_list``
        # deliberately does not consult this — reading the phases a project
        # already has is never unsafe.
        refusal = await self.db.phase_mode_refusal(project_id)
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

        phase_state: dict[str, object] = {}

        async def initialise_phase_on(conn, task_id: str, actual_parent: str | None) -> None:
            """Write the phase flag, order metadata and all gates atomically.

            ``_create_task`` invokes this inside the same transaction that
            inserts the task and its parent edge.  The hierarchy lock is
            already held by each creation path before this point, so sibling
            order allocation cannot race another create/reparent/delete.
            """
            siblings = await self._phase_siblings(project_id, actual_parent, conn=conn)
            order = max((int(row.meta.get("order") or 0) for row in siblings), default=0) + 1
            previous = siblings[-1].id if siblings else None
            gates = [row.id for row in siblings if row.status != TaskStatus.COMPLETED.value]
            await self.db.mark_container(task_id, conn=conn)
            await self.db._upsert_meta(
                task_id, PHASE_KEY, {"order": order, "label": label}, conn=conn
            )
            for gate in gates:
                await self.db.add_dependency(
                    task_id,
                    gate,
                    DepType.BLOCKS.value,
                    description="phase order",
                    conn=conn,
                )
            phase_state.update(
                order=order,
                previous=previous,
                gates=gates,
                parent_id=actual_parent,
            )

        create_args["_after_create_on"] = initialise_phase_on
        try:
            created = await self._cmd_create_task(create_args)
        except HierarchyError as exc:
            return {
                "success": False,
                "code": f"hierarchy.{exc.code}",
                "error": f"hierarchy.{exc.code}: {exc.detail}; phase creation was rolled back",
            }
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
            if str(refusal.get("code") or "").startswith("hierarchy."):
                refusal["error"] = (
                    f"{refusal['error']}; phase creation was rolled back"
                )
            return refusal

        return {
            "success": True,
            "phase": {
                "id": task_id,
                "order": phase_state["order"],
                "label": label,
                "parent_id": phase_state["parent_id"],
                # The predecessor is history, so preserve the old spelling for
                # compatibility and publish the unambiguous preferred spelling.
                "previous_phase_id": phase_state["previous"],
                "blocked_by": phase_state["previous"],
                "blocked_by_all": phase_state["gates"],
            },
        }

    async def _cmd_phase_list(self, args: dict) -> dict:
        """Every phase under a project root (or under ``parent_id``), in order."""
        project_id, parent_id, refusal = await self._phase_scope(args)
        if refusal is not None:
            return refusal

        phases = []
        for task in await self._phase_siblings(project_id, parent_id):
            summary = await self.db.get_children_summary(task.id) or {}
            phases.append({
                "id": task.id,
                "title": task.title,
                "label": task.meta.get("label") or task.title,
                "order": int(task.meta.get("order") or 0),
                "status": task.status,
                "is_blocked": task.is_blocked,
                "total": int(summary.get("total") or 0),
                "done": int(summary.get("done") or 0),
            })
        return {"success": True, "phases": phases}
