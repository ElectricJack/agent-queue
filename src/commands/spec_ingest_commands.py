"""Server-owned approval task receipts and the scoped immutable document read."""

import time

from sqlalchemy import or_, select, update

from src.api.auth import RequestScope
from src.api.scope import spec_ingest_task_for_session
from src.asks.ingestion import INGEST_META, approved_packet, dispatch, stamp_for, task_stamp
from src.asks.models import AskError
from src.asks.operations import get_operation, succeed
from src.commands.principal import (
    TRUSTED_LOCAL, ExecutionPrincipal, PrincipalKind, current_principal, principal_context,
)
from src.database.tables import archived_tasks, escalation_actions, tasks


class IngestExists(Exception):
    def __init__(self, receipt):
        self.receipt = receipt


class SpecIngestCommandsMixin:
    async def _cmd_ensure_spec_ingest(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION:
            return {"success": False, "error": "Only approved policy may dispatch ingestion"}
        if set(args) - {"project_id", "decision_id", "packet_sha256"}:
            return {"success": False, "error": "Ingestion metadata and role are server-owned"}
        project_id, decision_id, sha = (args.get(k) for k in (
            "project_id", "decision_id", "packet_sha256",
        ))
        if not all((project_id, decision_id, sha)):
            return {"success": False, "error": "project_id, decision_id and packet_sha256 required"}
        if principal.project_id and principal.project_id != project_id:
            return {"success": False, "error": "out of scope: project mismatch"}
        key = f"spec-ingest:decision:{decision_id}"

        async def before(conn):
            observed = await get_operation(conn, decision_id, "ingestion")
            packet = await approved_packet(
                self.db, decision_id, sha, project_id, conn=conn,
                require_current=not observed or observed["status"] != "succeeded",
            )
            op = await get_operation(conn, decision_id, "ingestion", lock=True)
            if not op or op["parameters"]["packet_sha256"] != sha:
                raise AskError("ask.application_unknown", "No matching ingestion dispatch")
            if op["status"] == "succeeded":
                receipt = op["receipt"] or {}
                tid = receipt.get("task_id")
                found = (await conn.execute(select(tasks.c.id).where(
                    tasks.c.id == tid, tasks.c.project_id == project_id,
                    tasks.c.dedup_key == key,
                ))).first()
                archived = (await conn.execute(select(archived_tasks.c.id).where(
                    archived_tasks.c.id == tid, archived_tasks.c.project_id == project_id,
                ))).first()
                if not (found or archived) or receipt.get("packet_sha256") != sha:
                    raise AskError("ask.application_unknown", "Ingestion receipt task is missing")
                raise IngestExists(receipt)
            # A dedup string alone never proves that policy stamped this task.
            if (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == project_id, tasks.c.dedup_key == key,
            ))).first():
                raise AskError("ask.idempotency_conflict", "Ingestion key has no task receipt")
            return packet

        async def record(conn, task_id, parent_id):
            packet = await approved_packet(self.db, decision_id, sha, project_id, conn=conn)
            await self.db._upsert_meta(task_id, INGEST_META, stamp_for(packet), conn=conn)
            op = await get_operation(conn, decision_id, "ingestion", lock=True)
            await succeed(conn, op, {**(op["receipt"] or {}), "task_id": task_id,
                                    "packet_sha256": sha, "dedup_key": key})
            pending = (await conn.execute(select(escalation_actions.c.id).where(
                escalation_actions.c.decision_id == decision_id,
                escalation_actions.c.status != "succeeded",
            ))).first()
            if not pending:
                decision = await self.db.get_ask_decision(decision_id, conn=conn)
                await self.db.update_ask_projection(decision["ask_id"], {
                    "application_state": "applied", "next_actor": "none",
                }, conn=conn)

        try:
            async with self.db.immediate() as conn:
                packet = await before(conn)
            # This is domain command composition, after checking the immutable
            # application receipt. No caller can choose the role or task metadata.
            with principal_context(ExecutionPrincipal.service("approved-spec-ingestion")):
                created = await self._cmd_create_task({
                    "project_id": project_id, "profile_id": "spec-ingest",
                    "title": f"Ingest approved decision {decision_id}", "dedup_key": key,
                    "root": True, "_suppress_created_event": True,
                    "description": (
                        f"Approved decision: {decision_id}\nPacket SHA-256: {sha}\n"
                        f"spec_path: {packet['spec_path']} (mirror only)\n"
                        f"spec_kind: {packet['spec_kind']}\n"
                        "Read approved_spec_packet with this decision/hash. Preserve every feedback "
                        "obligation and existing follow-up. Use task_batch_propose once for the "
                        "complete graph; the server applies it atomically and returns its receipt. "
                        "Follow docs/templates/spec.md and the Spec supersession checklist in "
                        "docs/guides/reviews.md. Account for every predecessor goal as kept, changed "
                        "or dropped with approved-revision citations, evidence and reasons. Every "
                        "drop requires Jack's explicit sign-off for the goal and replacement "
                        "revision. Report missing accounting or unsigned drops to the supervisor "
                        "and do not commit affected work; general approval alone is insufficient."
                    ),
                    "_before_create_on": before, "_after_create_on": record,
                })
            if not created.get("success"):
                return created
            return {"success": True, "task_id": created["created"], "created": True}
        except IngestExists as existing:
            return {"success": True, "task_id": existing.receipt["task_id"], "created": False}
        except AskError as exc:
            return {"success": False, "error": str(exc), "code": exc.code}

    async def _cmd_approved_spec_packet(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.SESSION:
            return {"success": False, "error": "A live held spec-ingest role is required"}
        scope = RequestScope(
            kind="session", session_id=principal.session_id,
            session_instance_token=principal.session_instance_token,
            project_id=principal.project_id, task_id=principal.task_id,
        )
        task = await spec_ingest_task_for_session(self.db, scope)
        if task is None:
            return {"success": False, "error": "A live held spec-ingest role is required"}
        async with self.db.immediate() as conn:
            stamp = await task_stamp(conn, task.id)
            if (not stamp or args.get("decision_id") != stamp["decision_id"]
                    or args.get("packet_sha256") != stamp["packet_sha256"]
                    or args.get("project_id", task.project_id) != task.project_id):
                return {"success": False, "error": "out of scope: approved packet mismatch"}
            try:
                packet = await approved_packet(self.db, stamp["decision_id"], stamp["packet_sha256"],
                                               task.project_id, conn=conn)
                return {"success": True, **packet}
            except AskError as exc:
                return {"success": False, "error": str(exc), "code": exc.code}

    async def _cmd_spec_ingest_reconcile(self, args):
        if args:
            return {"success": False, "error": "Reconciliation accepts no caller authority"}
        if (current_principal() or TRUSTED_LOCAL).kind not in {
            PrincipalKind.LOCAL, PrincipalKind.SERVICE,
        }:
            return {"success": False, "error": "Ingestion reconciliation is operator-only"}
        async with self.db._engine.connect() as conn:
            ids = (await conn.execute(select(escalation_actions.c.decision_id).where(
                escalation_actions.c.operation_kind == "ingestion",
                escalation_actions.c.status != "succeeded",
                or_(escalation_actions.c.retry_at.is_(None),
                    escalation_actions.c.retry_at <= time.time()),
            ).order_by(escalation_actions.c.retry_at.nullsfirst()).limit(20))).scalars().all()
        errors = []
        for decision_id in ids:
            try:
                async with self.db._engine.connect() as conn:
                    decision = await self.db.get_ask_decision(decision_id, conn=conn)
                await dispatch(self.db, decision, self.orchestrator.bus.emit, self.config.vault_root)
            except Exception as exc:
                errors.append({"decision_id": decision_id, "error": str(exc)})
                # A stale source must not monopolize the bounded first page.
                # This changes retry telemetry only, never approval authority.
                async with self.db.immediate() as conn:
                    await conn.execute(update(escalation_actions).where(
                        escalation_actions.c.decision_id == decision_id,
                        escalation_actions.c.operation_kind == "ingestion",
                        escalation_actions.c.status != "succeeded",
                    ).values(retry_at=time.time() + 60, error=str(exc)))
        return {"success": True, "dispatched": len(ids) - len(errors), "errors": errors}

    async def _cmd_spec_ingest_legacy_inventory(self, args):
        if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return {"success": False, "error": "Legacy reconciliation is operator-only"}
        if not args.get("project_id") or set(args) - {"project_id", "after_id"}:
            return {"success": False, "error": "project_id required; only after_id is optional"}
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(
                tasks.c.id, tasks.c.status, tasks.c.dedup_key,
            ).where(
                tasks.c.project_id == args["project_id"], tasks.c.profile_id == "spec-ingest",
                tasks.c.dedup_key.startswith("spec-ingest:"),
                ~tasks.c.dedup_key.startswith("spec-ingest:decision:"),
                tasks.c.id > args.get("after_id", ""),
            ).order_by(tasks.c.id).limit(101))).mappings().all()
        return {"success": True, "assignments": [dict(r) for r in rows[:100]],
                "next_after_id": rows[99]["id"] if len(rows) > 100 else None,
                "policy": "Committed receipts remain readable. New materialization is held; "
                          "the operator must explicitly review/snapshot the source. No approval replay."}
