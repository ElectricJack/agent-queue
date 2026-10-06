"""Task-proposal commands — staged spec ingestion (design §8, Phase 6).

Registers four CommandHandler commands:

- ``task_batch_propose`` — validate + persist a proposed batch, emit
  ``proposal.ready``.
- ``task_batch_update`` — replace the payload while status is draft/ready
  and no approval gate awaits the proposal yet.
- ``task_batch_discard`` — soft-drop the proposal.
- ``task_batch_commit`` — atomically materialize the batch into the live
  work graph, only under a human gate that approves this exact proposal.
  A replay of an already committed proposal returns the original receipt.
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import select, update, insert

from src.database.queries import proposal_queries
from src.database.tables import events, task_metadata, task_proposals, tasks
from src.models import Task, TaskStatus
from src.commands import task_changes
from src.routing.filing import choice_forbidden

logger = logging.getLogger(__name__)

#: The ``resolution`` values that approve a proposal's human gate.  The
#: dashboard's proposal pane and activity drawer write ``approved``; the task
#: pane writes ``approve``.  The shipped ``default-pipeline`` filters its
#: commit rule on exactly this set, so the event filter and the command agree.
APPROVAL_RESOLUTIONS = frozenset({"approve", "approved"})

#: Task metadata key linking each materialised task to its proposal, so a
#: replayed commit can report the original receipt.
PROPOSAL_ID_META = "proposal_id"


class TaskProposalCommandsMixin:
    """Mount on CommandHandler alongside the other command mixins."""

    # ----- helpers -------------------------------------------------------

    async def _emit_proposal_event(self, event_type: str, payload: dict) -> None:
        bus = getattr(self.orchestrator, "bus", None)
        if bus is None:
            return
        try:
            await bus.emit(event_type, payload)
        except Exception:  # pragma: no cover — defensive
            logger.debug("bus.emit %s failed", event_type, exc_info=True)

    # ----- commands ------------------------------------------------------

    async def _prepare_change_set(self, conn, project_id, args, source):
        payload = task_changes.normalize(args)
        expected, rows, project = await task_changes.snapshot(conn, project_id, payload)
        # Exercise exactly the commit path, including integration and state
        # guards, but never publish speculative tasks, audit or notifications.
        savepoint = await conn.begin_nested()
        try:
            receipt, _ = await task_changes.apply(
                self, conn, project_id, payload, expected, rows, project, source
            )
            after = (
                (await conn.execute(select(tasks).where(tasks.c.project_id == project_id)))
                .mappings()
                .all()
            )
            by_id = {row["id"]: dict(row) for row in after}
            aliases = {real: temp for temp, real in receipt["temp_ids"].items()}
            fields = (
                "title",
                "description",
                "priority",
                "status",
                "class_hint",
                "task_type",
                "parent_task_id",
                "route_source",
                "is_blocked",
            )

            def brief(row):
                return (
                    {
                        key: aliases.get(row[key], row[key])
                        if key == "parent_task_id"
                        else row[key]
                        for key in fields
                    }
                    if row
                    else None
                )

            changes = []
            for tid in sorted(rows.keys() | by_id.keys()):
                before, after_row = brief(rows.get(tid)), brief(by_id.get(tid))
                if before != after_row:
                    changes.append(
                        {"task_id": aliases.get(tid, tid), "before": before, "after": after_row}
                    )
            payload["diff"] = {
                "tasks": changes,
                "edges": payload["edges"],
                "remove_edges": payload["remove_edges"],
                "comments": payload["comments"],
            }
        finally:
            await savepoint.rollback()
        payload["expected"] = expected
        return payload

    @staticmethod
    def _change_set_failure(exc, command="task_batch_propose"):
        if getattr(exc, "refused", None):
            return choice_forbidden(command, exc.refused)
        return {
            "success": False,
            "code": getattr(exc, "code", "change_set.invalid"),
            "error": str(exc),
        }

    async def _cmd_task_batch_propose(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        source = args.get("source")
        if not project_id or not source:
            return {"success": False, "error": "project_id and source are required"}
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                payload = await self._prepare_change_set(conn, project_id, args, source)
                if args.get("dry_run"):
                    return {"success": True, "dry_run": True, "diff": payload["diff"]}
                proposal_id = await proposal_queries.insert_proposal(
                    self.db,
                    project_id=project_id,
                    source=source,
                    payload=payload,
                    conn=conn,
                    status="ready",
                )
        except Exception as exc:
            return self._change_set_failure(exc)
        await self._emit_proposal_event(
            "proposal.ready", {"project_id": project_id, "proposal_id": proposal_id}
        )
        return {"success": True, "proposal_id": proposal_id, "diff": payload["diff"]}

    async def _cmd_task_batch_update(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {"success": False, "error": f"proposal '{proposal_id}' not found"}
        try:
            async with task_changes.boundary(self.db, row["project_id"]) as conn:
                current = (
                    (
                        await conn.execute(
                            select(task_proposals)
                            .where(task_proposals.c.id == proposal_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if current["status"] not in ("draft", "ready"):
                    raise task_changes.ChangeSetError(
                        f"cannot update proposal in status '{current['status']}'"
                    )
                payload = await self._prepare_change_set(
                    conn, row["project_id"], args.get("payload") or {}, row["source"]
                )
                if not await proposal_queries.update_ungated_payload(
                    self.db, proposal_id, project_id=row["project_id"], payload=payload, conn=conn
                ):
                    raise task_changes.ChangeSetError(
                        f"proposal '{proposal_id}' already has an approval gate; its payload is frozen; "
                        "discard it and propose the revised change set"
                    )
        except Exception as exc:
            return self._change_set_failure(exc, "task_batch_update")
        return {"success": True, "diff": payload["diff"]}

    async def _cmd_task_batch_discard(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        if not proposal_id:
            return {"success": False, "error": "proposal_id is required"}
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {
                "success": False,
                "error": f"proposal '{proposal_id}' not found",
            }
        async with self.db.immediate() as conn:
            result = await conn.execute(
                update(task_proposals)
                .where(
                    task_proposals.c.id == proposal_id,
                    task_proposals.c.status.in_(("draft", "ready")),
                )
                .values(status="discarded", updated_at=time.time())
            )
            if not result.rowcount:
                return {"success": False, "error": "proposal already committed or discarded"}
        await self._emit_proposal_event(
            "proposal.status_changed",
            {
                "project_id": row["project_id"],
                "proposal_id": proposal_id,
                "status": "discarded",
            },
        )
        return {"success": True}

    async def _proposal_approval_error(
        self, row: dict, *, gate_id: str | None, project_id: str | None
    ) -> str | None:
        """Why no human decision approves this exact proposal, or ``None``.

        Materialisation is the authority boundary.  An event filter narrows
        which resolutions reach the pipeline's commit rule, but any caller can
        invoke ``task_batch_commit`` directly, so the approving gate is re-read
        here: a ``human`` gate in the proposal's own project, awaiting this
        proposal id, resolved with one of :data:`APPROVAL_RESOLUTIONS`.  With
        no ``gate_id`` the newest such gate is the decision of record.  The
        payload is frozen once that gate exists (``task_batch_update``), so the
        gate approves exactly the revision being committed.
        """
        proposal_id, owner = row["id"], row["project_id"]
        if project_id and project_id != owner:
            return f"proposal '{proposal_id}' belongs to project '{owner}', not '{project_id}'"
        if gate_id:
            gate = await self.db.get_gate(str(gate_id))
            if gate is None:
                return f"approval gate '{gate_id}' not found"
        else:
            candidates = await self.db.list_gates(
                project_id=owner, gate_type="human", await_id=proposal_id
            )
            if not candidates:
                return f"proposal '{proposal_id}' has no human approval gate"
            gate = candidates[0]
        label = f"gate '{gate['id']}'"
        if gate["gate_type"] != "human":
            return f"{label} is a '{gate['gate_type']}' gate, not a human approval"
        if gate["project_id"] != owner:
            return f"{label} belongs to project '{gate['project_id']}', not '{owner}'"
        if gate["await_id"] != proposal_id:
            return f"{label} awaits '{gate['await_id']}', not proposal '{proposal_id}'"
        if gate["status"] != "resolved":
            return f"{label} is {gate['status']}, not resolved"
        if gate["resolution"] not in APPROVAL_RESOLUTIONS:
            return f"{label} was resolved {gate['resolution']!r}, which is not an approval"
        return None

    async def _proposal_task_ids(self, proposal_id: str) -> list[str]:
        """The tasks a committed proposal materialised, oldest first."""
        async with self.db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(tasks.c.id)
                    .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                    .where(
                        task_metadata.c.key == PROPOSAL_ID_META,
                        task_metadata.c.value == json.dumps(proposal_id),
                    )
                    .order_by(tasks.c.created_at, tasks.c.id)
                )
            ).all()
        return [r[0] for r in rows]

    async def _cmd_task_batch_commit(self, args: dict) -> dict:
        """Commit the graph, proposal receipt and audit in one SQL transaction."""
        proposal_id = args.get("proposal_id")
        if not proposal_id:
            return {"success": False, "error": "proposal_id is required"}
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {"success": False, "error": f"proposal '{proposal_id}' not found"}
        project_id = row["project_id"]
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                current = (
                    (
                        await conn.execute(
                            select(task_proposals)
                            .where(task_proposals.c.id == proposal_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if current["status"] == "discarded":
                    raise task_changes.ChangeSetError("proposal was discarded")
                approval_error = await self._proposal_approval_error(
                    row, gate_id=args.get("gate_id"), project_id=args.get("project_id")
                )
                if approval_error:
                    return {"success": False, "not_approved": True, "error": approval_error}
                stored = json.loads(current["payload"])
                if current["status"] == "committed":
                    return {
                        "success": True,
                        "already_committed": True,
                        "task_ids": stored["receipt"]["task_ids"]
                        if "receipt" in stored
                        else await self._proposal_task_ids(proposal_id),
                    }
                if current["status"] != "ready":
                    raise task_changes.ChangeSetError("proposal not in 'ready' state")
                payload = task_changes.normalize(stored)
                expected, rows, project = await task_changes.snapshot(
                    conn,
                    project_id,
                    payload,
                    previous=stored.get("expected"),
                )
                if "expected" in stored and expected != stored["expected"]:
                    raise task_changes.ChangeSetError(
                        "change set conflicts with task versions or graph state; propose a fresh revision",
                        "change_set.conflict",
                    )
                receipt, notifications = await task_changes.apply(
                    self, conn, project_id, payload, expected, rows, project, current["source"]
                )
                for task_id in receipt["task_ids"]:
                    await self.db._upsert_meta(task_id, PROPOSAL_ID_META, proposal_id, conn=conn)
                stored["receipt"] = receipt
                await conn.execute(
                    update(task_proposals)
                    .where(task_proposals.c.id == proposal_id)
                    .values(status="committed", payload=json.dumps(stored), updated_at=time.time())
                )
                await conn.execute(
                    insert(events).values(
                        event_type="task.change_set_committed",
                        project_id=project_id,
                        payload=json.dumps(
                            {
                                "proposal_id": proposal_id,
                                "source": current["source"],
                                "diff": stored.get("diff"),
                                "receipt": receipt,
                                "actor": (self._current_scope or {}).get("session_id")
                                or "operator",
                            }
                        ),
                        timestamp=time.time(),
                    )
                )
        except Exception as exc:
            logger.info("task change set %s refused: %s", proposal_id, exc)
            return self._change_set_failure(exc, "task_batch_commit")
        # No task/routing event is emitted until every change and its audit
        # are durable. A failing listener cannot turn this commit into failure.
        await self._emit_proposal_event(
            "proposal.status_changed",
            {"project_id": project_id, "proposal_id": proposal_id, "status": "committed"},
        )
        try:
            await self.db.log_blocked_flips(notifications["flipped"])
            await self.db._notify_ready([(tid, "unblocked") for tid in notifications["ready"]])
            await self.db._notify_settled(notifications["settled"].settled)
            for tid in notifications["routing"]:
                await self._emit_admitted_routing_gates(tid)
            for tid in notifications["updated"]:
                task = await self.db.get_task(tid)
                if task:
                    if tid in receipt["task_ids"]:
                        await self.orchestrator._emit_task_event(
                            "task.created",
                            task,
                            parent_task_id=task.parent_task_id,
                            profile_id=task.profile_id,
                            created_by_kind=None,
                            created_by_id=None,
                        )
                    else:
                        await self._emit_task_graph_change("task.updated", task)
            for tid, (old_parent, new_parent) in notifications["reparented"].items():
                await self.orchestrator._emit_task_event(
                    "task.reparented",
                    await self.db.get_task(tid),
                    old_parent=old_parent or "",
                    new_parent=new_parent or "",
                )
            for task in notifications["archived"]:
                await self._emit_task_graph_change("task.archived", task)
            for comment in notifications["comments"]:
                task = await self.db.get_task(comment["task_id"])
                if task:
                    await self._notify_task_comment(task, comment)
        except Exception:
            logger.exception("post-commit notifications failed for %s", proposal_id)
        return {"success": True, "task_ids": receipt["task_ids"]}

    @staticmethod
    def _proposal_task(project_id: str, spec: dict) -> Task:
        return Task(
            id="",
            project_id=project_id,
            title=spec["title"],
            description=spec.get("description", ""),
            priority=spec.get("priority", 100),
            deliverables=spec.get("deliverables", []),
            # The class is the filer's hint; the router writes the route.
            class_hint=spec.get("intelligence_class"),
            status=TaskStatus.DEFINED,
        )
