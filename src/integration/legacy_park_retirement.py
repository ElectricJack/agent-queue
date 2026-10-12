"""Local abandonment of fenced, inactive historical publisher journal entries."""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import cast, insert, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB

from src.database import tables as t
from src.database.queries.integration_state_queries import (
    session_attached_clause,
    unresolved_claim_clause,
)
from src.integration.engine import RootEngineOwnership


class LegacyParkRetirement:
    def __init__(self, db):
        self.db = db

    async def _inactive_on(self, conn, project_id):
        task_ids = select(t.tasks.c.id).where(t.tasks.c.project_id == project_id)
        repositories = select(t.repos.c.id).where(t.repos.c.project_id == project_id)
        batches = select(t.integration_batches.c.id).where(
            t.integration_batches.c.project_id == project_id,
        )
        operation = t.integration_repair_operations
        guards = (
            select(t.sessions.c.id).where(
                t.sessions.c.project_id == project_id,
                t.sessions.c.task_id.is_not(None),
                session_attached_clause(),
            ),
            select(t.workspaces.c.id).where(
                t.workspaces.c.project_id == project_id,
                or_(t.workspaces.c.locked_by_task_id.is_not(None),
                    t.workspaces.c.locked_by_agent_id.is_not(None)),
            ),
            select(t.task_metadata.c.task_id).where(
                t.task_metadata.c.task_id.in_(task_ids),
                t.task_metadata.c.key == "claimed_by_session",
                unresolved_claim_clause(),
            ),
            select(t.integration_branch_owners.c.id).where(
                t.integration_branch_owners.c.repository_id.in_(repositories),
                t.integration_branch_owners.c.handoff_state != "released",
                or_(t.integration_branch_owners.c.holder.is_not(None),
                    t.integration_branch_owners.c.session_id.is_not(None),
                    t.integration_branch_owners.c.workspace_id.is_not(None),
                    t.integration_branch_owners.c.handoff_state.in_(
                        ("attached", "handoff_pending"))),
            ),
            select(t.integration_subjects.c.id).where(
                t.integration_subjects.c.project_id == project_id,
                t.integration_subjects.c.phase != "done",
            ),
            select(t.integration_candidate_ref_mutations.c.id).where(
                t.integration_candidate_ref_mutations.c.repository_id.in_(repositories),
                t.integration_candidate_ref_mutations.c.state == "reserved",
            ),
            select(t.integration_promotion_intents.c.id).where(
                t.integration_promotion_intents.c.repository_id.in_(repositories),
                t.integration_promotion_intents.c.state.not_in(("committed", "conflict", "superseded")),
            ),
            select(t.project_integration_leases.c.batch_id).where(
                t.project_integration_leases.c.project_id == project_id,
                t.project_integration_leases.c.expires_at > time.time(),
            ),
            select(t.integration_batches.c.id).where(
                t.integration_batches.c.project_id == project_id,
                t.integration_batches.c.lifecycle.not_in(
                    ("promoted", "empty", "failed", "aborted")),
            ),
            select(operation.c.id).where(
                or_(operation.c.batch_id.in_(batches), operation.c.parent_task_id.in_(task_ids)),
                operation.c.state.not_in(("completed", "cancelled")),
            ),
        )
        names = ("session", "workspace", "claim", "branch writer", "subject", "ref mutation",
                 "promotion intent", "integration lease", "batch", "repair operation")
        for name, guard in zip(names, guards, strict=True):
            if await conn.scalar(guard.limit(1)):
                raise ValueError(f"project still has active integration authority: {name}")

    async def run(self, request):
        project = await self.db.get_project(request.project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated integration repository")
        async with RootEngineOwnership(self.db).exclusion(project.integration_repository_id) as conn:
            await self.db.lock_hierarchy_project(conn, request.project_id)
            current_repo = await conn.scalar(select(t.projects.c.integration_repository_id).where(
                t.projects.c.id == request.project_id).with_for_update())
            if (current_repo != project.integration_repository_id or not await conn.scalar(
                    select(t.repos.c.id).where(t.repos.c.id == current_repo,
                                             t.repos.c.project_id == request.project_id))):
                raise ValueError("designated project repository changed or belongs to another project")
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"),
                               {"id": "development-operation:" + request.operation_id})
            event = (await conn.execute(select(t.events.c.id, t.events.c.payload).where(
                t.events.c.event_type == "development.operation",
                t.events.c.project_id == request.project_id,
                cast(t.events.c.payload, JSONB)["id"].as_string() == request.operation_id,
            ).order_by(t.events.c.id.desc()).limit(1))).mappings().one_or_none()
            if event is None:
                raise ValueError("historical operation does not exist in the selected project")
            row = json.loads(event["payload"])
            digest = hashlib.sha256(event["payload"].encode()).hexdigest()
            manifest = row.get("manifest")
            if (row.get("project_id") != request.project_id or not isinstance(manifest, list)
                    or not manifest or any(not isinstance(member, dict)
                        or not isinstance(member.get("task_id"), str) for member in manifest)):
                raise ValueError("historical manifest is malformed or belongs to another project")
            named = sorted({member["task_id"] for member in manifest})
            if sorted(request.task_ids) != named:
                raise ValueError("explicit task ids must match the entire historical manifest")
            decision = (row.get("evidence") or {}).get("operator_retirement")
            replay = (row.get("state") == "cancelled" and isinstance(decision, dict)
                      and decision.get("event_id") == request.expected_event_id
                      and decision.get("sha256") == request.expected_sha256
                      and decision.get("task_ids") == named
                      and decision.get("reason") == request.reason)
            result = {"project_id": request.project_id, "operation_id": request.operation_id,
                      "task_ids": named, "event_id": event["id"], "sha256": digest,
                      "dry_run": request.dry_run}
            if replay:
                return {**result, "outcome": "retired"}
            if row.get("state") != "parked":
                raise ValueError("only a parked historical operation may be retired")
            if ((request.expected_event_id is not None and request.expected_event_id != event["id"])
                    or (request.expected_sha256 is not None and request.expected_sha256 != digest)):
                raise ValueError("historical operation changed since preview")
            live = (await conn.execute(select(t.tasks.c.id, t.tasks.c.project_id, t.tasks.c.status,
                                             t.tasks.c.assigned_agent_id).where(
                t.tasks.c.id.in_(named)).with_for_update())).mappings().all()
            archived = (await conn.execute(select(t.archived_tasks.c.id,
                t.archived_tasks.c.project_id, t.archived_tasks.c.status).where(
                t.archived_tasks.c.id.in_(named)))).mappings().all()
            sources = {source["id"]: source for source in [*archived, *live]}
            if (set(sources) != set(named) or any(source["project_id"] != request.project_id
                    or source["status"] not in ("COMPLETED", "FAILED")
                    or source.get("assigned_agent_id") for source in sources.values())):
                raise ValueError("every explicitly abandoned source must be terminal in this project")
            await self._inactive_on(conn, request.project_id)
            if request.dry_run:
                return {**result, "outcome": "preview"}
            now = time.time()
            evidence = {**(row.get("evidence") or {}), "operator_retirement": {
                "event_id": event["id"], "sha256": digest, "task_ids": named,
                "reason": request.reason, "actor": "local:operator", "at": now,
                "disposition": "abandoned",
            }}
            revised = {**row, "state": "cancelled", "evidence": evidence, "updated_at": now}
            await conn.execute(insert(t.events).values(
                event_type="development.operation", project_id=request.project_id,
                payload=json.dumps(revised), timestamp=now,
            ))
            await self.db.log_event("integration.legacy_park_retired", project_id=request.project_id,
                                   payload=json.dumps(evidence["operator_retirement"]), conn=conn)
            return {**result, "outcome": "retired"}
