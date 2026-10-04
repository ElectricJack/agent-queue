"""PR retirement after settlement, periodic delivery proof, and orphan inventory."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime

from sqlalchemy import or_, select

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_repair_operations,
    integration_subjects,
    projects,
    sessions,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.delivery_branches import ACTIVE_BATCH_LIFECYCLES, branch_of
from src.integration.live_operations import ACTIVE_OPERATION_STATES

logger = logging.getLogger(__name__)
SWEEP_PRINCIPAL = "integration-pr-cleanup"
ORPHAN_AGE_SECONDS = 24 * 60 * 60
SETTLED_PR_CLEANUP_KEY = "integration_pr_cleanup"


async def open_aq_pull_requests(db, git, project_id):
    """Inventory all pages, including untracked PRs and non-default bases."""
    project = await db.get_project(project_id)
    repo_id = getattr(project, "integration_repository_id", None)
    repo = await db.get_repo(repo_id) if repo_id else None
    if repo is None or repo.project_id != project_id or not repo.url:
        return []
    binding = await git.bind_github_repository(repo.url)
    client = git._github_client(binding)
    pulls = await client.paged_list(
        f"/repositories/{binding.repository_id}/pulls"
        "?state=open&sort=created&direction=asc&per_page=100",
        max_pages=100,
    )
    result = []
    for pull in pulls:
        head = pull.get("head") or {}
        if (
            pull.get("state") != "open"
            or not str(head.get("ref", "")).startswith("aq/")
            or (head.get("repo") or {}).get("id") != binding.repository_id
        ):
            continue
        number = IntegrationCleanupService._pr_number(pull["html_url"], binding.full_name)
        if number != pull["number"]:
            raise ValueError("PR inventory identity is inconsistent")
        result.append(
            {
                "project_id": project_id,
                "repository_id": repo.id,
                "pr_number": number,
                "pr_url": pull["html_url"],
                "branch": head["ref"],
                "head_sha": head["sha"],
                "created_at": datetime.fromisoformat(
                    pull["created_at"].replace("Z", "+00:00")
                ).timestamp(),
            }
        )
    return result


class PullRequestReconciler:
    """Bounded visits of every aq PR through the existing proof command."""

    def __init__(
        self, db, git, *, commands, interval_seconds=300.0, page_size=20, item_timeout_seconds=60.0
    ):
        if page_size <= 0 or interval_seconds <= 0 or item_timeout_seconds <= 0:
            raise ValueError("PR cleanup page size, interval and timeout must be positive")
        self.db, self.git, self.commands = db, git, commands
        self.interval_seconds, self.page_size = interval_seconds, page_size
        self.item_timeout_seconds = item_timeout_seconds
        self.next_due_at = 0.0
        self.pending = deque()
        self.projects = deque()

    async def tick(self, now):
        if now < self.next_due_at:
            return
        if not self.pending and not self.projects:
            async with self.db._engine.connect() as conn:
                self.pending.extend(
                    {"settled_task_id": task_id}
                    for task_id in (
                        await conn.execute(
                            select(task_metadata.c.task_id)
                            .where(
                                task_metadata.c.key == SETTLED_PR_CLEANUP_KEY,
                            )
                            .order_by(task_metadata.c.task_id)
                        )
                    ).scalars()
                )
                self.projects.extend(
                    (
                        await conn.execute(
                            select(projects.c.id)
                            .where(
                                projects.c.integration_repository_id.is_not(None),
                            )
                            .order_by(projects.c.id)
                        )
                    ).scalars()
                )
        if self.projects:
            project_id = self.projects[0]
            try:
                async with asyncio.timeout(self.item_timeout_seconds):
                    self.pending.extend(await open_aq_pull_requests(self.db, self.git, project_id))
            except Exception:
                logger.warning("Could not inventory PRs for %s", project_id, exc_info=True)
            self.projects.popleft()
        for _ in range(min(self.page_size, len(self.pending))):
            pull = self.pending[0]
            try:
                async with asyncio.timeout(self.item_timeout_seconds):
                    if "settled_task_id" in pull:
                        await retry_settled_task_prs(self.db, self.git, [pull["settled_task_id"]])
                    else:
                        await self.close_delivered(pull)
            except Exception:
                logger.warning("Could not retire PR %s", pull, exc_info=True)
            # Cancellation keeps this PR queued for the next visit.
            self.pending.popleft()
        if not self.pending and not self.projects:
            self.next_due_at = now + self.interval_seconds

    async def close_delivered(self, pull):
        from src.commands.principal import ExecutionPrincipal, principal_context

        handler = self.commands()
        args = {"project_id": pull["project_id"], "pr_number": pull["pr_number"]}
        with principal_context(ExecutionPrincipal.service(SWEEP_PRINCIPAL)):
            preview = await handler.execute("integration_close_delivered_pr", args)
            if preview.get("outcome") != "would_close":
                if preview.get("outcome") == "blocked" or not preview.get("outcome"):
                    logger.warning("PR proof for %s: %s", pull["pr_url"], preview)
                return preview
            result = await handler.execute(
                "integration_close_delivered_pr",
                {
                    **args,
                    "dry_run": False,
                    "expected_head_sha": preview["head_sha"],
                    "reason": "Periodic integration sweep proved delivery on the default branch",
                },
            )
            if result.get("outcome") not in {"closed", "nothing_to_close"}:
                logger.warning("PR closure for %s: %s", pull["pr_url"], result)
            return result


class SettledTaskPullRequestClosure:
    """Retire a settled task's canonical PR without claiming its content landed."""

    def __init__(self, db, git):
        self.db, self.git = db, git

    async def run(self, task_id, *, reason, principal, settlement=None):
        task = await self.db.get_task(task_id)
        if task is None or not task.pr_url:
            return {"outcome": "nothing_to_close", "task_id": task_id}
        base = {"task_id": task_id, "pr_url": task.pr_url}
        try:
            if settlement is not None and any(
                getattr(task, key) != settlement[key]
                for key in ("pr_url", "branch_name", "updated_at")
            ):
                raise ValueError("task no longer matches the queued settlement")
            project = await self.db.get_project(task.project_id)
            repo = await self.db.get_repo(project.integration_repository_id)
            if repo is None or repo.project_id != task.project_id or task.repo_id != repo.id:
                raise ValueError("task is outside the designated repository")
            binding = await self.git.bind_github_repository(repo.url)
            number = IntegrationCleanupService._pr_number(task.pr_url, binding.full_name)
            client = self.git._github_client(binding)
            pull = await client.pull_request(task.pr_url)
            head = pull.get("head") or {}
            if (
                (head.get("repo") or {}).get("id") != binding.repository_id
                or head.get("ref") != branch_of(task.branch_name)
                or not str(head.get("ref", "")).startswith("aq/")
            ):
                raise ValueError("PR does not name the task's canonical repository branch")
            if pull.get("state") == "closed":
                return {**base, "outcome": "nothing_to_close"}
            expected = head["sha"]
            marker = f"<!-- aq-settled-pr:{task_id}:{expected} -->"
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, task.project_id)
                current = (
                    (
                        await conn.execute(
                            select(tasks)
                            .where(
                                tasks.c.id == task_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                writer = await conn.scalar(
                    select(sessions.c.id)
                    .where(
                        sessions.c.task_id == task_id,
                        session_attached_clause(),
                    )
                    .limit(1)
                )
                workspace = await conn.scalar(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.locked_by_task_id == task_id,
                    )
                    .limit(1)
                )
                owner = await conn.scalar(
                    select(integration_branch_owners.c.id)
                    .where(
                        integration_branch_owners.c.repository_id == repo.id,
                        integration_branch_owners.c.ref.in_(
                            (head["ref"], "refs/heads/" + head["ref"])
                        ),
                        integration_branch_owners.c.handoff_state != "released",
                        or_(
                            integration_branch_owners.c.owner_id != task_id,
                            integration_branch_owners.c.session_id.is_not(None),
                            integration_branch_owners.c.workspace_id.is_not(None),
                            integration_branch_owners.c.handoff_state.in_(
                                ("attached", "handoff_pending")
                            ),
                        ),
                    )
                    .limit(1)
                )
                if (
                    current is None
                    or current["status"] != "COMPLETED"
                    or current["assigned_agent_id"]
                    or writer
                    or workspace
                    or owner
                    or current["pr_url"] != task.pr_url
                    or current["branch_name"] != task.branch_name
                    or current["updated_at"] != task.updated_at
                ):
                    raise ValueError("task settlement changed or a writer still holds it")
                if not await client.has_comment_marker(number=number, marker=marker):
                    await client.comment_pull_request(
                        number=number,
                        marker=marker,
                        body=f"{marker}\nClosing because task `{task_id}` was settled. {reason}",
                    )
                exact = await client.exact_pull_request(number=number)
                if exact is None:
                    return {**base, "outcome": "nothing_to_close"}
                if (
                    exact["repository_numeric_id"] != binding.repository_id
                    or exact["repository_full_name"] != binding.full_name
                    or exact["head_sha"] != expected
                ):
                    raise ValueError("PR repository or head changed before closure")
                if exact["state"] != "closed":
                    await client.close_pull_request(number=number)
                await self.db.log_event(
                    "integration.pr_closed_settled",
                    project_id=task.project_id,
                    task_id=task_id,
                    payload=json.dumps(
                        {**base, "head_sha": expected, "reason": reason, "principal": principal}
                    ),
                    conn=conn,
                )
            return {**base, "outcome": "closed"}
        except Exception as exc:
            logger.warning("Settled PR cleanup for %s failed: %s", task_id, exc)
            return {**base, "outcome": "blocked", "reason": str(exc)}


async def queue_settled_task_prs_on(db, conn, task_ids, *, reason, principal):
    """Retain failed adoption cleanup across restarts without binding a newer close."""
    rows = (
        (
            await conn.execute(
                select(tasks).where(
                    tasks.c.id.in_(task_ids),
                    tasks.c.pr_url.is_not(None),
                    tasks.c.pr_url != "",
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        await db._upsert_meta(
            row["id"],
            SETTLED_PR_CLEANUP_KEY,
            {
                "pr_url": row["pr_url"],
                "branch_name": row["branch_name"],
                "updated_at": row["updated_at"],
                "reason": reason,
                "principal": principal,
            },
            conn=conn,
        )


async def retry_settled_task_prs(db, git, task_ids=None):
    from sqlalchemy import delete

    statement = select(task_metadata).where(task_metadata.c.key == SETTLED_PR_CLEANUP_KEY)
    if task_ids is not None:
        statement = statement.where(task_metadata.c.task_id.in_(task_ids))
    async with db._engine.connect() as conn:
        rows = (await conn.execute(statement.order_by(task_metadata.c.task_id))).mappings().all()
    service = SettledTaskPullRequestClosure(db, git)
    results = []
    for row in rows:
        settlement = json.loads(row["value"])
        result = await service.run(
            row["task_id"],
            reason=settlement["reason"],
            principal=settlement["principal"],
            settlement=settlement,
        )
        results.append(result)
        if result["outcome"] in {"closed", "nothing_to_close"}:
            async with db.immediate() as conn:
                await conn.execute(
                    delete(task_metadata).where(
                        task_metadata.c.task_id == row["task_id"],
                        task_metadata.c.key == SETTLED_PR_CLEANUP_KEY,
                        task_metadata.c.value == row["value"],
                    )
                )
    return results


async def orphaned_pull_requests(db, git, project_id, *, now=None):
    """Read-only report: old open aq PRs with no task, train or writer owner."""
    now = time.time() if now is None else now
    pulls = await open_aq_pull_requests(db, git, project_id)
    if not pulls:
        return []
    repository_id = pulls[0]["repository_id"]
    branches, urls = set(), set()
    async with db._engine.connect() as conn:
        live_writer = (
            select(sessions.c.id)
            .where(
                sessions.c.task_id == tasks.c.id,
                session_attached_clause(),
            )
            .exists()
        )
        locked = (
            select(workspaces.c.id)
            .where(
                workspaces.c.locked_by_task_id == tasks.c.id,
            )
            .exists()
        )
        rows = (
            await conn.execute(
                select(tasks.c.branch_name, tasks.c.pr_url).where(
                    tasks.c.project_id == project_id,
                    or_(
                        tasks.c.status.not_in(("COMPLETED", "FAILED")),
                        tasks.c.assigned_agent_id.is_not(None),
                        live_writer,
                        locked,
                    ),
                )
            )
        ).all()
        branches.update(branch_of(row.branch_name) for row in rows)
        urls.update(row.pr_url for row in rows)
        branches.update(
            branch_of(ref)
            for ref in (
                await conn.execute(
                    select(
                        integration_branch_owners.c.ref,
                    ).where(
                        integration_branch_owners.c.repository_id == repository_id,
                        integration_branch_owners.c.handoff_state != "released",
                    )
                )
            ).scalars()
        )
        live_batches = select(integration_batches.c.id).where(
            integration_batches.c.repository_id == repository_id,
            integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES),
        )
        branches.update(
            branch_of(ref)
            for ref in (
                await conn.execute(
                    select(
                        integration_batches.c.integration_branch,
                    ).where(integration_batches.c.id.in_(live_batches))
                )
            ).scalars()
        )
        members = (
            await conn.execute(
                select(
                    integration_batch_members.c.source_ref,
                    integration_batch_members.c.pr_url,
                ).where(integration_batch_members.c.batch_id.in_(live_batches))
            )
        ).all()
        branches.update(branch_of(row.source_ref) for row in members)
        urls.update(row.pr_url for row in members)
        urls.update(
            (
                await conn.execute(
                    select(integration_candidate_publications.c.pr_url).where(
                        integration_candidate_publications.c.batch_id.in_(live_batches),
                    )
                )
            ).scalars()
        )
        subjects = (
            await conn.execute(
                select(
                    integration_subjects.c.target_ref,
                    tasks.c.branch_name,
                    tasks.c.pr_url,
                )
                .outerjoin(tasks, tasks.c.id == integration_subjects.c.task_id)
                .where(
                    integration_subjects.c.repository_id == repository_id,
                    integration_subjects.c.phase != "done",
                )
            )
        ).all()
        branches.update(branch_of(row.target_ref) for row in subjects)
        branches.update(branch_of(row.branch_name) for row in subjects)
        urls.update(row.pr_url for row in subjects)
        operations = (
            await conn.execute(
                select(tasks.c.branch_name, tasks.c.pr_url)
                .join(
                    integration_repair_operations,
                    or_(
                        tasks.c.id == integration_repair_operations.c.parent_task_id,
                        tasks.c.id == integration_repair_operations.c.verifier_task_id,
                    ),
                )
                .where(
                    tasks.c.project_id == project_id,
                    integration_repair_operations.c.state.in_(ACTIVE_OPERATION_STATES),
                )
            )
        ).all()
        branches.update(branch_of(row.branch_name) for row in operations)
        urls.update(row.pr_url for row in operations)
    return [
        {**pull, "age_seconds": now - pull["created_at"]}
        for pull in pulls
        if now - pull["created_at"] > ORPHAN_AGE_SECONDS
        and pull["branch"] not in branches
        and pull["pr_url"] not in urls
    ]
