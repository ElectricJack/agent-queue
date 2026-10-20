"""Proposal previews and durable ingestion-batch review evidence."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from src.api.auth import LOCAL_SCOPE
from src.api.models.task import IngestBatchDetail
from src.database.queries import ingest_batch_queries as batches
from src.database.queries import proposal_queries
from src.database.tables import ingest_batch_receipts, task_proposals

__all__ = ["build_proposals_router", "router"]


def build_proposals_router(*, db=None) -> APIRouter:
    router = APIRouter()

    def database():
        if db is not None:
            return db
        from src.api import dependencies as deps

        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        return orch.db

    @router.get("/api/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str) -> dict:
        row = await proposal_queries.get_proposal(database(), proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {
            "proposal_id": row["id"],
            "project_id": row["project_id"],
            "source": row["source"],
            "tasks": row["payload"].get("tasks", []),
            "edges": row["payload"].get("edges", []),
            "edits": row["payload"].get("edits", []),
            "remove_edges": row["payload"].get("remove_edges", []),
            "comments": row["payload"].get("comments", []),
            "diff": row["payload"].get("diff"),
            "receipt": row["payload"].get("receipt"),
            "status": row["status"],
        }

    @router.get(
        "/api/projects/{project_id}/batches/{batch_id}", response_model=IngestBatchDetail,
        operation_id="get_ingest_batch",
    )
    async def get_ingest_batch(
        project_id: str, batch_id: str, request: Request,
    ) -> IngestBatchDetail:
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        if scope.kind == "session" and (
            scope.project_id != project_id
            and not (scope.elevated and scope.project_id is None)
        ):
            raise HTTPException(status_code=403, detail="out of scope: project mismatch")
        # Repeatable read keeps revision, membership and receipts on one snapshot
        # while a reviewer edits or a release commits concurrently.
        async with database()._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                batch = await batches.get_batch(conn, batch_id)
                if batch is None or batch["project_id"] != project_id:
                    raise HTTPException(status_code=404, detail="batch not found")
                proposal = (await conn.execute(select(
                    task_proposals.c.source, task_proposals.c.payload,
                ).where(task_proposals.c.id == batch_id))).mappings().one()
                membership = await batches.members(conn, batch_id)
                receipts = [dict(row) for row in (await conn.execute(
                    select(ingest_batch_receipts)
                    .where(ingest_batch_receipts.c.batch_id == batch_id)
                    .order_by(ingest_batch_receipts.c.created_at,
                              ingest_batch_receipts.c.receipt_id)
                )).mappings()]
        current = next((receipt for receipt in reversed(receipts) if (
            receipt["structure_revision"] == batch["structure_revision"]
            and receipt["structure_digest"] == batch["structure_digest"]
        )), {})
        findings = current.get("structural_findings", [])
        if isinstance(findings, dict):
            findings = findings.get("findings", [])
        checks = batch["check_results"] or {}
        return IngestBatchDetail(
            **batches.batch_summary({**batch, "source": proposal["source"]}),
            **{key: batch[key] for key in (
                "project_id", "structure_digest", "source_review_id", "source_revision",
                "source_content_sha256", "decision_id", "check_results", "release_receipt",
                "release_actor", "release_revision", "release_digest", "released_at",
            )},
            members={m["member_key"]: m["task_id"] for m in membership if m["state"] == "active"},
            membership=membership,
            section_verdicts=current.get("section_verdicts", {}),
            reverse_coverage_verdict=current.get("reverse_coverage_verdict"),
            walkthrough_verdict=current.get("walkthrough_verdict"),
            structural_findings=findings,
            diff=checks.get("last_diff", json.loads(proposal["payload"]).get("diff")),
            receipts=receipts,
        )

    return router


router = build_proposals_router()
