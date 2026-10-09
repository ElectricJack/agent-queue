"""Command-owned recovery of conclusively failed root PR checks."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    archived_tasks,
    integration_source_ci,
    messages,
    projects,
    task_branch_origins,
    task_context,
    task_metadata,
    tasks,
)
from src.integration.checks import ChecksResult, ChecksState, Conclusion
from src.integration.delivery_truth import DeliveryRequest, load_delivery_requests
from src.models import TaskStatus

RECOVERY_KEY = "root_pr_check_recovery"


@dataclass(frozen=True)
class RootPRCheckFailure:
    """Facts from the admission gate's authenticated observation, never tool input."""

    request: DeliveryRequest
    source_sha: str
    source_base_sha: str
    pr_url: str
    policy_generation: int
    policy: dict
    checks: ChecksResult

    def feedback(self) -> str:
        lines = [f"Required PR checks failed on {self.source_sha}: {self.pr_url}"]
        for check in self.checks.checks:
            if check.conclusion is not Conclusion.FAILURE:
                continue
            lines.append(f"- {check.name}: failure {check.run_url or ''}".rstrip())
            for test_id in check.detail.get("failing_test_ids", ()):
                lines.append(f"  FAILED {test_id}")
        lines.append("Fix the reported failures on your assigned branch, run focused and "
                     "relevant area checks, publish, and close with verification evidence.")
        return "\n".join(lines)


async def recover_root_pr_checks(handler, observation: RootPRCheckFailure) -> dict:
    """Reopen with feedback atomically, or name the supervisor's recovery blocker."""
    if (not isinstance(observation, RootPRCheckFailure)
            or observation.checks.state is not ChecksState.RED
            or observation.checks.sha != observation.source_sha
            or observation.checks.repository_id != observation.request.repository_id
            or not any(check.conclusion is Conclusion.FAILURE
                       for check in observation.checks.checks)):
        return {"success": False, "outcome": "invalid"}
    db, request = handler.db, observation.request
    feedback = observation.feedback()
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, request.project_id)
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == request.task_id,
        ).with_for_update())).mappings().one_or_none()
        current = (await load_delivery_requests(db, [request.task_id],
            repository_id=request.repository_id, target_ref=request.target_ref,
            conn=conn, reduced=True)).get(request.task_id)
        project = (await conn.execute(select(projects).where(
            projects.c.id == request.project_id,
        ).with_for_update())).mappings().one_or_none()
        origin = await conn.scalar(select(task_branch_origins.c.id).where(
            task_branch_origins.c.task_id == request.task_id,
            task_branch_origins.c.repository_id == request.repository_id,
            task_branch_origins.c.branch_name == request.branch_name,
            task_branch_origins.c.base_sha == observation.source_base_sha,
            task_branch_origins.c.retired_at.is_(None),
        ).limit(1))
        if (task is None or current != request or task["status"] != "COMPLETED"
                or task["parent_task_id"] is not None or task["pr_url"] != observation.pr_url
                or project is None or not origin
                or project["hierarchical_integration_generation"] != observation.policy_generation
                or (project["hierarchical_integration_policy"] or {}) != observation.policy):
            return {"success": True, "outcome": "stale"}
        if request.reported_source != observation.source_sha:
            # The member window can predate a new close. Never bind its old
            # failure to the new completion loaded by the admission gate.
            return {"success": True, "outcome": "stale"}

        # Keep a separately allocated repair's source available for paired admission.
        repair = await conn.scalar(select(integration_source_ci.c.repair_task_id).where(
            integration_source_ci.c.task_id == request.task_id,
            integration_source_ci.c.repository_id == request.repository_id,
            integration_source_ci.c.source_head == observation.source_sha,
            integration_source_ci.c.source_base == observation.source_base_sha,
            integration_source_ci.c.repair_task_id.is_not(None),
        ).limit(1))
        if repair:
            return {"success": True, "outcome": "already_repairing", "repair_task_id": repair}

        stored = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == request.task_id, task_metadata.c.key == RECOVERY_KEY,
        ))
        history = json.loads(stored) if stored else {}
        heads = list(history.get("heads", ()))
        policy = (observation.policy.get("root") or {}).get("repair") or {}
        limit = policy.get("primary_attempts", 3)
        container = False
        for table in (tasks, archived_tasks):
            if await conn.scalar(select(table.c.id).where(
                    table.c.parent_task_id == request.task_id,
            ).limit(1)):
                container = True
                break
        reason = ("root_pr_checks_need_repair" if container else
                  "root_pr_checks_unchanged" if observation.source_sha in heads else
                  "root_pr_checks_recovery_exhausted" if len(heads) >= limit
                  and policy.get("on_exhausted", "human") != "continue" else None)
        if reason:
            identity = hashlib.sha256(repr((request.task_id, request.completion_id,
                observation.source_sha, reason)).encode()).hexdigest()
            await conn.execute(pg_insert(messages).values(
                id=f"root-pr-checks-{identity}", project_id=request.project_id,
                from_kind="system", from_id="integration-admission", to_kind="session",
                to_id=f"supervisor-{request.project_id}",
                subject=f"Root PR checks require repair: {request.task_id}",
                body=f"{reason}; automatic reopen attempts: {len(heads)}/{limit}.\n{feedback}",
                body_kind="integration_root_pr_checks", created_at=time.time(), priority=50,
                archive_after_inject=1,
            ).on_conflict_do_nothing(index_elements=[messages.c.id]))
            await db._upsert_meta(request.task_id, RECOVERY_KEY,
                {**history, "blocker": reason, "source_sha": observation.source_sha}, conn=conn)
            return {"success": True, "outcome": "supervisor_notified", "blocker": reason}

        transition = await db._apply_transition(conn, request.task_id, TaskStatus.READY,
            context="reopen_with_feedback", assigned_agent_id=None, pr_url=None, retry_count=0,
            description=task["description"] + "\n\n---\n**Reopen Feedback:**\n" + feedback)
        await db._upsert_meta(request.task_id, RECOVERY_KEY,
            {"heads": [*heads, observation.source_sha], "attempts": len(heads) + 1}, conn=conn)
        context_id = hashlib.sha256(repr((request.task_id, request.completion_id,
            observation.source_sha)).encode()).hexdigest()
        await conn.execute(insert(task_context).values(
            id=f"root-pr-feedback-{context_id}", task_id=request.task_id,
            type="reopen_feedback", label="Root PR check failure", content=feedback,
            created_at=time.time(),
        ))
        await db.add_task_comment(request.task_id, feedback, author_kind="agent",
            author_id="service:integration-admission", conn=conn)
        await db.log_event("integration.root_pr_checks_reopened", project_id=request.project_id,
            task_id=request.task_id, payload=json.dumps({"source_sha": observation.source_sha,
                "completion_id": request.completion_id, "attempt": len(heads) + 1}), conn=conn)
    await db.log_blocked_flips(transition.flipped)
    await db._notify_settled(transition.settled)
    await db._notify_ready(transition.ready)
    return {"success": True, "outcome": "reopened", "task_id": request.task_id}
