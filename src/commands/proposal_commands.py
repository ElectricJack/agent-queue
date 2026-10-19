"""Task-proposal commands — staged spec ingestion (design §8, Phase 6).

Ordinary proposals stage a human-approved change set. A live spec-ingest
assignment materializes implementation work atomically under a durable batch
hold; fidelity review and authorized release expose it to execution. Design
approval creates a released server-authored implementation-spec task. Commit
replays released batches and refuses held ones; discard cannot retire a hold.
A project supervisor's in-scope repair of a released batch (spec P4 L4.4)
commits without the human gate; every other change keeps it.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from sqlalchemy import exists, insert, or_, select, update

from src.api.auth import RequestScope
from src.api.scope import spec_ingest_task_for_session
from src.asks.ingestion import approved_packet, stamp_for, task_stamp
from src.asks.operations import get_operation, operation, succeed
from src.commands import task_changes
from src.commands.principal import PrincipalKind, current_principal
from src.commands.supervisor_authority import operator_or_supervisor
from src.database.queries import ingest_batch_queries as batches
from src.database.queries import proposal_queries
from src.database.tables import (
    approved_document_snapshots,
    events,
    gates,
    ingest_batch_members,
    ingest_batches,
    task_dependencies,
    task_metadata,
    task_proposals,
    tasks,
)
from src.integration.outbox import enqueue_integration_event
from src.models import Task, TaskStatus
from src.routing.filing import choice_forbidden
from src.task_graph import batch_checks

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

#: Proposal source naming the released batch a supervisor repair targets.
BATCH_REPAIR_PREFIX = "batch-repair:"

#: Server-owned payload stamp: the project supervisor proposed an in-scope
#: repair of a released batch.  ``normalize`` drops it from caller payloads.
BATCH_REPAIR_META = "batch_repair"

#: Edit shapes of an in-scope repair: an L4.1 move and an L4.3 retirement.
#: An L4.2 split remainder is a created task under or delivering to a member.
_REPAIR_MOVE_FIELDS = frozenset({"task_id", "parent_id", "reason"})
_REPAIR_RETIRE_FIELDS = frozenset(
    {"task_id", "action", "reason", *task_changes.RETIREMENT_FIELDS}
)


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
        async with self.db.immediate() as conn:
            stamp = await task_stamp(conn, task.id)
            packet = await approved_packet(self.db, stamp["decision_id"], stamp["packet_sha256"],
                                           project_id, conn=conn)
        if source != f"spec:decision:{packet['decision_id']}":
            raise ValueError("proposal source must match the held ingestion task's decision")
        return {"task_id": task.id, **stamp_for(packet)}

    async def _lock_ingest(self, conn, ingest, *, require_current=True):
        return await approved_packet(self.db, ingest["decision_id"], ingest["packet_sha256"],
                                     ingest["project_id"], conn=conn,
                                     require_current=require_current)

    async def _ingest_change_set(self, conn, change_set, ingest):
        packet = await self._lock_ingest(conn, ingest)
        payload = task_changes.normalize(change_set)
        if packet["spec_kind"] == "design":
            tasks_in, edges_in = self._design_spec_batch(packet["spec_path"])
            tasks_in[1]["description"] += (
                f"\nApproved decision {packet['decision_id']} ({packet['packet_sha256']})."
                f"\nImmutable approved design:\n{packet['body']}"
            )
            payload = task_changes.normalize({"tasks": tasks_in, "edges": edges_in})
        parents = {s["parent_id"] for s in payload["tasks"] if s.get("parent_id")}
        parents.update(e["to"] for e in payload["edges"] if e.get("dep_type", "blocks") == "parent-child")
        leaves = {s["tempId"] for s in payload["tasks"]} - parents
        for obligation in packet["feedback"]:
            if not obligation["required_before_release"] or obligation["disposition"] != "follow_up":
                continue
            task_id = obligation["task_id"]
            target = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if (not target or target["project_id"] != packet["project_id"]
                    or target["status"] == "FAILED" or await self.db.is_container(task_id, conn=conn)):
                raise task_changes.ChangeSetError("Required follow-up is missing or unrepresentable")
            choice = obligation["accepted_by"]["choice"]
            coverage = choice.get("coverage", {}).get("leaf_keys")
            if coverage is not None and (not set(coverage) <= leaves or not coverage):
                raise task_changes.ChangeSetError("Accepted prerequisite coverage does not match leaves")
            for leaf in sorted(leaves if coverage is None else coverage):
                edge = {"from": leaf, "to": task_id, "dep_type": "blocks"}
                if leaf != task_id and edge not in payload["edges"]:
                    payload["edges"].append(edge)
        return payload, packet

    @staticmethod
    def _design_spec_batch(path: str) -> tuple[list[dict], list[dict]]:
        """Design approval schedules its implementation document, never implementation work."""
        return ([
            {"tempId": "implementation_spec", "title": "Implementation specification",
             "description": f"Implementation planning for approved design {path}.",
             "section_anchor": "Implementation specification",
             "task_type": "design", "intelligence_class": "deep-high"},
            {"tempId": "write_spec", "title": f"Write implementation spec for {Path(path).name}",
             "task_type": "design", "intelligence_class": "deep-high",
             "section_anchor": "Implementation specification",
             "primary_outcome": "Approved implementation specification",
             "acceptance": "Submit the implementation specification for document review",
             "description": (
                 f"Read approved design spec {path} and inspect the current repository. "
                 "Write an implementation spec grounded in files, functions, tests and rollout. "
                 "Follow docs/templates/spec.md and the Spec supersession checklist in "
                 "docs/guides/reviews.md: identify approved specs replaced or overlapped, "
                 "including this design, cite approved revisions, and list every predecessor "
                 "goal as kept, changed or dropped with replacement evidence and reasons. "
                 "Flag every drop in the review; each requires Jack's explicit sign-off for "
                 "the goal and replacement revision. Record decision evidence or pending "
                 "sign-off; pending drops block approval and affected implementation work. "
                 "Delegated or general approval alone does not authorize dropping a goal. "
                 "Choose defaults for open questions. Include spec_kind: implementation in "
                 "frontmatter. File no implementation work. Submit the document to Jack with "
                 "aq review submit --task-id <held-task> --file <draft> --kind spec --title "
                 "<title>; do not commit the review draft. Its approval triggers ingestion."
             ),
             "deliverables": [{"id": "implementation_review", "kind": "review", "target": "spec"}]},
        ], [{"from": "write_spec", "to": "implementation_spec", "dep_type": "parent-child"}])

    async def _validate_ingest_graph(self, conn, payload, project_id, source):
        """Only new work may use the approved document's ingestion authority."""
        if payload["edits"] or payload["remove_edges"] or payload["comments"]:
            raise task_changes.ChangeSetError("spec-ingest batches only create tasks and edges")
        report = await batch_checks.run(
            conn, payload, authority=batch_checks.Authority(project_id, source)
        )
        report.raise_for_hard()
        graph = batch_checks.BatchGraph.from_payload(payload)
        containers = {parent for node in graph.nodes.values() for parent in node.parents}
        if any(not node.parents and tid not in containers
               and node.placement != "operational_root" for tid, node in graph.nodes.items()):
            raise task_changes.ChangeSetError(
                "spec-ingest root tasks must be epics with children or operational roots",
                "membership",
            )
        return report

    async def _batch_receipt(self, conn, batch, *, replayed=False):
        members = await batches.member_map(conn, batch["batch_id"])
        stored = json.loads((await conn.execute(select(task_proposals.c.payload).where(
            task_proposals.c.id == batch["batch_id"],
        ))).scalar_one())
        original_ids = stored.get("receipt", {}).get("task_ids", [])
        task_ids = list(dict.fromkeys(
            [tid for tid in original_ids if tid in members.values()] + list(members.values())
        ))
        return {
            "success": True, "proposal_id": batch["batch_id"], "batch_id": batch["batch_id"],
            "status": "held" if batch["state"] in batches.HELD_STATES else batch["state"],
            "state": batch["state"], "structure_revision": batch["structure_revision"],
            "structure_digest": batch["structure_digest"], "members": members,
            "task_ids": task_ids, "replayed": replayed,
            "lineage": {key: batch[key] for key in (
                "lineage_kind", "design_review_id", "design_revision", "design_content_sha256",
            )},
            "release_receipt": batch["release_receipt"],
            "reviewer_task_id": batch["reviewer_task_id"],
        }

    async def _create_ingest_batch(self, conn, proposal_id, project_id, source, payload,
                                   ingest, packet, report):
        design = packet["spec_kind"] == "design"
        source_id = packet["review_id"] or packet["snapshot_id"]
        lineage = await batches.resolve_design_lineage(conn, source_id)
        created = await batches.create_batch(
            conn, batch_id=proposal_id, project_id=project_id,
            source_review_id=source_id, source_revision=packet["revision"] or 1,
            source_content_sha256=packet["content_sha256"], decision_id=packet["decision_id"],
            lineage=lineage, idempotency_key=f"ingest:{packet['decision_id']}",
            state="released" if design else "held", check_results=report.to_dict(),
            release_actor="service:design_authoring_shape" if design else None,
            release_receipt={"kind": "design_authoring_shape"} if design else None,
        )
        if created["replayed"]:
            raise task_changes.ChangeSetError("Batch source changed under the ingestion lock")
        expected, rows, project = await task_changes.snapshot(conn, project_id, payload)
        receipt, notifications = await task_changes.apply(
            self, conn, project_id, payload, expected, rows, project, source,
            approved=True, hold=proposal_id,
        )
        for task_id in receipt["task_ids"]:
            await self.db._upsert_meta(task_id, PROPOSAL_ID_META, proposal_id, conn=conn)
            await self.db._upsert_meta(task_id, SPEC_INGEST_META,
                                      payload[SPEC_INGEST_META], conn=conn)
        structure_digest = await batches.store_live_digest(conn, proposal_id)
        if design:
            await conn.execute(update(ingest_batches).where(
                ingest_batches.c.batch_id == proposal_id,
            ).values(release_digest=structure_digest))
        payload["receipt"] = receipt
        payload[SPEC_INGEST_META] = {**payload[SPEC_INGEST_META], "task_ids": receipt["task_ids"]}
        await conn.execute(update(task_proposals).where(task_proposals.c.id == proposal_id).values(
            status="committed" if design else "held", payload=json.dumps(payload),
            updated_at=time.time(),
        ))
        decision = await self.db.get_ask_decision(ingest["decision_id"], conn=conn)
        op = await operation(self.db, decision, "materialization", conn=conn)
        if op["status"] != "pending":
            raise task_changes.ChangeSetError("Decision already materialized or needs reconciliation")
        batch = await batches.get_batch(conn, proposal_id)
        result = await self._batch_receipt(conn, batch)
        await succeed(conn, op, result)
        if not design:
            await operation(self.db, decision, "fidelity_review.dispatch", conn=conn,
                            parameters={"batch_id": proposal_id, "structure_revision": 1})
            event_id = f"batch.held:{proposal_id}:1"
            await enqueue_integration_event(
                conn, event_id=event_id, dedup_key=event_id, project_id=project_id,
                event_type="batch.held", available_at=time.time(), payload={
                    "project_id": project_id, "batch_id": proposal_id, "structure_revision": 1,
                    "structure_digest": structure_digest, "reviewer_task_id": None,
                },
            )
        await conn.execute(insert(events).values(
            event_type="task.change_set_committed" if design else "task.change_set_held",
            project_id=project_id, timestamp=time.time(), payload=json.dumps({
                "proposal_id": proposal_id, "source": source, "diff": payload.get("diff"),
                "receipt": result, "actor": ingest["task_id"],
            }),
        ))
        return result, notifications

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
            if source.startswith(BATCH_REPAIR_PREFIX):
                payload["diff"]["impact"] = await self._batch_repair_impact(
                    conn, project, source, payload, rows, receipt, aliases
                )
        finally:
            await savepoint.rollback()
        return payload

    @staticmethod
    def _change_set_failure(exc, command="task_batch_propose"):
        if getattr(exc, "refused", None):
            return choice_forbidden(command, exc.refused)
        return {
            # A hierarchy refusal (``needs_quiescence``) names its owner and
            # safe continuation; keep them beside the code.
            **(getattr(exc, "context", None) or {}),
            "success": False,
            "code": getattr(exc, "code", "change_set.invalid"),
            "error": str(exc),
        }

    async def _batch_repair_scope(self, conn, project_id, source, payload):
        """``(batch, None)`` for an in-scope supervisor repair, else ``(None, why)``.

        Narrow by construction (spec P4 L4.4): the caller is this project's
        live named supervisor; ``source`` names a released batch of the same
        project; every edit and removed edge touches only its active members;
        and each operation is an L4.1 move, an L4.2 split remainder (a created
        task under, or delivering to, a member) or an L4.3 retirement, each
        with a reason.  Added edges, created tasks and comments stay inside the
        members and the tasks this set creates.  Anything else keeps the gate.
        """
        principal = current_principal()
        actor, refusal = await operator_or_supervisor(
            self.db, project_id, subject="batch repair"
        )
        if (refusal or not actor.startswith("supervisor session:")
                or principal is None or principal.project_id != project_id):
            return None, "only this project's live supervisor may repair a released batch"
        batch_id = source.removeprefix(BATCH_REPAIR_PREFIX)
        batch = await batches.get_batch(conn, batch_id, lock=True)
        if batch is None or batch["project_id"] != project_id:
            return None, f"'{batch_id}' is not an ingest batch of project '{project_id}'"
        if batch["state"] != "released":
            return None, f"batch '{batch_id}' is {batch['state']}, not released"
        members = {
            row["task_id"] for row in await batches.members(conn, batch_id, state="active")
        }
        created = {spec["tempId"] for spec in payload["tasks"]}

        def outside(*task_ids, allowed=members | created):
            return sorted({tid for tid in task_ids if tid} - allowed)

        for edit in payload["edits"]:
            tid = edit["task_id"]
            if outside(tid, allowed=members):
                return None, f"{tid} is not an active member of batch '{batch_id}'"
            if not (edit.get("reason") or "").strip():
                return None, f"the repair of {tid} needs a reason"
            if edit.get("action") == "archive" and set(edit) <= _REPAIR_RETIRE_FIELDS:
                if stray := outside(edit.get("replacement_task_id")):
                    return None, f"replacement {stray[0]} is outside batch '{batch_id}'"
            elif "parent_id" in edit and set(edit) <= _REPAIR_MOVE_FIELDS:
                if stray := outside(edit["parent_id"]):
                    return None, f"new parent {stray[0]} is outside batch '{batch_id}'"
            else:
                return None, f"the edit of {tid} is not a move (L4.1) or retirement (L4.3)"
        for edge in payload["remove_edges"]:
            if stray := outside(edge["from"], edge["to"], allowed=members):
                return None, f"removed edge endpoint {stray[0]} is not an active member"
        for edge in payload["edges"]:
            if stray := outside(edge["from"], edge["to"]):
                return None, f"added edge endpoint {stray[0]} is outside batch '{batch_id}'"
            if edge.get("dep_type", "blocks") != "parent-child" and edge["from"] not in created:
                return None, "an added prerequisite must belong to a split remainder"
        for spec in payload["tasks"]:
            anchor = [spec.get("parent_id"), *spec.get("delivery_targets", [])]
            if not any(anchor):
                return None, f"split remainder {spec['tempId']} needs a member parent or target"
            if stray := outside(*anchor):
                return None, f"split remainder anchor {stray[0]} is outside batch '{batch_id}'"
        for comment in payload["comments"]:
            if stray := outside(comment["task_id"]):
                return None, f"comment target {stray[0]} is outside batch '{batch_id}'"
        return {**batch, "actor": actor}, None

    async def _batch_repair_impact(self, conn, project, source, payload, rows, receipt, aliases):
        """The impact set of a ``batch-repair:`` change set (spec P4 L4.4).

        Read inside the validation savepoint after the set applied, so moves,
        retirements, prerequisites and coverage describe the final graph.
        ``delivery_target`` is the structural root the moved work delivers
        into; ``active_owner`` names claims and an active integration batch
        that would make a move wait for quiescence.
        """
        batch_id = source.removeprefix(BATCH_REPAIR_PREFIX)
        temp = receipt["temp_ids"]

        def shown(tid):
            return aliases.get(tid, tid)

        parents = {
            row["id"]: row["parent_task_id"]
            for row in (await conn.execute(
                select(tasks.c.id, tasks.c.parent_task_id)
                .where(tasks.c.project_id == project["id"])
            )).mappings()
        }

        def root(tid):
            while parents.get(tid):
                tid = parents[tid]
            return tid

        moved = [
            {"task_id": tid, "old_parent": rows[tid]["parent_task_id"],
             "new_parent": shown(parents.get(tid)), "delivery_target": shown(root(tid))}
            for tid in sorted(rows)
            if tid in parents and parents[tid] != rows[tid]["parent_task_id"]
        ]
        retired = [
            {"task_id": edit["task_id"], "reason": edit.get("reason"),
             **{key: edit.get(key) for key in task_changes.RETIREMENT_FIELDS}}
            for edit in payload["edits"] if edit.get("action") == "archive"
        ]
        created = [
            {"temp_id": spec["tempId"], "parent_id": spec.get("parent_id"),
             "placement": spec.get("placement", "child"),
             "delivery_targets": spec.get("delivery_targets", []),
             "delivery_target": shown(root(temp[spec["tempId"]]))}
            for spec in payload["tasks"]
        ]
        touched = {entry["task_id"] for entry in moved} | set(temp.values()) | {
            temp.get(tid, tid) for tid in (e.get("replacement_task_id") for e in retired) if tid
        }
        prerequisites = {}
        for row in (await conn.execute(
            select(task_dependencies).where(
                task_dependencies.c.task_id.in_(touched),
                task_dependencies.c.dep_type != "parent-child",
            ).order_by(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id)
        )).mappings():
            prerequisites.setdefault(shown(row["task_id"]), []).append(
                {"task_id": shown(row["depends_on_task_id"]), "dep_type": row["dep_type"]}
            )
        member_rows = (await conn.execute(
            select(ingest_batch_members).where(
                ingest_batch_members.c.batch_id == batch_id,
                ingest_batch_members.c.state == "active",
            )
        )).mappings().all()
        gone = {entry["task_id"] for entry in retired}
        anchors = {row["section_anchor"] for row in member_rows}
        covered = {row["section_anchor"] for row in member_rows if row["task_id"] not in gone}
        existing = {entry["task_id"] for entry in moved + retired}
        old_parents = {rows[tid]["parent_task_id"] for tid in existing if tid in rows}
        owners = [
            {"task_id": tid, "assigned_agent_id": rows[tid]["assigned_agent_id"],
             "branch_name": rows[tid]["branch_name"]}
            for tid in sorted(existing)
            if tid in rows and (rows[tid]["assigned_agent_id"] or rows[tid]["branch_name"])
        ]
        integration = None
        if project["hierarchical_integration_mode"] in {"hierarchy", "train"} and existing:
            integration = await self._hierarchy_integration_service()._active_batch_owner(
                conn, sorted((existing | old_parents | {parents.get(t) for t in existing}) - {None})
            )
        return {
            "batch_id": batch_id,
            "moved": moved,
            "retired": retired,
            "created": created,
            "prerequisites": prerequisites,
            "remaining_source_coverage": {
                "covered": sorted(covered),
                # A section left without an active member after this repair;
                # a split remainder must carry it forward.
                "uncovered": sorted(anchors - covered),
            },
            "active_owner": {"tasks": owners, "integration_batch": integration},
        }

    async def _stamp_batch_repair(self, conn, project_id, source, payload) -> bool:
        """Stamp an in-scope supervisor repair, recording the verdict in its impact."""
        if not source.startswith(BATCH_REPAIR_PREFIX):
            return False
        batch, refusal = await self._batch_repair_scope(conn, project_id, source, payload)
        payload["diff"]["impact"]["bypass"] = {"eligible": batch is not None, "refusal": refusal}
        if batch is not None:
            payload[BATCH_REPAIR_META] = {"batch_id": batch["batch_id"], "actor": batch["actor"]}
        return batch is not None

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
        committed = notifications = None
        repair = False
        try:
            async with task_changes.boundary(
                self.db, project_id,
                source_lock=(lambda conn: self._lock_ingest(conn, ingest)) if ingest else None,
            ) as conn:
                if ingest:
                    packet = await self._lock_ingest(conn, ingest)
                    existing_batch = await batches.find_batch_by_source(
                        conn, project_id, packet["review_id"] or packet["snapshot_id"],
                        packet["revision"] or 1, packet["content_sha256"], lock=True,
                    )
                    if existing_batch:
                        return await self._batch_receipt(conn, existing_batch, replayed=True)
                    prior_id = (await conn.execute(select(ingest_batches.c.batch_id).where(
                        ingest_batches.c.project_id == project_id,
                        or_(
                            ingest_batches.c.source_review_id == (
                                packet["review_id"] or packet["snapshot_id"]
                            ),
                            ingest_batches.c.source_review_id.in_(select(
                                approved_document_snapshots.c.id,
                            ).where(
                                approved_document_snapshots.c.project_id == project_id,
                                approved_document_snapshots.c.source_path == packet["spec_path"],
                            )),
                        ),
                    ).order_by(ingest_batches.c.created_at.desc()).limit(1))).scalar_one_or_none()
                    if prior_id:
                        prior = await batches.get_batch(conn, prior_id, lock=True)
                        return {
                            "success": False, "error_code": "incremental_plan_required",
                            "error": "Source changed; use an explicit incremental plan",
                            "previous_receipt": await self._batch_receipt(conn, prior, replayed=True),
                        }
                    existing = await get_operation(conn, ingest["decision_id"], "materialization", lock=True)
                    if existing and existing["status"] == "succeeded":
                        return {"success": True, "committed": True, "already_committed": True,
                                **existing["receipt"]}
                    change_set, packet = await self._ingest_change_set(conn, change_set, ingest)
                    report = await self._validate_ingest_graph(conn, change_set, project_id, source)
                payload = await self._prepare_change_set(conn, project_id, change_set, source)
                repair = await self._stamp_batch_repair(conn, project_id, source, payload)
                if args.get("dry_run"):
                    return {"success": True, "dry_run": True, "diff": payload["diff"]}
                if ingest:
                    payload[SPEC_INGEST_META] = {**ingest, "feedback": packet["feedback"]}
                proposal_id = await proposal_queries.insert_proposal(
                    self.db,
                    project_id=project_id,
                    source=source,
                    payload=payload,
                    conn=conn,
                    status="ready",
                )
                if ingest:
                    committed, notifications = await self._create_ingest_batch(
                        conn, proposal_id, project_id, source, payload, ingest, packet, report
                    )
        except Exception as exc:
            return self._change_set_failure(exc)
        if committed is not None:
            await self._publish_committed(
                proposal_id, project_id, committed["task_ids"], notifications,
                status="held" if committed["state"] in batches.HELD_STATES else "committed",
            )
            return {**committed, "diff": payload["diff"]}
        if not repair:
            # An in-scope supervisor repair needs no human gate; the
            # supervisor reviews the impact preview and commits it.
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
                # A gate-free repair stays gate-free only while it stays in scope:
                # no proposal.ready gate was ever opened for it.
                stamped = json.loads(current["payload"]).get(BATCH_REPAIR_META)
                if stamped and not await self._stamp_batch_repair(
                    conn, row["project_id"], row["source"], payload
                ):
                    raise task_changes.ChangeSetError(
                        "this batch repair would leave its scope "
                        f"({payload['diff']['impact']['bypass']['refusal']}); "
                        "propose the change as a new batch for human approval"
                    )
                if not await proposal_queries.update_ungated_payload(
                    self.db, proposal_id, project_id=row["project_id"], payload=payload, conn=conn
                ):
                    raise task_changes.ChangeSetError(
                        f"proposal '{proposal_id}' already has an approval gate; its payload is frozen; "
                        "commit it as approved and put subsequent changes in a new batch"
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
        async with task_changes.boundary(self.db, row["project_id"]) as conn:
            batch = await batches.get_batch(conn, proposal_id, lock=True)
            if batch and batch["state"] in batches.HELD_STATES:
                return {"success": False, "error_code": "batch_retire_required",
                        "error": "Held batches must be retired, not discarded",
                        "batch_id": proposal_id, "state": batch["state"]}
            approval_gate = exists().where(
                gates.c.project_id == row["project_id"],
                gates.c.gate_type == "human",
                gates.c.await_id == proposal_id,
            )
            result = await conn.execute(
                update(task_proposals)
                .where(
                    task_proposals.c.id == proposal_id,
                    task_proposals.c.status.in_(("draft", "ready")),
                    ~approval_gate,
                )
                .values(status="discarded", updated_at=time.time())
            )
            if not result.rowcount:
                return {
                    "success": False,
                    "error": "proposal has an approval gate or was already committed or discarded",
                }
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
        self, row: dict, *, gate_id: str | None, project_id: str | None, conn=None
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
        A ``batch-repair:`` proposal stamped at propose time is the second
        bypass: :meth:`_batch_repair_scope` re-runs against the committing
        principal and live membership, and any refusal falls back to the gate.
        """
        proposal_id, owner = row["id"], row["project_id"]
        if project_id and project_id != owner:
            return f"proposal '{proposal_id}' belongs to project '{owner}', not '{project_id}'"
        if row["payload"].get(SPEC_INGEST_META):
            return None
        repair = row["payload"].get(BATCH_REPAIR_META)
        if repair and conn is not None and row["source"] == BATCH_REPAIR_PREFIX + repair["batch_id"]:
            # Re-checked at commit, as the committing principal: the batch,
            # membership and caller must still be in scope (spec P4 L4.4).
            batch, refusal = await self._batch_repair_scope(
                conn, owner, row["source"], task_changes.normalize(row["payload"])
            )
            if batch is not None:
                return None
            error = await self._gate_approval_error(row, gate_id=gate_id, conn=conn)
            return error and f"{error}; the batch repair is out of scope: {refusal}"
        return await self._gate_approval_error(row, gate_id=gate_id, conn=conn)

    async def _gate_approval_error(self, row: dict, *, gate_id: str | None, conn) -> str | None:
        """Why no resolved human gate approves this proposal, or ``None``."""
        proposal_id, owner = row["id"], row["project_id"]
        if gate_id:
            gate = await self.db.get_gate(str(gate_id), conn=conn, lock=conn is not None)
            if gate is None:
                return f"approval gate '{gate_id}' not found"
        else:
            candidates = await self.db.list_gates(
                project_id=owner, gate_type="human", await_id=proposal_id,
                conn=conn, lock=conn is not None
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

    async def _repair_batch_membership(self, conn, batch_id, payload, receipt) -> None:
        """Keep a released batch's membership the record of its repaired structure.

        Retired tasks leave the batch audit-intact; split remainders join it,
        carrying their own section anchor or their parent's (or delivery
        target's), so source coverage stays answerable.  A released batch has
        no revision fence (``bump_revision`` is for held batches), so rows are
        stamped with its current ``structure_revision``.
        """
        batch = await batches.get_batch(conn, batch_id, lock=True)
        revision = batch["structure_revision"]
        reasons = {e["task_id"]: e["reason"] for e in payload["edits"]
                   if e.get("action") == "archive"}
        active = {row["task_id"]: row for row in await batches.members(
            conn, batch_id, state="active", lock=True)}
        for task_id in receipt["archived_task_ids"]:
            if task_id in active:
                await batches.retire_member(
                    conn, batch_id, task_id,
                    reason=reasons.get(task_id, "retired with its archived ancestor"),
                    retired_revision=revision,
                )
        temp = receipt["temp_ids"]
        specs = {spec["tempId"]: spec for spec in payload["tasks"]}
        parents = {tid: spec.get("parent_id") for tid, spec in specs.items()}

        def anchor(tid):
            if tid not in specs:
                return active[tid]["section_anchor"]
            spec = specs[tid]
            return spec.get("section_anchor") or anchor(
                spec.get("parent_id") or spec["delivery_targets"][0]
            )

        for tid, spec in specs.items():
            role = ("operational_root" if spec.get("placement") == "operational_root"
                    else "container" if tid in parents.values() else "leaf")
            joined = await batches.add_member(
                conn, batch_id, task_id=temp[tid], member_key=f"repair:{temp[tid]}", role=role,
                section_anchor=anchor(tid), primary_outcome=spec.get("primary_outcome", ""),
                created_revision=revision, acceptance=spec.get("acceptance"),
                constraints=spec.get("constraints"), size=spec.get("size"),
            )
            if not joined["success"]:
                raise task_changes.ChangeSetError(joined["error"])

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
        if args.get("project_id") and args["project_id"] != project_id:
            return {"success": False, "not_approved": True,
                    "error": "proposal belongs to another project"}
        try:
            ingest = row["payload"].get(SPEC_INGEST_META)
            if ingest and not ingest.get("decision_id") and row["status"] != "committed":
                raise task_changes.ChangeSetError("Legacy ingestion requires operator reconciliation")
            async with task_changes.boundary(
                self.db, project_id,
                source_lock=(lambda conn: self._lock_ingest(conn, ingest, require_current=False))
                if ingest and row["status"] != "committed" else None,
            ) as conn:
                batch = await batches.get_batch(conn, proposal_id, lock=True)
                if batch:
                    if batch["state"] in batches.HELD_STATES:
                        return {
                            "success": False, "error_code": "fidelity_review_required",
                            "error": "The batch requires fidelity review and authorized release",
                            "batch_id": proposal_id, "state": batch["state"],
                            "reviewer_task_id": batch["reviewer_task_id"],
                        }
                    if batch["state"] == "released":
                        return {**await self._batch_receipt(conn, batch, replayed=True),
                                "already_committed": True}
                    raise task_changes.ChangeSetError("batch was retired")
                if ingest and row["status"] != "committed":
                    raise task_changes.ChangeSetError("Legacy ingestion requires operator reconciliation")
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
            current, gate_id=gate_id, project_id=project_id, conn=conn
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
                "missing_task_ids": stored.get("receipt", {}).get("missing_task_ids", []),
                "skipped_operations": stored.get("receipt", {}).get("skipped_operations", []),
            }, None
        if current["status"] != "ready":
            raise task_changes.ChangeSetError("proposal not in 'ready' state")
        if stored.get(SPEC_INGEST_META):
            raise task_changes.ChangeSetError("Legacy ingestion requires operator reconciliation")
        payload = task_changes.normalize(stored)
        expected, rows, project = await task_changes.snapshot(
            conn,
            current["project_id"],
            payload,
            allow_missing=True,
        )
        payload, omissions = task_changes.surviving_operations(payload, rows)
        receipt, notifications = await task_changes.apply(
            self, conn, current["project_id"], payload, expected, rows, project, current["source"],
            approved=True,
        )
        receipt.update(omissions)
        if stored.get(BATCH_REPAIR_META):
            await self._repair_batch_membership(conn, stored[BATCH_REPAIR_META]["batch_id"],
                                                payload, receipt)
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
        return {"success": True, "task_ids": receipt["task_ids"], **omissions}, notifications

    async def _publish_committed(
        self, proposal_id: str, project_id: str, task_ids: list[str], notifications: dict,
        *, status: str = "committed",
    ) -> None:
        """Announce a materialized change set after its transaction commits."""
        # No task/routing event is emitted until every change and its audit
        # are durable. A failing listener cannot turn this commit into failure.
        await self._emit_proposal_event(
            "proposal.status_changed",
            {"project_id": project_id, "proposal_id": proposal_id, "status": status},
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
