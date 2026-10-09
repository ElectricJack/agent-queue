"""Task-proposal commands — staged spec ingestion (design §8, Phase 6).

Registers four CommandHandler commands:

- ``task_batch_propose`` — validate + persist a proposed batch and emit
  ``proposal.ready``.  A batch carrying a live spec-ingest assignment's
  approved-document authority is applied live in the same transaction, so it
  needs no proposal gate and no separate commit call.
- ``task_batch_update`` — replace the payload while status is draft/ready
  and no approval gate awaits the proposal yet.
- ``task_batch_discard`` — soft-drop the proposal.
- ``task_batch_commit`` — atomically materialize the batch into the live
  work graph under a human gate that approves this exact proposal, or
  approved-document authority stamped by a live spec-ingest assignment.
  A replay of an already committed proposal returns the original receipt.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ruamel.yaml import YAML
from sqlalchemy import insert, select, update

from src.api.auth import RequestScope
from src.api.scope import spec_ingest_task_for_session
from src.commands import task_changes
from src.commands.principal import PrincipalKind, current_principal
from src.database.queries import proposal_queries
from src.database.tables import events, task_metadata, task_proposals, tasks
from src.models import Task, TaskStatus
from src.reviews.vault import spec_kind_from_content, split_frontmatter
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

#: Server-owned payload stamp carrying a live spec-ingest assignment's
#: approved-document authority.  ``normalize`` drops it from caller payloads.
SPEC_INGEST_META = "spec_ingest"


class TaskProposalCommandsMixin:
    """Mount on CommandHandler alongside the other command mixins."""

    # ----- helpers -------------------------------------------------------

    async def _spec_ingest_context(self, project_id: str, source: str) -> dict | None:
        principal = current_principal()
        if (principal is None or principal.kind is not PrincipalKind.SESSION
                or principal.profile_id != "spec-ingest"):
            return None
        task = await spec_ingest_task_for_session(self.db, RequestScope(
            kind="session", session_id=principal.session_id,
            session_instance_token=principal.session_instance_token,
            project_id=principal.project_id, task_id=principal.task_id,
        ))
        if task is None or task.project_id != project_id:
            raise ValueError("spec ingestion requires a live role assignment in this project")
        path = Path(task.dedup_key.removeprefix("spec-ingest:")).resolve()
        root = (Path(self.config.vault_root) / "projects" / project_id).resolve()
        if not any(path.is_relative_to(root / directory) for directory in ("specs", "plans")):
            raise ValueError("spec ingestion path is outside the project's specs/plans")
        if source != f"spec:{path}":
            raise ValueError("proposal source must match the held ingestion task's spec path")
        raw = path.read_text(encoding="utf-8")
        spec_kind = spec_kind_from_content(raw)
        frontmatter, _ = split_frontmatter(raw)
        if not frontmatter or (YAML(typ="safe").load(frontmatter) or {}).get("status") != "approved":
            raise ValueError("spec ingestion requires an approved document")
        return {"task_id": task.id, "spec_path": str(path), "spec_kind": spec_kind}

    @staticmethod
    def _design_spec_batch(path: str) -> tuple[list[dict], list[dict]]:
        """Design approval schedules its implementation document, never implementation work."""
        return ([
            {"tempId": "implementation_spec", "title": "Implementation specification",
             "description": f"Implementation planning for approved design {path}.",
             "task_type": "design", "intelligence_class": "deep-high"},
            {"tempId": "write_spec", "title": f"Write implementation spec for {Path(path).name}",
             "task_type": "design", "intelligence_class": "deep-high",
             "description": (
                 f"Read approved design spec {path} and inspect the current repository. "
                 "Write an implementation spec grounded in files, functions, tests and rollout. "
                 "Choose defaults for open questions. Include spec_kind: implementation in "
                 "frontmatter. File no implementation work. Submit the document to Jack with "
                 "aq review submit --task-id <held-task> --file <draft> --kind spec --title "
                 "<title>; do not commit the review draft. Its approval triggers ingestion."
             ),
             "deliverables": [{"id": "implementation_review", "kind": "review", "target": "spec"}]},
        ], [{"from": "write_spec", "to": "implementation_spec", "dep_type": "parent-child"}])

    async def _validate_ingest_graph(self, conn, payload: dict) -> None:
        """Refuse an ingestion change set that is not epics, children and leaf edges.

        Spec-ingest authority commits without a human gate, so it creates new
        work only: no edits, edge removals or comments on existing tasks.
        """
        if payload["edits"] or payload["remove_edges"] or payload["comments"]:
            raise task_changes.ChangeSetError("spec-ingest batches only create tasks and edges")
        specs = {spec["tempId"] for spec in payload["tasks"]}
        parents: dict[str, str] = {}
        structural = [
            (spec["tempId"], spec["parent_id"])
            for spec in payload["tasks"]
            if spec.get("parent_id")
        ] + [
            (edge["from"], edge["to"])
            for edge in payload["edges"]
            if edge.get("dep_type", "blocks") == "parent-child"
        ]
        for child, parent in structural:
            if child not in specs or parent not in specs:
                raise task_changes.ChangeSetError(
                    "spec-ingest parents and children must be in the same batch"
                )
            if child in parents:
                raise task_changes.ChangeSetError("spec-ingest child has multiple parent edges")
            parents[child] = parent
        containers = set(parents.values())
        if not containers or (specs - parents.keys()) - containers:
            raise task_changes.ChangeSetError("spec-ingest root tasks must be epics with children")
        for edge in payload["edges"]:
            if edge.get("dep_type", "blocks") == "parent-child":
                continue
            for endpoint in (edge["from"], edge["to"]):
                if endpoint in containers:
                    raise task_changes.ChangeSetError(
                        "spec-ingest dependency edges must connect children, never containers"
                    )
                if endpoint not in specs and await self.db.is_container(endpoint, conn=conn):
                    raise task_changes.ChangeSetError(
                        "spec-ingest dependency edges must not connect existing containers"
                    )

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
            ingest = await self._spec_ingest_context(project_id, source)
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}
        change_set = args
        if ingest and ingest["spec_kind"] == "design":
            tasks_in, edges_in = self._design_spec_batch(ingest["spec_path"])
            change_set = {"tasks": tasks_in, "edges": edges_in}
        committed = notifications = None
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                if ingest:
                    await self._validate_ingest_graph(conn, task_changes.normalize(change_set))
                payload = await self._prepare_change_set(conn, project_id, change_set, source)
                if args.get("dry_run"):
                    return {"success": True, "dry_run": True, "diff": payload["diff"]}
                if ingest:
                    payload[SPEC_INGEST_META] = ingest
                proposal_id = await proposal_queries.insert_proposal(
                    self.db,
                    project_id=project_id,
                    source=source,
                    payload=payload,
                    conn=conn,
                    status="ready",
                )
                if ingest:
                    # An approved document is its own authority: the validated
                    # graph goes live in the proposing transaction. A staged
                    # proposal awaiting a commit call is a graph nobody
                    # publishes, which is how an approved spec used to end up
                    # waiting for an operator to commit it by hand.
                    committed, notifications = await self._materialize_proposal(
                        conn, proposal_id, gate_id=None, project_id=project_id
                    )
                    if notifications is None:
                        raise task_changes.ChangeSetError(
                            committed.get("error") or "ingestion batch was not applied"
                        )
        except Exception as exc:
            return self._change_set_failure(exc)
        if committed is not None:
            await self._publish_committed(
                proposal_id, project_id, committed["task_ids"], notifications
            )
            return {
                "success": True,
                "proposal_id": proposal_id,
                "committed": True,
                "task_ids": committed["task_ids"],
                "diff": payload["diff"],
            }
        await self._emit_proposal_event(
            "proposal.ready", {"project_id": project_id, "proposal_id": proposal_id}
        )
        return {"success": True, "proposal_id": proposal_id, "diff": payload["diff"]}

    async def _cmd_task_batch_update(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {"success": False, "error": f"proposal '{proposal_id}' not found"}
        if row["payload"].get(SPEC_INGEST_META):
            return {
                "success": False,
                "error": "propose a new spec-ingest batch instead of updating it",
            }
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
        """Why this proposal lacks approval authority, or ``None``.

        Materialisation is the authority boundary.  An event filter narrows
        which resolutions reach the pipeline's commit rule, but any caller can
        invoke ``task_batch_commit`` directly, so the approving gate is re-read
        here: a ``human`` gate in the proposal's own project, awaiting this
        proposal id, resolved with one of :data:`APPROVAL_RESOLUTIONS`.  With
        no ``gate_id`` the newest such gate is the decision of record.  The
        payload is frozen once that gate exists (``task_batch_update``), so the
        gate approves exactly the revision being committed.  Spec-ingest
        authority is stamped only by ``_spec_ingest_context``, never parsed
        from caller payloads; those immutable batches need no second decision.
        """
        proposal_id, owner = row["id"], row["project_id"]
        if project_id and project_id != owner:
            return f"proposal '{proposal_id}' belongs to project '{owner}', not '{project_id}'"
        if row["payload"].get(SPEC_INGEST_META):
            return None
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
                result, notifications = await self._materialize_proposal(
                    conn,
                    proposal_id,
                    gate_id=args.get("gate_id"),
                    project_id=args.get("project_id"),
                )
                if notifications is None:
                    return result
        except Exception as exc:
            logger.info("task change set %s refused: %s", proposal_id, exc)
            return self._change_set_failure(exc, "task_batch_commit")
        await self._publish_committed(proposal_id, project_id, result["task_ids"], notifications)
        return result

    async def _materialize_proposal(
        self, conn, proposal_id: str, *, gate_id: str | None, project_id: str | None
    ) -> tuple[dict, dict | None]:
        """Write a ready proposal's graph, receipt and audit into ``conn``.

        ``conn`` is the caller's commit boundary, so the graph, the proposal
        status, the routing gates and the audit become durable together.  The
        second element is the notifications to publish once that transaction
        commits, and is ``None`` when nothing was materialised: a refusal, or a
        replay of an already committed proposal.
        """
        # The authority check reads the stamp from the payload, so decode first.
        current = dict(
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
        current["payload"] = stored = json.loads(current["payload"])
        if current["status"] == "discarded":
            raise task_changes.ChangeSetError("proposal was discarded")
        approval_error = await self._proposal_approval_error(
            current, gate_id=gate_id, project_id=project_id
        )
        if approval_error:
            return {"success": False, "not_approved": True, "error": approval_error}, None
        if current["status"] == "committed":
            return {
                "success": True,
                "already_committed": True,
                "task_ids": stored["receipt"]["task_ids"]
                if "receipt" in stored
                else await self._proposal_task_ids(proposal_id),
            }, None
        if current["status"] != "ready":
            raise task_changes.ChangeSetError("proposal not in 'ready' state")
        payload = task_changes.normalize(stored)
        ingest = stored.get(SPEC_INGEST_META)
        if ingest:
            await self._validate_ingest_graph(conn, payload)
        expected, rows, project = await task_changes.snapshot(
            conn,
            current["project_id"],
            payload,
            previous=stored.get("expected"),
        )
        if "expected" in stored and expected != stored["expected"]:
            raise task_changes.ChangeSetError(
                "change set conflicts with task versions or graph state; propose a fresh revision",
                "change_set.conflict",
            )
        receipt, notifications = await task_changes.apply(
            self, conn, current["project_id"], payload, expected, rows, project, current["source"]
        )
        for task_id in receipt["task_ids"]:
            await self.db._upsert_meta(task_id, PROPOSAL_ID_META, proposal_id, conn=conn)
        stored["receipt"] = receipt
        if ingest:
            stored[SPEC_INGEST_META] = {**ingest, "task_ids": receipt["task_ids"]}
        await conn.execute(
            update(task_proposals)
            .where(task_proposals.c.id == proposal_id)
            .values(status="committed", payload=json.dumps(stored), updated_at=time.time())
        )
        await conn.execute(
            insert(events).values(
                event_type="task.change_set_committed",
                project_id=current["project_id"],
                payload=json.dumps(
                    {
                        "proposal_id": proposal_id,
                        "source": current["source"],
                        "diff": stored.get("diff"),
                        "receipt": receipt,
                        "actor": (self._current_scope or {}).get("session_id") or "operator",
                    }
                ),
                timestamp=time.time(),
            )
        )
        return {"success": True, "task_ids": receipt["task_ids"]}, notifications

    async def _publish_committed(
        self, proposal_id: str, project_id: str, task_ids: list[str], notifications: dict
    ) -> None:
        """Announce a committed change set. Runs only after its transaction commits."""
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
                    if tid in task_ids:
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
