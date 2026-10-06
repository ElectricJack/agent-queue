"""Open the pull request a completed train root still lacks.

The train freezes a root only with an open exact-head PR, green PR checks and
the boundary's required human reviews. Two paths open that PR when the root
completes: ``EpicCompletions.settle`` for an epic and session close for a
childless root. Both are best-effort: a GitHub
failure must not undo a committed completion.

:class:`RootPullRequestReconciler` is their retry.  Every interval it pages
through completed train roots that have no PR and a collected completion or
legacy finished checkpoint, and opens it through ``EpicPullRequestService``. A root it
cannot open (GitHub unavailable, head already on the default branch) is
deferred with exponential backoff, so it costs one read per backoff window.

:class:`RootDeliveryRedrive` is the dry-run-first control for one stuck root
(``aq integration redrive-root``).  The dry run reports what the root's
delivery waits on -- its checkpoint, the remote branch tip, whether the head
is already on the default branch -- and ``--apply`` opens the PR for exactly
the head the dry run reported.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update

from src.database.queries.integration_train_queries import _root_delivery_receipt_conditions
from src.database.tables import (
    archived_tasks,
    integration_branch_owners,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.logging_config import log_handled
from src.models import TaskStatus
from src.projects.github import GitHubError
from src.task_graph.integration_dependencies import dependencies_for

_OID = re.compile(r"[0-9a-f]{40}")

logger = logging.getLogger(__name__)

#: ``open_for_epic`` outcomes after which the root has its PR.
_PR_PRESENT = frozenset({"opened", "already_open"})

#: Event written once per applied redrive.
REDRIVE_EVENT = "integration.root_redriven"


def _has_children(task_id_column):
    child = tasks.alias("root_pr_child")
    archived_child = archived_tasks.alias("root_pr_archived_child")
    return or_(
        exists(select(child.c.id).where(child.c.parent_task_id == task_id_column)),
        exists(
            select(archived_child.c.id).where(archived_child.c.parent_task_id == task_id_column)
        ),
    )


def pr_ready_roots_statement(*, after_id: str | None, limit: int):
    """Completed train roots with no PR and a completion or finished checkpoint.

    A childless root is ready once its leaf checkpoint moved past its origin
    base; an epic once its aggregate verification completed.  A root already
    delivered to the default branch is never selected.
    """
    checkpoint = task_integration_checkpoints
    origin = task_branch_origins
    has_children = _has_children(tasks.c.id)
    leaf_ready = and_(
        ~has_children,
        checkpoint.c.checkpoint_sha.is_not(None),
        checkpoint.c.episode_id.is_(None),
        checkpoint.c.current_verification_id.is_(None),
        checkpoint.c.checkpoint_sha != origin.c.base_sha,
    )
    parent_ready = and_(
        has_children,
        checkpoint.c.last_completed_verification_id.is_not(None),
        checkpoint.c.current_verification_id == checkpoint.c.last_completed_verification_id,
        checkpoint.c.verified_sha == checkpoint.c.checkpoint_sha,
    )
    delivered = exists(
        select(task_delivery_receipts.c.id).where(
            task_delivery_receipts.c.source_task_id == tasks.c.id,
            *_root_delivery_receipt_conditions(tasks.c.repo_id, repos.c.default_branch),
        )
    )
    statement = (
        select(tasks.c.id)
        .select_from(
            tasks.join(projects, projects.c.id == tasks.c.project_id)
            .join(
                repos,
                and_(repos.c.id == tasks.c.repo_id, repos.c.project_id == tasks.c.project_id),
            )
            .outerjoin(
                checkpoint,
                and_(
                    checkpoint.c.task_id == tasks.c.id,
                    checkpoint.c.repository_id == tasks.c.repo_id,
                    checkpoint.c.branch == tasks.c.branch_name,
                ),
            )
            .join(
                origin,
                and_(
                    origin.c.task_id == tasks.c.id,
                    origin.c.repository_id == tasks.c.repo_id,
                    origin.c.retired_at.is_(None),
                ),
            )
        )
        .where(
            projects.c.hierarchical_integration_mode == "train",
            projects.c.integration_repository_id == tasks.c.repo_id,
            tasks.c.parent_task_id.is_(None),
            tasks.c.status == TaskStatus.COMPLETED.value,
            or_(tasks.c.pr_url.is_(None), func.trim(tasks.c.pr_url) == ""),
            tasks.c.branch_name.is_not(None),
            or_(leaf_ready, parent_ready, exists(select(task_completion_records.c.id).where(
                task_completion_records.c.task_id == tasks.c.id,
                task_completion_records.c.id.like("epic-%"),
                task_completion_records.c.outcome == "pass",
            ))),
            ~delivered,
        )
        .order_by(tasks.c.id)
        .limit(limit)
    )
    if after_id is not None:
        statement = statement.where(tasks.c.id > after_id)
    return statement


class RootPullRequestReconciler:
    """Retry the PR a completed train root's completion path failed to open."""

    def __init__(
        self,
        db: Any,
        git_manager: Any,
        *,
        interval_seconds: float = 60.0,
        page_size: int = 20,
        max_backoff_seconds: float = 3600.0,
    ) -> None:
        if interval_seconds <= 0 or page_size <= 0 or max_backoff_seconds <= 0:
            raise ValueError("root PR reconciler interval, page and backoff must be positive")
        self.db = db
        self.git = git_manager
        self.interval_seconds = interval_seconds
        self.page_size = page_size
        self.max_backoff_seconds = max_backoff_seconds
        self.next_due_at = 0.0
        self.after_id: str | None = None
        #: task id -> (consecutive misses, retry not before)
        self._deferred: dict[str, tuple[int, float]] = {}

    async def tick(self, now: float) -> None:
        if now < self.next_due_at:
            return
        self.next_due_at = now + self.interval_seconds
        rows = await self._page(self.after_id)
        if not rows and self.after_id is not None:
            self.after_id = None
            rows = await self._page(None)
        for task_id in rows:
            self.after_id = task_id
            await self.open_one(task_id, now)

    async def _page(self, after_id: str | None) -> list[str]:
        async with self.db._engine.connect() as conn:
            result = await conn.execute(
                pr_ready_roots_statement(after_id=after_id, limit=self.page_size)
            )
            return list(result.scalars())

    async def open_one(self, task_id: str, now: float) -> dict[str, Any] | None:
        deferred = self._deferred.get(task_id)
        if deferred is not None and deferred[1] > now:
            return None
        try:
            result = await EpicPullRequestService(
                self.db, git_manager=self.git
            ).open_for_epic(task_id)
        except (GitError, GitHubError, GitHubAccessError) as exc:
            log_handled(logger, logging.WARNING,
                        "Could not open the pull request for train root %s", task_id, exc=exc)
            self._defer(task_id, now)
            return None
        except Exception:
            logger.warning(
                "Could not open the pull request for train root %s", task_id, exc_info=True
            )
            self._defer(task_id, now)
            return None
        if result["outcome"] in _PR_PRESENT:
            self._deferred.pop(task_id, None)
            logger.info(
                "Train root %s pull request %s: %s",
                task_id,
                result["outcome"],
                result.get("pr_url"),
            )
        else:
            logger.info("Train root %s pull request not opened: %s", task_id, result["outcome"])
            self._defer(task_id, now)
        return result

    def _defer(self, task_id: str, now: float) -> None:
        misses = self._deferred.get(task_id, (0, 0.0))[0] + 1
        # The first miss already skips one due tick; each further miss doubles.
        delay = min(self.interval_seconds * 2 ** misses, self.max_backoff_seconds)
        self._deferred[task_id] = (misses, now + delay)


