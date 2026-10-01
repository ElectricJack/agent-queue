"""Observe human GitHub reviews of completed train epics."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, select

from src.database.tables import projects, repos, tasks
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError
from src.integration.source_ancestry import SourceAncestryInvalid, SourceAncestryObservation
from src.models import TaskStatus

logger = logging.getLogger(__name__)


class GitHubReviewPoller:
    """Read a bounded page through AQ's configured repository credential."""

    def __init__(
        self, db: Any, evidence_producer: Any, git_manager: Any, *,
        interval_seconds: float = 30.0, page_size: int = 20,
        source_ci_handler=None,
        ancestry_handler=None,
    ) -> None:
        if interval_seconds <= 0 or page_size <= 0:
            raise ValueError("review poll interval and page size must be positive")
        self.db = db
        self.producer = evidence_producer
        self.git = git_manager
        self.interval_seconds = interval_seconds
        self.page_size = page_size
        self.next_due_at = 0.0
        self.after_id: str | None = None
        self.source_ci_handler = source_ci_handler
        self.ancestry_handler = ancestry_handler

    async def tick(self, now: float) -> None:
        if now < self.next_due_at:
            return
        self.next_due_at = now + self.interval_seconds
        rows = await self._page(self.after_id)
        if not rows and self.after_id is not None:
            self.after_id = None
            rows = await self._page(None)
        for row in rows:
            self.after_id = row["id"]
            try:
                await self._poll(row)
            except Exception:
                logger.exception("GitHub PR review poll failed for epic %s", row["id"])

    async def _page(self, after_id: str | None) -> list[dict[str, Any]]:
        statement = (
            select(
                tasks.c.id, tasks.c.pr_url, repos.c.url,
                repos.c.default_branch,
                projects.c.hierarchical_integration_policy,
                projects.c.hierarchical_integration_generation,
            )
            .select_from(
                tasks.join(projects, projects.c.id == tasks.c.project_id).join(
                    repos,
                    and_(
                        repos.c.id == tasks.c.repo_id,
                        repos.c.project_id == tasks.c.project_id,
                    ),
                )
            )
            .where(
                projects.c.hierarchical_integration_mode == "train",
                projects.c.integration_repository_id == tasks.c.repo_id,
                tasks.c.parent_task_id.is_(None),
                tasks.c.status == TaskStatus.COMPLETED.value,
                tasks.c.pr_url.is_not(None),
                tasks.c.pr_url != "",
            )
            .order_by(tasks.c.id)
            .limit(self.page_size)
        )
        if after_id is not None:
            statement = statement.where(tasks.c.id > after_id)
        async with self.db._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings()]

    async def _poll(self, row: dict[str, Any]) -> None:
        withdrawn = None
        async with self.db._engine.connect() as conn:
            source = await self.producer._pull_request_source_on(conn, row["id"])
            if source is not None:
                withdrawn = await self.producer.ancestry_rejection_on(conn, row["id"], source)
        if source is None:
            logger.warning(
                "Completed train root %s has PR %s but no eligible review source; "
                "inspect its checkpoint and branch origin (aq integration materialize-root %s)",
                row["id"], row["pr_url"], row["id"],
            )
            return
        if withdrawn is not None:
            # Withdrawn at construction, or a repair that did not finish: the
            # exact identity is still the completed source, so route it again.
            await self._repair_ancestry(
                SourceAncestryObservation.from_evidence(row["id"], source, withdrawn)
            )
            return
        try:
            await self._observe(row, source)
        except SourceAncestryInvalid as exc:
            await self._repair_ancestry(exc.observation)

    async def _repair_ancestry(self, observation: SourceAncestryObservation) -> None:
        if self.ancestry_handler is None:
            logger.warning("Train root %s cannot be admitted: %s", observation.task_id,
                           observation.reason)
            return
        result = await self.ancestry_handler(observation)
        if not (result or {}).get("success", False):
            logger.warning(
                "Source ancestry repair for %s did not complete: %s",
                observation.task_id, (result or {}).get("error") or result,
            )

    async def _observe(self, row: dict[str, Any], source: dict[str, Any]) -> None:
        binding = await self.git.bind_github_repository(row["url"])
        number = GitHubAccess.validate_pr_url(binding, source["pr_url"])
        client = self.git._github_client(binding)
        pull = await client.pull_request(source["pr_url"])
        head, base = pull.get("head"), pull.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if (
            pull.get("state") != "open"
            or not isinstance(head, dict)
            or not isinstance(base, dict)
            or not isinstance(head_repo, dict)
            or not isinstance(base_repo, dict)
            or head.get("sha") != source["head"]
            or head.get("ref") != source["branch"]
            or head_repo.get("id") != binding.repository_id
            or base.get("ref") != row["default_branch"]
            or base_repo.get("id") != binding.repository_id
        ):
            return
        reviews = await client.paged_list(
            f"/repositories/{binding.repository_id}/pulls/{number}/reviews?per_page=100"
        )
        for review in sorted(reviews, key=lambda item: item.get("id", -1)):
            review_id = review.get("id")
            if isinstance(review_id, bool) or not isinstance(review_id, int) or review_id <= 0:
                raise GitHubAccessError("conflict_or_invalid", "GitHub review ID was malformed")
            state = review.get("state")
            if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                continue
            user = review.get("user")
            if not isinstance(user, dict) or user.get("type") != "User":
                continue
            login = user.get("login")
            if not isinstance(login, str) or not login.strip():
                raise GitHubAccessError("conflict_or_invalid", "GitHub reviewer was malformed")
            if review.get("commit_id") != source["head"]:
                continue
            body = review.get("body")
            note = body[:4000] if isinstance(body, str) else ""
            approved = state == "APPROVED"
            await self.producer.snapshot_from_pull_request(
                row["id"],
                verdict="approved" if approved else "rejected",
                reviewer_login=login,
                reviewed_sha=source["head"],
                summary=note if approved else "",
                feedback="" if approved else note or state.lower(),
                github_review_id=review_id,
            )
        if self.source_ci_handler is not None:
            from src.integration.source_ci import observe_source_ci
            await observe_source_ci(
                row=row, source=source, client=client, handler=self.source_ci_handler,
                pull=pull,
            )
        policy = row.get("hierarchical_integration_policy") or {}
        if policy.get("root", {}).get("admission") == "authorized":
            await self.producer.snapshot_authorized(
                row["id"], reviewed_sha=source["head"],
                policy_generation=row["hierarchical_integration_generation"],
            )
