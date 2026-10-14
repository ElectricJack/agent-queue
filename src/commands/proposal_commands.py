"""Task-proposal commands — staged spec ingestion (design §8, Phase 6).

Ordinary proposals stage a human-approved change set. A live spec-ingest
assignment materializes implementation work atomically under a durable batch
hold; fidelity review and authorized release expose it to execution. Design
approval creates a released server-authored implementation-spec task. Commit
replays released batches and refuses held ones; discard cannot retire a hold.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import exists, insert, or_, select, update

from src.api.auth import RequestScope
from src.api.scope import fidelity_review_task_for_session, spec_ingest_task_for_session
from src.asks.ingestion import approved_packet, stamp_for, task_stamp
from src.asks.operations import get_operation, operation, succeed
from src.asks.models import AskError
from src.commands.contracts.builtin import TaskBatchReleaseArgs, TaskBatchRetireArgs
from src.commands import task_changes
from src.commands.principal import PrincipalKind, batch_release_actor, current_principal
from src.database.queries import ingest_batch_queries as batches
from src.database.queries import proposal_queries
from src.database.tables import (
    approved_document_snapshots,
    doc_review_revisions,
    agents,
    sessions,
    ask_decisions,
    human_asks,
    ingest_batch_members,
    ingest_batch_receipts,
    events,
    gates,
    ingest_batches,
    task_metadata,
    task_proposals,
    tasks,
)
from src.models import Task, TaskStatus
from src.integration.outbox import enqueue_batch_event, enqueue_integration_event
from src.routing.filing import choice_forbidden
from src.task_graph import batch_checks
from src.task_graph.batch_digest import canonical_batch_structure, normalize_description

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
        finally:
            await savepoint.rollback()
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
        committed = notifications = None
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
        await self._emit_proposal_event(
            "proposal.ready", {"project_id": project_id, "proposal_id": proposal_id}
        )
        return {"success": True, "proposal_id": proposal_id, "diff": payload["diff"]}

    async def _cmd_task_batch_update(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {"success": False, "error": f"proposal '{proposal_id}' not found"}
        async with self.db._engine.connect() as conn:
            batch = await batches.get_batch(conn, proposal_id)
        if batch is not None:
            return await self._update_held_batch(row, args)
        if row["payload"].get(SPEC_INGEST_META):
            return {"success": False, "error": "Legacy ingestion requires reconciliation"}
        if args.get("operations") is not None:
            return {"success": False, "error": "operations require a held ingestion batch"}
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
                        "commit it as approved and put subsequent changes in a new batch"
                    )
        except Exception as exc:
            return self._change_set_failure(exc, "task_batch_update")
        return {"success": True, "diff": payload["diff"]}

    async def _update_held_batch(self, row, args):
        """Reviewer-scoped changes, live proof and audit share one commit boundary."""
        batch_id, project_id = row["id"], row["project_id"]
        principal = current_principal()
        reviewer = None
        if principal and principal.kind is PrincipalKind.SESSION and not principal.elevated:
            reviewer = await fidelity_review_task_for_session(self.db, RequestScope(
                kind="session", session_id=principal.session_id,
                session_instance_token=principal.session_instance_token,
                project_id=principal.project_id, task_id=principal.task_id,
            ), batch_id)
            if reviewer is None:
                return {"success": False, "code": "authority", "error_code": "authority",
                        "error": "Only the live assigned reviewer may edit this held batch"}
        if ((principal and principal.project_id and principal.project_id != project_id)
                or args.get("project_id") not in (None, project_id)):
            return {"success": False, "code": "authority", "error_code": "authority",
                    "error": "Batch belongs to another project"}
        revision = args.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return {"success": False, "error": "expected_revision must be a positive integer"}
        if args.get("payload") is not None:
            return {"success": False, "error": "held updates take operations, not payload"}
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                batch = await batches.get_batch(conn, batch_id, lock=True)
                if batch["state"] not in batches.HELD_STATES:
                    raise task_changes.ChangeSetError("Batch is not held", "batch_not_held")
                if batch["structure_revision"] != revision:
                    raise task_changes.ChangeSetError("Reload the current batch revision", "stale_revision")
                if reviewer and (batch["reviewer_task_id"] != reviewer.id
                                 or batch["reviewer_claim_epoch"] != reviewer.claim_epoch
                                 or batch["reviewer_session_id"] != principal.session_id):
                    raise task_changes.ChangeSetError("Reviewer assignment changed", "authority")
                if reviewer:
                    # Re-read the live holder under the graph writer's claim
                    # fence: assignment discovery happened before this lock.
                    live = await conn.scalar(select(tasks.c.id).join(
                        sessions, sessions.c.task_id == tasks.c.id,
                    ).join(agents, agents.c.id == sessions.c.agent_id).where(
                        tasks.c.id == reviewer.id,
                        tasks.c.claim_epoch == reviewer.claim_epoch,
                        tasks.c.project_id == project_id,
                        tasks.c.profile_id == reviewer.profile_id,
                        tasks.c.route_source == "role", tasks.c.dedup_key == reviewer.dedup_key,
                        tasks.c.status == "IN_PROGRESS",
                        tasks.c.assigned_agent_id == agents.c.id,
                        sessions.c.id == principal.session_id,
                        sessions.c.project_id == project_id,
                        sessions.c.profile_id == reviewer.profile_id,
                        sessions.c.lifecycle.in_(("task", "pool")),
                        or_(principal.session_instance_token is None,
                            sessions.c.instance_token == principal.session_instance_token),
                        sessions.c.state.in_(("starting", "running")),
                        sessions.c.desired_state == "running",
                        or_(sessions.c.last_claim_epoch.is_(None),
                            sessions.c.last_claim_epoch == tasks.c.claim_epoch),
                        agents.c.enabled.is_(True), agents.c.deleted_at.is_(None),
                        agents.c.state == "BUSY", agents.c.current_task_id == tasks.c.id,
                    ))
                    if live is None:
                        raise task_changes.ChangeSetError("Reviewer no longer holds its claim", "authority")
                if batch["state"] == "needs_scope":
                    # A server-recorded answer permits a new review round; it
                    # never clears the hold or stands in for a fidelity receipt.
                    answered = await conn.scalar(select(ask_decisions.c.id).join(
                        human_asks, human_asks.c.id == ask_decisions.c.ask_id,
                    ).where(
                        human_asks.c.project_id == project_id,
                        human_asks.c.source_key == f"batch-scope:{batch_id}:{revision}",
                        human_asks.c.decision_state.in_(("answered", "approved", "changes_requested")),
                        human_asks.c.application_state == "applied",
                        ask_decisions.c.decision_generation == human_asks.c.decision_generation,
                        ask_decisions.c.source_fingerprint == human_asks.c.source_fingerprint,
                    ).limit(1))
                    if answered is None:
                        raise task_changes.ChangeSetError("Scope question needs an applied answer", "needs_scope")
                before_members, before_edges = await batches.hydrate_structure(conn, batch_id, lock=True)
                before = canonical_batch_structure(before_members, before_edges)
                digest = await batches.live_digest(conn, batch_id)
                if digest != batch["structure_digest"]:
                    raise task_changes.ChangeSetError("Live batch digest changed; reconcile it", "stale_revision")
                # Even a title-only operation refuses a live holder anywhere in
                # this held graph. Reuse the same claim/integration proof as release.
                pre = await batch_checks.run(
                    conn, batch_checks.BatchGraph.from_members(before_members, before_edges),
                    authority=batch_checks.Authority(project_id, row["source"]),
                    expected_revision=revision, batch_id=batch_id,
                )
                for finding in pre.hard:
                    if finding.code in ("active_claim", "integration_owned"):
                        raise task_changes.ChangeSetError(finding.detail, finding.code)
                payload = task_changes.held_operations(args.get("operations"), before_members, before_edges)
                structural = False
                by_id = {m["task_id"]: m for m in before_members}
                for edit in payload["edits"]:
                    if set(edit) - {"task_id", "title", "description", "priority", "tags"}:
                        structural = True
                    if "description" in edit and normalize_description(edit["description"]) != (
                        normalize_description(by_id[edit["task_id"]]["description"])
                    ):
                        structural = True
                structural |= bool(payload["tasks"] or payload["edges"] or payload["remove_edges"])
                try:
                    expected, rows, project = await task_changes.snapshot(conn, project_id, payload)
                    receipt, notifications = await task_changes.apply(
                        self, conn, project_id, payload, expected, rows, project, row["source"],
                        hold=batch_id, hold_revision=revision + int(structural),
                    )
                except task_changes.ChangeSetError as exc:
                    if exc.code == "change_set.invalid":
                        raise task_changes.ChangeSetError(str(exc), "validation_failed") from exc
                    raise
                for task_id in receipt["task_ids"]:
                    await self.db._upsert_meta(task_id, PROPOSAL_ID_META, batch_id, conn=conn)
                    await self.db._upsert_meta(task_id, SPEC_INGEST_META,
                                              row["payload"][SPEC_INGEST_META], conn=conn)
                # Parent edits can change container/leaf roles. Operational
                # placement is explicit and must survive this derived refresh.
                active = await batches.members(conn, batch_id, state="active")
                active_ids = [m["task_id"] for m in active]
                parents = set((await conn.execute(select(tasks.c.parent_task_id).where(
                    tasks.c.id.in_(active_ids),
                ))).scalars())
                for member in active:
                    if member["role"] != "operational_root":
                        await conn.execute(update(ingest_batch_members).where(
                            ingest_batch_members.c.batch_id == batch_id,
                            ingest_batch_members.c.task_id == member["task_id"],
                        ).values(role="container" if member["role"] == "container"
                                 or member["task_id"] in parents else "leaf"))
                after_members, after_edges = await batches.hydrate_structure(conn, batch_id)
                graph = batch_checks.BatchGraph.from_members(after_members, after_edges)
                report = await batch_checks.run(
                    conn, graph, authority=batch_checks.Authority(project_id, row["source"]),
                    expected_revision=revision, batch_id=batch_id,
                )
                if report.hard:
                    raise task_changes.ChangeSetError(
                        json.dumps(report.to_dict()), "validation_failed",
                    )
                # add_leaf/split/combine may never create a container by alias.
                created = set(receipt["task_ids"])
                if any(m["task_id"] in created and m["role"] == "container" for m in after_members):
                    raise task_changes.ChangeSetError("New scoped tasks must be leaves", "validation_failed")
                results = {**(batch["check_results"] or {}), **report.to_dict()}
                diff = {"before": before,
                        "after": canonical_batch_structure(after_members, after_edges),
                        "created": receipt["task_ids"], "edited": receipt["edited_task_ids"],
                        "retired": receipt["archived_task_ids"], "structural": structural}
                results["last_diff"] = diff
                if structural:
                    bumped = await batches.bump_revision(conn, batch_id, revision)
                    if not bumped["success"]:
                        raise task_changes.ChangeSetError(bumped["error"], bumped["error_code"])
                    superseded = dict(results.get("superseded_receipts", {}))
                    for rid in (await conn.execute(select(ingest_batch_receipts.c.receipt_id).where(
                        ingest_batch_receipts.c.batch_id == batch_id,
                        ingest_batch_receipts.c.structure_revision <= revision,
                    ))).scalars():
                        # Preserve each immutable historical verdict and record
                        # its supersession in the batch's review audit.
                        superseded[rid] = revision + 1
                    results["superseded_receipts"] = superseded
                elif await batches.live_digest(conn, batch_id) != digest:
                    raise task_changes.ChangeSetError("Nonstructural edit changed the digest", "validation_failed")
                if batch["state"] == "needs_scope":
                    await batches.set_state(conn, batch_id, "held", from_states={"needs_scope"})
                await batches.set_check_results(conn, batch_id, results)
                result = await self._batch_receipt(conn, await batches.get_batch(conn, batch_id))
                result.update(diff=diff)
                await conn.execute(insert(events).values(
                    event_type="task.batch_updated", project_id=project_id, timestamp=time.time(),
                    payload=json.dumps({"batch_id": batch_id, "previous_revision": revision,
                                        "structure_revision": result["structure_revision"],
                                        "actor": principal.session_id if principal else "operator",
                                        "diff": diff}),
                ))
        except Exception as exc:
            failure = self._change_set_failure(exc, "task_batch_update")
            failure["error_code"] = failure.get("code", "change_set.invalid")
            return failure
        await self._publish_committed(batch_id, project_id, receipt["task_ids"], notifications,
                                      status="held")
        return result

    async def _cmd_task_batch_release(self, args: dict) -> dict:
        """Atomically expose only the exact, approved and attested live graph."""
        try:
            request = TaskBatchReleaseArgs.model_validate(args)
        except ValidationError as exc:
            return {"success": False, "error_code": "invalid_arguments", "error": str(exc)}
        async with self.db._engine.connect() as conn:
            batch = await batches.get_batch(conn, request.batch_id)
        if batch is None:
            return batches._error("batch_not_found", "Batch not found")
        project_id, batch_id = batch["project_id"], request.batch_id
        if request.project_id not in (None, project_id):
            return batches._error("not_authorized", "Batch belongs to another project")
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                batch = await batches.get_batch(conn, batch_id, lock=True)
                if batch is None or batch["project_id"] != project_id:
                    return batches._error("batch_not_found", "Batch not found")
                actor = await batch_release_actor(conn, project_id)
                if actor is None:
                    return batches._error("not_authorized", "Only ingestion policy or the project "
                                          "supervisor may release a batch")
                # A committed release is immutable evidence: replay it even if
                # its source or tasks have since changed. Never clear holds twice.
                if batch["state"] == "released":
                    return await batches.release(
                        conn, batch_id, expected_revision=request.expected_revision,
                        expected_digest=request.expected_digest, actor=actor, release_receipt=None,
                    )
                if batch["state"] not in batches.HELD_STATES:
                    return batches._error("batch_not_held", "Batch is not held")
                decision = await self.db.get_ask_decision(batch["decision_id"], conn=conn)
                if decision is None:
                    return batches._error("source_diverged", "Source approval is missing")
                try:
                    packet = await approved_packet(
                        self.db, batch["decision_id"], decision["packet_sha256"], project_id, conn=conn,
                    )
                except AskError as exc:
                    return batches._error("source_diverged", str(exc))
                if packet["review_id"]:
                    source_hash = (await conn.execute(select(
                        doc_review_revisions.c.content_sha256,
                    ).where(
                        doc_review_revisions.c.review_id == packet["review_id"],
                        doc_review_revisions.c.revision == packet["revision"],
                    ).with_for_update())).scalar_one_or_none()
                else:
                    source_hash = (await conn.execute(select(
                        approved_document_snapshots.c.body_sha256,
                    ).where(
                        approved_document_snapshots.c.id == packet["snapshot_id"],
                    ).with_for_update())).scalar_one_or_none()
                if ((packet["review_id"] or packet["snapshot_id"]) != batch["source_review_id"]
                        or (packet["revision"] or 1) != batch["source_revision"]
                        or packet["content_sha256"] != batch["source_content_sha256"]
                        or source_hash != batch["source_content_sha256"]):
                    return batches._error("source_diverged", "Approved source identity changed")
                active = await batches.members(conn, batch_id, state="active", lock=True)
                member_rows, edges = await batches.hydrate_structure(conn, batch_id, lock=True)
                stale = batches._stale(batch, request.expected_revision, request.expected_digest,
                                       await batches.live_digest(conn, batch_id))
                if stale:
                    return stale
                ids = {member["task_id"] for member in active}
                held = set((await conn.execute(select(tasks.c.id).where(
                    tasks.c.held_batch_id == batch_id,
                ).with_for_update())).scalars())
                if not ids or held != ids:
                    return batches._error("membership", "Active membership and held tasks differ")
                source = (await conn.execute(select(task_proposals.c.source).where(
                    task_proposals.c.id == batch_id,
                ))).scalar_one()
                report = await batch_checks.run(
                    conn, batch_checks.BatchGraph.from_members(member_rows, edges),
                    authority=batch_checks.Authority(project_id, source),
                    expected_revision=request.expected_revision, batch_id=batch_id,
                )
                if report.hard:
                    return {**batches._error(report.hard[0].code, report.hard[0].detail),
                            **report.to_dict()}
                receipt = await batches.passing_receipt(conn, batch_id, request.expected_revision)
                if (not receipt or not batch["reviewer_task_id"]
                        or receipt["reviewer_task_id"] != batch["reviewer_task_id"]
                        or receipt["structure_digest"] != request.expected_digest):
                    return batches._error("fidelity_review_required", "Current reviewer must attest "
                                          "the exact revision and digest")
                sections = {member["section_anchor"] for member in active}
                verdicts = receipt["section_verdicts"]
                allowed = {"faithful", "fixed"}
                if (not isinstance(verdicts, dict) or not sections <= verdicts.keys()
                        or any(verdict not in allowed for verdict in verdicts.values())
                        or receipt["reverse_coverage_verdict"] not in allowed
                        or receipt["walkthrough_verdict"] not in allowed):
                    return batches._error("incomplete_review", "Every section, reverse coverage and "
                                          "walkthrough requires a passing verdict")
                # A question from an earlier revision also stays authoritative
                # until its answer has actually been applied by the ask service.
                open_ask = (await conn.execute(select(human_asks.c.id).where(
                    human_asks.c.project_id == project_id,
                    human_asks.c.source_kind == "batch_scope",
                    human_asks.c.source_identity["batch_id"].astext == batch_id,
                    or_(human_asks.c.decision_state != "answered",
                        human_asks.c.application_state != "applied"),
                ).with_for_update())).first()
                if batch["state"] == "needs_scope" or open_ask:
                    return batches._error("needs_scope", "Batch has an unresolved scope question")
                now = time.time()
                release_receipt = {
                    "batch_id": batch_id, "structure_revision": request.expected_revision,
                    "structure_digest": request.expected_digest, "actor": actor, "released_at": now,
                    "review_receipt_id": receipt["receipt_id"], "members": active,
                    "task_ids": sorted(ids),
                }
                result = await batches.release(
                    conn, batch_id, expected_revision=request.expected_revision,
                    expected_digest=request.expected_digest, actor=actor,
                    release_receipt=release_receipt, now=now,
                )
                if not result["success"]:
                    return result
                flipped = await self.db.recompute_blocked(ids, conn=conn)
                ready = await self.db._note_frontier_entry(conn, ids | flipped, reason="unblocked")
                released = await batches.get_batch(conn, batch_id)
                await enqueue_batch_event(conn, released, "batch.released", now=now)
                event_id = f"batch-committed:{batch_id}:{request.expected_revision}"
                payload = {"proposal_id": batch_id, "source": source,
                           "receipt": release_receipt, "actor": actor}
                await enqueue_integration_event(
                    conn, event_id=event_id, dedup_key=event_id, project_id=project_id,
                    event_type="task.change_set_committed", payload=payload, available_at=now,
                )
                await conn.execute(insert(events).values(
                    event_type="task.change_set_committed", project_id=project_id,
                    timestamp=now, payload=json.dumps(payload),
                ))
        except Exception as exc:
            failure = self._change_set_failure(exc, "task_batch_release")
            return {**failure, "error_code": failure.get("code", "release_failed")}
        await self._emit_proposal_event("proposal.status_changed", {
            "project_id": project_id, "proposal_id": batch_id, "status": "committed",
        })
        try:
            await self.db.log_blocked_flips(flipped)
            await self.db._notify_ready([(tid, "unblocked") for tid in ready])
        except Exception:
            logger.debug("Release notification failed for %s", batch_id, exc_info=True)
        return result

    async def _cmd_task_batch_retire(self, args: dict) -> dict:
        """Finalize prior supported retirement; never retire live work or clear holds."""
        from src.sessions.obsolete import ObsoleteCloseRefused
        from src.task_graph.batch_retirement import prove_retired_on

        try:
            request = TaskBatchRetireArgs.model_validate(args)
        except ValidationError as exc:
            return batches._error("invalid_arguments", str(exc))
        async with self.db._engine.connect() as conn:
            batch = await batches.get_batch(conn, request.batch_id)
        if batch is None:
            return batches._error("batch_not_found", "Batch not found")
        project_id, batch_id = batch["project_id"], request.batch_id
        if request.project_id not in (None, project_id):
            return batches._error("not_authorized", "Batch belongs to another project")
        try:
            async with task_changes.boundary(self.db, project_id) as conn:
                batch = await batches.get_batch(conn, batch_id, lock=True)
                actor = await batch_release_actor(conn, project_id)
                if actor is None:
                    return batches._error("not_authorized", "Only ingestion policy or the project "
                                          "supervisor may retire a batch")
                if batch is None:
                    return batches._error("batch_not_found", "Batch not found")
                if batch["structure_revision"] != request.expected_revision:
                    return batches._error("stale_revision", "Batch revision changed")
                if batch["state"] == "retired":
                    return {"success": True, "batch_id": batch_id, "state": "retired",
                            "already_retired": True}
                if batch["state"] not in batches.HELD_STATES or batch["release_receipt"]:
                    return batches._error("batch_not_held", "Only unreleased batches may retire")
                members = await batches.members(conn, batch_id, lock=True)
                evidence = await prove_retired_on(self.db, conn, batch, members)
                now = time.time()
                await batches.set_state(conn, batch_id, "retired", now=now)
                await conn.execute(update(task_proposals).where(
                    task_proposals.c.id == batch_id,
                ).values(status="discarded", updated_at=now))
                await self.db.log_event(
                    "task.batch_retired", project_id=project_id, conn=conn,
                    payload=json.dumps({"batch_id": batch_id, "actor": actor,
                                        "reason": request.reason,
                                        "structure_revision": request.expected_revision,
                                        "retirement_evidence": evidence}),
                )
        except ObsoleteCloseRefused as exc:
            return batches._error(exc.code, exc.detail)
        except Exception as exc:
            failure = self._change_set_failure(exc, "task_batch_retire")
            return {**failure, "error_code": failure.get("code", "retirement_failed")}
        await self._emit_proposal_event("proposal.status_changed", {
            "project_id": project_id, "proposal_id": batch_id, "status": "discarded",
        })
        return {"success": True, "batch_id": batch_id, "state": "retired",
                "already_retired": False}

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
        """
        proposal_id, owner = row["id"], row["project_id"]
        if project_id and project_id != owner:
            return f"proposal '{proposal_id}' belongs to project '{owner}', not '{project_id}'"
        if row["payload"].get(SPEC_INGEST_META):
            return None
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