class RootDeliveryRedrive:
    """Diagnose one completed train root and re-drive its missing PR."""

    def __init__(self, db: Any, git_manager: Any, *, clock=time.time) -> None:
        self.db = db
        self.git = git_manager
        self.clock = clock

    async def run(
        self,
        task_id: str,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        diagnosis = await self.diagnose(task_id)
        if diagnosis["outcome"] == "would_collect":
            from src.integration.collecting_parent_recovery import CollectingParentRecovery

            return await CollectingParentRecovery(self.db, clock=self.clock).run(
                task_id, dry_run=dry_run, expected_head_sha=expected_head_sha,
                reason=reason, operator_id=operator_id,
            )
        if dry_run or diagnosis["outcome"] != "would_open":
            return diagnosis
        if diagnosis.get("head_sha") != expected_head_sha:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "the root's head is not the one the dry run reported; "
                "run the dry run again",
            }
        try:
            opened = await EpicPullRequestService(
                self.db, git_manager=self.git
            ).open_for_epic(task_id)
        except Exception as exc:
            logger.warning("Redrive could not open the pull request for %s", task_id, exc_info=True)
            return {**diagnosis, "outcome": "blocked", "reason": f"pull request creation failed: {exc}"}
        if opened["outcome"] == "opened":
            outcome = "opened"
        elif opened["outcome"] in {"already_open", "already_on_default"}:
            outcome = "nothing_to_redrive"
        else:
            outcome = "blocked"
        result = {
            **diagnosis,
            "outcome": outcome,
            "pr_url": opened.get("pr_url"),
            "reason": opened["outcome"],
        }
        await self.db.log_event(
            REDRIVE_EVENT,
            project_id=diagnosis.get("project_id"),
            task_id=task_id,
            payload=json.dumps(
                {
                    "operator_id": operator_id,
                    "reason": reason,
                    "head_sha": diagnosis.get("head_sha"),
                    "outcome": outcome,
                    "pull_request_outcome": opened["outcome"],
                    "pr_url": opened.get("pr_url"),
                    "at": self.clock(),
                }
            ),
        )
        return result

    async def diagnose(self, task_id: str) -> dict[str, Any]:
        """Report what the root's delivery waits on; reads only."""
        async with self.db._engine.connect() as conn:
            task = (
                await conn.execute(select(tasks).where(tasks.c.id == task_id))
            ).mappings().one_or_none()
            if task is None:
                return {"outcome": "not_found", "task_id": task_id}
            base = {"task_id": task_id, "project_id": task["project_id"]}
            project = (
                await conn.execute(select(projects).where(projects.c.id == task["project_id"]))
            ).mappings().one()
            if project["hierarchical_integration_mode"] != "train":
                return {**base, "outcome": "not_eligible",
                        "reason": "the project is not in train mode"}
            if task["parent_task_id"] is not None:
                return {**base, "outcome": "not_eligible",
                        "reason": "not a root: its parent collects it"}
            if task["status"] == TaskStatus.BLOCKED.value:
                from src.integration.collecting_parent_recovery import CollectingParentRecovery

                return await CollectingParentRecovery(self.db, clock=self.clock).diagnose(task_id)
            if task["status"] != TaskStatus.COMPLETED.value:
                return {**base, "outcome": "not_eligible",
                        "reason": f"the root is {task['status']}, not COMPLETED"}
            base["branch"] = task["branch_name"]
            if task["pr_url"] and task["pr_url"].strip():
                return {**base, "outcome": "nothing_to_redrive", "pr_url": task["pr_url"],
                        "reason": "the root already has its pull request"}
            checkpoint = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            ).mappings().one_or_none()
            if checkpoint is None:
                return {**base, "outcome": "not_eligible",
                        "reason": "no integration checkpoint: the legacy completion "
                        "pipeline owns this root's pull request"}
            origin = (
                await conn.execute(
                    select(task_branch_origins.c.base_sha).where(
                        task_branch_origins.c.task_id == task_id,
                        task_branch_origins.c.repository_id == task["repo_id"],
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings().one_or_none()
            owner = (
                await conn.execute(
                    select(
                        integration_branch_owners.c.id,
                        integration_branch_owners.c.owner_id,
                        integration_branch_owners.c.owner_role,
                        integration_branch_owners.c.handoff_state,
                    ).where(
                        integration_branch_owners.c.repository_id == task["repo_id"],
                        integration_branch_owners.c.ref == task["branch_name"],
                    )
                )
            ).mappings().one_or_none()
            children = (
                await conn.execute(select(_has_children(task_id)))
            ).scalar_one()
            delivered = (
                await conn.execute(
                    select(task_delivery_receipts.c.id)
                    .select_from(
                        task_delivery_receipts.join(
                            repos, repos.c.id == task_delivery_receipts.c.repository_id
                        )
                    )
                    .where(
                        task_delivery_receipts.c.source_task_id == task_id,
                        *_root_delivery_receipt_conditions(
                            task["repo_id"], repos.c.default_branch
                        ),
                    )
                    .limit(1)
                )
            ).first()
            repository = (
                await conn.execute(
                    select(repos.c.url, repos.c.default_branch).where(
                        repos.c.id == task["repo_id"],
                        repos.c.id == project["integration_repository_id"],
                    )
                )
            ).mappings().one_or_none()

        base.update(
            kind="parent" if children else "leaf",
            base_sha=origin["base_sha"] if origin is not None else None,
            checkpoint={
                key: checkpoint[key]
                for key in (
                    "state", "generation", "checkpoint_sha", "verified_sha",
                    "verified_generation", "episode_id", "current_verification_id",
                    "last_completed_verification_id",
                )
            },
            owner=dict(owner) if owner is not None else None,
        )
        if delivered is not None:
            return {**base, "outcome": "nothing_to_redrive",
                    "reason": "the root is already delivered to the default branch"}
        if children:
            if (
                checkpoint["last_completed_verification_id"] is None
                or checkpoint["current_verification_id"]
                != checkpoint["last_completed_verification_id"]
                or checkpoint["verified_sha"] != checkpoint["checkpoint_sha"]
            ):
                return {**base, "outcome": "blocked",
                        "reason": "the epic's aggregate verification has not completed; "
                        "its parent integration run completes it"}
            head = checkpoint["verified_sha"]
        else:
            source = leaf_root_source(
                checkpoint, origin, branch=task["branch_name"], repo_id=task["repo_id"]
            )
            if source == "no_changes":
                return {**base, "outcome": "blocked",
                        "reason": "the leaf checkpoint is still the origin base: "
                        "the close recorded no finished head"}
            if source != "ready":
                return {**base, "outcome": "blocked",
                        "reason": "the leaf checkpoint does not name a finished head "
                        "on the root's own branch"}
            head = checkpoint["checkpoint_sha"]
        base["head_sha"] = head
        if repository is None or not repository["url"]:
            return {**base, "outcome": "blocked",
                    "reason": "the root is not in the project's integration repository"}

        try:
            binding = await self.git.bind_github_repository(repository["url"])
            remote_head = await self.git.aremote_branch_head(
                repository=binding, branch=task["branch_name"]
            )
            ahead = await self.git.acommits_ahead_of_base(
                repository=binding, base=repository["default_branch"], head_sha=head
            )
        except (GitError, ValueError) as exc:
            return {**base, "outcome": "blocked",
                    "reason": f"could not read the repository: {exc}"}
        base.update(remote_head_sha=remote_head, ahead_by=ahead)
        if remote_head is None:
            return {**base, "outcome": "blocked",
                    "reason": "the root's branch is not published on the repository"}
        if remote_head != head:
            return {**base, "outcome": "blocked",
                    "reason": "the remote branch moved from the recorded head; "
                    "the pull request would propose an unreviewed head"}
        if ahead == 0:
            return {**base, "outcome": "nothing_to_redrive",
                    "reason": "the head is already on the default branch"}
        return {**base, "outcome": "would_open",
                "reason": "the root is complete with a published head and no pull request"}


def leaf_root_source(checkpoint, origin, *, branch: str | None, repo_id: str | None) -> str:
    """Classify a childless root's checkpoint as a PR source.

    Returns ``ready`` when the checkpoint names a finished leaf head on the
    root's own branch (the shape ``eligible_root_page_on`` seats as a leaf),
    ``no_checkpoint`` when the root is outside hierarchical delivery (the legacy
    completion pipeline owns its PR), ``no_changes`` when the head is still
    the origin base, and ``checkpoint_not_ready`` otherwise.
    """
    if checkpoint is None:
        return "no_checkpoint"
    if (
        origin is None
        or checkpoint["branch"] != branch
        or checkpoint["repository_id"] != repo_id
        or not checkpoint["checkpoint_sha"]
        or not _OID.fullmatch(checkpoint["checkpoint_sha"])
        or checkpoint["episode_id"] is not None
        or checkpoint["current_verification_id"] is not None
    ):
        return "checkpoint_not_ready"
    if checkpoint["checkpoint_sha"] == origin["base_sha"]:
        return "no_changes"
    return "ready"


def render_body(
    *,
    epic_id: str,
    epic_title: str,
    children: list[dict],
    dependencies: list[dict],
) -> str:
    """Describe the epic and carry its stable identity in a GitHub trailer.

    A childless root has no children section.
    """
    lines = [epic_title]
    if children:
        lines.extend(("", "## Children", ""))
        lines.extend(f"- `{child['id']}` {child['title']}" for child in children)
    if dependencies:
        lines.extend(("", "## Depends on", ""))
        lines.extend(f"- `{dependency['id']}` {dependency['title']}" for dependency in dependencies)
    lines.extend(("", f"AQ-Epic: {epic_id}"))
    return "\n".join(lines)


class EpicPullRequestService:
    """Create one GitHub pull request and retain its URL on the root task."""

    def __init__(self, db, *, git_manager, clock=time.time) -> None:
        self._db = db
        self._git = git_manager
        self._clock = clock

    async def open_for_epic(self, epic_id: str, *,
                            expected_head_sha: str | None = None) -> dict[str, Any]:
        result = await self._open_for_epic(epic_id, expected_head_sha=expected_head_sha)
        if result["outcome"] != "already_open":
            return result
        # A retained URL is descriptive state, not proof the PR remains open.
        # Read GitHub outside the transaction and surface a recoverable blocker.
        task = await self._db.get_task(epic_id)
        repo = await self._db.get_repo(task.repo_id)
        binding = await self._git.bind_github_repository(repo.url)
        GitHubAccess.validate_pr_url(binding, result["pr_url"])
        pull = await self._git._github_client(binding).pull_request(result["pr_url"])
        if pull.get("state") == "closed":
            return {**result, "outcome": "pr_closed",
                    "reason": "stored pull request is closed; reopen it before root admission"}
        if pull.get("state") != "open":
            raise ValueError("GitHub pull request state is malformed")
        return result

    async def _open_for_epic(self, epic_id: str, *,
                             expected_head_sha: str | None = None) -> dict[str, Any]:
        if expected_head_sha is None and getattr(self._db, "_delivery_observer", None) is not None:
            # The retry locates the source from the immutable completion ref,
            # rather than treating a descriptive completion row as code truth.
            from src.integration.delivery_truth import load_delivery_requests
            from src.integration.train import TrainTarget
            from src.integration.train_sources import project_snapshot

            task = await self._db.get_task(epic_id)
            repo = await self._db.get_repo(task.repo_id) if task and task.repo_id else None
            if repo:
                ref = "refs/heads/" + repo.default_branch.removeprefix("refs/heads/")
                snapshot = await project_snapshot(self._db, TrainTarget(
                    task.project_id, repo.id, ref))
                requests = await load_delivery_requests(self._db, [epic_id],
                    repository_id=repo.id, target_ref=ref, reduced=True)
                if snapshot is not None and epic_id in requests:
                    proof = await snapshot.is_delivered(requests[epic_id])
                    expected_head_sha = proof.source_oid
        async with self._db.immediate() as conn:
            epic = (
                await conn.execute(select(tasks).where(tasks.c.id == epic_id))
            ).mappings().one_or_none()
            if epic is None:
                return {"outcome": "unknown_epic", "epic_id": epic_id}
            if epic["pr_url"]:
                return {"outcome": "already_open", "epic_id": epic_id, "pr_url": epic["pr_url"]}
            project = (
                await conn.execute(
                    select(projects.c.hierarchical_integration_mode, projects.c.integration_repository_id)
                    .where(projects.c.id == epic["project_id"])
                )
            ).one()
            if project.hierarchical_integration_mode != "train":
                return {"outcome": "not_train", "epic_id": epic_id}
            if epic["parent_task_id"] is not None:
                return {"outcome": "not_root_epic", "epic_id": epic_id}
            if epic["status"] != "COMPLETED":
                return {"outcome": "epic_incomplete", "epic_id": epic_id}

            active_children = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.title, tasks.c.status)
                    .where(tasks.c.parent_task_id == epic_id)
                    .order_by(tasks.c.id)
                )
            ).mappings().all()
            archived_children = (
                await conn.execute(
                    select(archived_tasks.c.id, archived_tasks.c.title, archived_tasks.c.status)
                    .where(archived_tasks.c.parent_task_id == epic_id)
                    .order_by(archived_tasks.c.id)
                )
            ).mappings().all()
            children = sorted(
                [*active_children, *archived_children], key=lambda child: child["id"]
            )
            checkpoint, origin = await self._source_rows_on(conn, epic)
            if expected_head_sha is not None:
                if not _OID.fullmatch(expected_head_sha):
                    return {"outcome": "invalid_head", "epic_id": epic_id}
                head = expected_head_sha
            elif not children:
                source = leaf_root_source(
                    checkpoint, origin, branch=epic["branch_name"], repo_id=epic["repo_id"]
                )
                if source == "no_checkpoint":
                    return {"outcome": "not_epic", "epic_id": epic_id}
                if source != "ready":
                    return {"outcome": source, "epic_id": epic_id}
                head = checkpoint["checkpoint_sha"]
            else:
                head = checkpoint["verified_sha"] if checkpoint is not None else None
            pending = [child["id"] for child in children if child["status"] != "COMPLETED"]
            if pending:
                return {"outcome": "children_incomplete", "epic_id": epic_id, "pending": pending}
            if not epic["branch_name"]:
                return {"outcome": "branch_missing", "epic_id": epic_id}

            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == project.integration_repository_id,
                        repos.c.project_id == epic["project_id"],
                        repos.c.id == epic["repo_id"],
                    )
                )
            ).mappings().one_or_none()
            if repository is None or not repository["url"]:
                return {"outcome": "repository_missing", "epic_id": epic_id}

            edges = await dependencies_for(conn, [epic_id])
            dependency_ids = sorted(edges.get(epic_id, set()))
            dependencies = []
            if dependency_ids:
                active_dependencies = (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.title).where(tasks.c.id.in_(dependency_ids))
                    )
                ).mappings().all()
                archived_dependencies = (
                    await conn.execute(
                        select(archived_tasks.c.id, archived_tasks.c.title).where(
                            archived_tasks.c.id.in_(dependency_ids)
                        )
                    )
                ).mappings().all()
                dependencies = sorted(
                    [*active_dependencies, *archived_dependencies], key=lambda dep: dep["id"]
                )
            body = render_body(
                epic_id=epic_id,
                epic_title=epic["title"],
                children=[{"id": child["id"], "title": child["title"]} for child in children],
                dependencies=[{"id": dep["id"], "title": dep["title"]} for dep in dependencies],
            )

        binding = await self._git.bind_github_repository(repository["url"])
        if expected_head_sha is not None:
            remote_head = await self._git.aremote_branch_head(
                repository=binding, branch=epic["branch_name"].removeprefix("refs/heads/"))
            if remote_head != expected_head_sha:
                return {"outcome": "head_changed", "epic_id": epic_id,
                        "head_sha": expected_head_sha, "remote_head_sha": remote_head}
        if head is not None:
            # A head already on the default branch (a fix-forward merged by
            # hand) has nothing to propose; GitHub would refuse the PR anyway.
            ahead = await self._git.acommits_ahead_of_base(
                repository=binding, base=repository["default_branch"], head_sha=head
            )
            if ahead == 0:
                return {"outcome": "already_on_default", "epic_id": epic_id, "head_sha": head}
        pr_url = await self._git.acreate_pr(
            repository["source_path"] or repository["checkout_base_path"],
            branch=epic["branch_name"],
            title=epic["title"],
            body=body,
            base=repository["default_branch"],
            project_id=epic["project_id"],
            repository=binding,
        )

        async with self._db.immediate() as conn:
            await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == epic_id,
                    or_(tasks.c.pr_url.is_(None), func.trim(tasks.c.pr_url) == ""),
                )
                .values(pr_url=pr_url)
            )
            stored = (
                await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == epic_id))
            ).scalar_one()
        if stored != pr_url:
            return {"outcome": "already_open", "epic_id": epic_id, "pr_url": stored}
        return {"outcome": "opened", "epic_id": epic_id, "pr_url": pr_url}

    @staticmethod
    async def _source_rows_on(conn, epic):
        """The root's live checkpoint and origin, either of which may be absent."""
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == epic["id"]
                )
            )
        ).mappings().one_or_none()
        origin = (
            await conn.execute(
                select(task_branch_origins.c.base_sha).where(
                    task_branch_origins.c.task_id == epic["id"],
                    task_branch_origins.c.repository_id == epic["repo_id"],
                    task_branch_origins.c.retired_at.is_(None),
                )
            )
        ).mappings().one_or_none()
        return checkpoint, origin
