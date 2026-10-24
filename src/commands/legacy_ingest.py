"""Explicit legacy adoption through the current held-materialization boundary.

A path or an old proposal stamp is discovery evidence, never approval authority.
Unprovable sources and unresolved human gates remain on their existing path.
"""

import json
from pathlib import Path

from sqlalchemy import select

from src.asks.ingestion import approved_packet, stamp_for
from src.commands import task_changes
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.queries import ingest_batch_queries as batches, proposal_queries
from src.database.tables import gates, task_proposals


async def adopt_legacy(handler, args):
    if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
        return {"success": False, "error": "Legacy adoption is operator-only"}
    if set(args) - {"proposal_id", "decision_id", "packet_sha256", "leave_gated"}:
        return {"success": False, "error": "Unknown legacy adoption arguments"}
    row = await proposal_queries.get_proposal(handler.db, args.get("proposal_id"))
    if row is None or not row["source"].startswith("spec:"):
        return {"success": False, "error": "A legacy spec proposal is required"}
    async with handler.db._engine.connect() as conn:
        batch = await batches.get_batch(conn, row["id"])
        if batch:
            return await handler._batch_receipt(conn, batch, replayed=True)
    if row["status"] != "ready":
        return {"success": False, "error": "Only uncommitted ready proposals can be adopted"}
    if args.get("leave_gated"):
        return {"success": True, "left_gated": True, "proposal_id": row["id"]}
    stamp = row["payload"].get("spec_ingest") or {}
    decision_id = args.get("decision_id") or stamp.get("decision_id")
    if not decision_id and row["source"].startswith("spec:decision:"):
        decision_id = row["source"].removeprefix("spec:decision:")
    if not decision_id:
        return {"success": False, "code": "approval_required",
                "error": "Approve/snapshot the legacy source and supply its decision_id; "
                         "a spec: path does not authorize adoption"}

    async def source_lock(conn):
        decision = await handler.db.get_ask_decision(decision_id, conn=conn)
        sha = args.get("packet_sha256") or stamp.get("packet_sha256")
        return await approved_packet(
            handler.db, decision_id, sha or (decision or {}).get("packet_sha256"),
            row["project_id"], conn=conn,
        )

    try:
        async with task_changes.boundary(
            handler.db, row["project_id"], source_lock=source_lock,
        ) as conn:
            packet = await source_lock(conn)
            current = (await conn.execute(select(task_proposals).where(
                task_proposals.c.id == row["id"],
            ).with_for_update())).mappings().one()
            batch = await batches.get_batch(conn, row["id"], lock=True)
            if batch:
                return await handler._batch_receipt(conn, batch, replayed=True)
            if current["status"] != "ready":
                raise task_changes.ChangeSetError("Proposal is no longer ready")
            payload = json.loads(current["payload"])
            if payload.get("receipt") or (payload.get("spec_ingest") or {}).get("task_ids"):
                raise task_changes.ChangeSetError("Legacy proposal already has materialization evidence")
            allowed_sources = {
                f"spec:decision:{decision_id}", f"spec:{packet['spec_path']}",
                "spec:" + str(Path(handler.config.vault_root) / packet["spec_path"]),
            }
            if current["source"] not in allowed_sources or packet["spec_kind"] != "implementation":
                raise task_changes.ChangeSetError("Approval must match this implementation source")
            human_gates = (await conn.execute(select(gates).where(
                gates.c.project_id == row["project_id"], gates.c.await_id == row["id"],
                gates.c.gate_type == "human",
            ).with_for_update())).mappings().all()
            if any(g["status"] != "resolved" or g["resolution"] not in {"approve", "approved"}
                   for g in human_gates):
                return {"success": True, "left_gated": True, "proposal_id": row["id"]}
            prior = await batches.find_batch_by_source(
                conn, row["project_id"], packet["review_id"] or packet["snapshot_id"],
                packet["revision"] or 1, packet["content_sha256"], lock=True,
            )
            if prior:
                raise task_changes.ChangeSetError("Source already materialized in another batch")
            ingest = {**stamp_for(packet), "task_id": stamp.get("task_id") or "operator:legacy-adoption"}
            change_set, packet = await handler._ingest_change_set(conn, payload, ingest)
            report = await handler._validate_ingest_graph(
                conn, change_set, row["project_id"], current["source"],
            )
            prepared = await handler._prepare_change_set(
                conn, row["project_id"], change_set, current["source"],
            )
            prepared["legacy_proposal"] = {"source": current["source"], "payload": payload}
            prepared["spec_ingest"] = {**ingest, "feedback": packet["feedback"]}
            result, notifications = await handler._create_ingest_batch(
                conn, row["id"], row["project_id"], current["source"],
                prepared, ingest, packet, report,
            )
        await handler._publish_committed(
            row["id"], row["project_id"], result["task_ids"], notifications, status="held",
        )
        return result
    except Exception as exc:
        return handler._change_set_failure(exc, "spec_ingest_legacy_adopt")
