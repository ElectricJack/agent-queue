"""Recover source-owned effects without ever retrying an ambiguous submission."""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select

from src.asks.models import AskError, DecisionActor, OperationResult, SourceIdentity
from src.asks.operations import get_operation
from src.asks.sources import authorize_source, finish_on
from src.database.tables import project_constraints, projects


logger = logging.getLogger(__name__)


async def project_paused(db, project_id, *, conn=None):
    if conn is None:
        async with db._engine.connect() as owned:
            return await project_paused(db, project_id, conn=owned)
    status = await conn.scalar(select(projects.c.status).where(projects.c.id == project_id))
    paused = await conn.scalar(
        select(project_constraints.c.pause_scheduling).where(
            project_constraints.c.project_id == project_id,
        )
    )
    return status != "ACTIVE" or bool(paused)


async def apply(handler, decision):
    db = handler.db
    source = SourceIdentity(**decision["packet"]["source"])
    adapter = handler._ask_service().adapters.get(source.kind)
    owner = f"ask-apply:{uuid.uuid4().hex}"
    async with db.immediate() as conn:
        op = await get_operation(conn, decision["id"], "apply")
    if op["status"] == "succeeded":
        return {
            "decision_id": decision["id"],
            "application_state": "applied",
            "receipt": op["receipt"],
        }
    if op["status"] in {"unknown", "processing"}:
        # Read the domain receipt before any attempt to claim another operation.
        result = await adapter.reconcile_operation(decision, op)
        async with db.immediate() as conn:
            await adapter.snapshot(source, conn=conn)
            await db.expire_ask_operation(op["id"], now=time.time(), conn=conn)
            current = await get_operation(conn, decision["id"], "apply", lock=True)
            if current["status"] == "unknown" and result.state in {"succeeded", "failed"}:
                await db.reconcile_ask_operation(
                    op["id"],
                    current["lease_generation"],
                    state=result.state,
                    receipt=result.receipt,
                    conn=conn,
                )
                if result.state == "succeeded":
                    await finish_on(db, decision, result.receipt, conn=conn)
                else:
                    await db.update_ask_projection(
                        decision["ask_id"],
                        {
                            "application_state": "failed",
                            "next_actor": "agent",
                            "updated_at": time.time(),
                        },
                        conn=conn,
                    )
                return {
                    "decision_id": decision["id"],
                    "application_state": "applied" if result.state == "succeeded" else "failed",
                    "receipt": result.receipt,
                }
        return {
            "decision_id": decision["id"],
            "application_state": "unknown",
            "receipt": result.receipt,
        }
    if op["status"] != "pending":
        return {
            "decision_id": decision["id"],
            "application_state": "failed",
            "receipt": op["receipt"],
        }
    async with db.immediate() as conn:
        snapshot = await adapter.snapshot(source, conn=conn)
        await authorize_source(
            handler,
            source,
            DecisionActor(**decision["packet"]["actor"]),
            conn=conn,
            application=True,
        )
        if snapshot.generation_token != decision["packet"]["domain"]["meaning"]:
            raise AskError("ask.stale_source", "Source meaning changed before application")
        if await project_paused(db, source.project_id, conn=conn):
            return {
                "decision_id": decision["id"],
                "application_state": "applying",
                "reason": "Project is paused; decision saved without resuming work",
            }
        claimed = await db.claim_ask_operation(
            op["id"], owner, now=time.time(), lease_seconds=120, conn=conn
        )
    if not claimed:
        return {"decision_id": decision["id"], "application_state": "applying"}
    try:
        result = await adapter.start_operation(decision, claimed)
    except Exception:
        logger.exception("Source application needs reconciliation for %s", decision["id"])
        result = OperationResult("unknown", {"reason": "Source application needs reconciliation"})
    async with db.immediate() as conn:
        await adapter.snapshot(source, conn=conn)
        completed = await db.complete_ask_operation(
            op["id"],
            owner,
            claimed["lease_generation"],
            state=result.state,
            receipt=result.receipt,
            now=time.time(),
            conn=conn,
        )
        if completed and result.state == "succeeded":
            await finish_on(db, decision, result.receipt, conn=conn)
        elif completed:
            await db.update_ask_projection(
                decision["ask_id"],
                {
                    "application_state": result.state,
                    "next_actor": "agent",
                    "updated_at": time.time(),
                },
                conn=conn,
            )
    return {
        "decision_id": decision["id"],
        "application_state": "applied"
        if completed and result.state == "succeeded"
        else result.state
        if completed
        else "unknown",
        "receipt": result.receipt,
    }
