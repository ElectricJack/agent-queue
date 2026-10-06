"""Authorized subject/tree verdicts for the reduced train.

CommandHandler owns calls that record verdicts or request ordinary review tasks.
The old table's head/base/generation fields are compatibility provenance only;
neither verification rows nor source-CI eligibility can supply a tree verdict.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from sqlalchemy import func, insert, select

from src.database.tables import archived_tasks, tasks
from src.database.tables import integration_review_evidence as evidence
from src.git.github_contracts import GitHubAccessError
from src.git.manager import is_valid_git_oid
from src.integration.git_truth import GitTruthSnapshot


def pull_request_review_state(
    reviews: Iterable[dict], head_sha: str, *, trusted_reviewers: frozenset[str] = frozenset(),
) -> str:
    """Reduce GitHub's latest decisive review per human, with exact-head approval.

    A request for changes remains outstanding across pushes until that reviewer
    approves or dismisses it. Comments do not erase a decision; dismissed reviews
    do not count as either approval or rejection.
    """
    latest = {}
    for review in reviews:
        state, user = review.get("state"), review.get("user")
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        if not isinstance(user, dict) or user.get("type") != "User":
            continue
        login, identity = user.get("login"), review.get("id")
        if (not isinstance(login, str) or not login.strip()
                or type(identity) is not int or identity <= 0):
            raise ValueError("GitHub review identity is malformed")
        key = login.casefold()
        if key not in trusted_reviewers:
            continue
        if key not in latest or identity > latest[key]["id"]:
            latest[key] = review
    if any(review["state"] == "CHANGES_REQUESTED" for review in latest.values()):
        return "pr_changes_requested"
    if any(review["state"] == "APPROVED" and review.get("commit_id") == head_sha
           for review in latest.values()):
        return "approved"
    return "pr_review_missing"


class ReviewerPermissionUnavailable(ValueError):
    """Repository permissions could not be read reliably for a human reviewer."""


async def trusted_pull_request_reviewers(
    reviews: Iterable[dict], *, client, binding,
    requirements: ReviewRequirements | None = None,
) -> frozenset[str]:
    """Verify repository write access; an optional allowlist can only narrow it.

    Permissions are read once per decisive human in this observation. Missing
    collaborators cannot approve; unreadable permissions fail the observation.
    """
    reviews = tuple(reviews)
    logins = {
        review["user"]["login"].casefold()
        for review in reviews
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
        and isinstance(review.get("user"), dict)
        and review["user"].get("type") == "User"
        and isinstance(review["user"].get("login"), str)
        and review["user"]["login"].strip()
    }
    allowed = {identity.removeprefix("github:").casefold()
               for identity in requirements.reviewers} if requirements else set()
    trusted = set()
    for login in sorted(logins):
        if allowed and login not in allowed:
            continue
        try:
            permission = await client.request_json("GET",
                f"/repos/{binding.full_name}/collaborators/{quote(login, safe='')}/permission")
        except GitHubAccessError as exc:
            if exc.category == "rate_limited":
                raise
            raise ReviewerPermissionUnavailable(
                f"reviewer permission lookup failed: {exc.category} (HTTP {exc.http_status})"
            ) from exc
        except (OSError, ValueError) as exc:
            raise ReviewerPermissionUnavailable("reviewer permission lookup failed") from exc
        if not isinstance(permission, dict):
            raise ReviewerPermissionUnavailable("GitHub reviewer permission is malformed")
        user = permission.get("user")
        if (not isinstance(user, dict) or not isinstance(user.get("login"), str)
                or user["login"].casefold() != login
                or permission.get("permission") not in {"admin", "write", "read", "none"}):
            raise ReviewerPermissionUnavailable("GitHub reviewer permission is malformed")
        if permission["permission"] in {"write", "admin"}:
            trusted.add(login)
    return frozenset(trusted)


async def observe_pull_request_review_state(
    reviews: Iterable[dict], head_sha: str, *, client, binding,
    requirements: ReviewRequirements | None = None,
) -> str:
    """Reduce only decisions by currently trusted repository reviewers."""
    reviews = tuple(reviews)
    trusted = await trusted_pull_request_reviewers(
        reviews, client=client, binding=binding, requirements=requirements)
    return pull_request_review_state(reviews, head_sha, trusted_reviewers=trusted)


@dataclass(frozen=True)
class ReviewSubject:
    project_id: str
    repository_id: str
    task_id: str
    tree_sha: str

    def __post_init__(self):
        if not all((self.project_id, self.repository_id, self.task_id)):
            raise ValueError("review requires project, repository and subject")
        if not is_valid_git_oid(self.tree_sha):
            raise ValueError("review requires an exact tree OID")

    @property
    def request_key(self) -> str:
        value = json.dumps((self.repository_id, self.task_id, self.tree_sha))
        return "tree-review:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class ReviewRequirements:
    required: bool = False
    reviewers: frozenset[str] = frozenset()

    def allows(self, reviewer: str | None) -> bool:
        return bool(reviewer and not reviewer.startswith("service:") and reviewer in self.reviewers)


@dataclass(frozen=True)
class TreeVerdict:
    state: Literal["optional", "missing", "approved", "rejected"]
    evidence_id: str | None = None
    reviewer: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.state in {"optional", "approved"}


class TreeReviews:
    """Read current authorization; append only server-observed reviewer decisions.

    The command adapter must authenticate the reviewer and observe the actual
    Git tree before calling ``record_on`` in its verdict transaction. Authority
    comes from current policy, never from fields supplied by a worker.
    """

    def __init__(self, db, *, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    async def verdict_on(self, conn, subject: ReviewSubject,
                         requirements: ReviewRequirements) -> TreeVerdict:
        if not requirements.required:
            return TreeVerdict("optional")
        rows = (await conn.execute(select(evidence).where(
            evidence.c.repository_id == subject.repository_id,
            evidence.c.source_task_id == subject.task_id,
            evidence.c.reviewed_tree_sha == subject.tree_sha,
        ).order_by(evidence.c.created_at.desc(), evidence.c.id.desc()))).mappings()
        for row in rows:
            reviewer = row["reviewer_identity"]
            # Legacy actual reviews are reusable; automatic source admission,
            # ancestry diagnostics and task-authorization rows are not reviews.
            path = row["evidence"].get("decision_path")
            if path not in {"tree_review", "github_pull_request", "review_task_close",
                            "reopen_with_feedback"} or not requirements.allows(reviewer):
                continue
            return TreeVerdict(row["verdict"], row["id"], reviewer)
        return TreeVerdict("missing")

    async def verdict(self, subject: ReviewSubject,
                      requirements: ReviewRequirements) -> TreeVerdict:
        async with self.db._engine.connect() as conn:
            return await self.verdict_on(conn, subject, requirements)

    @staticmethod
    async def observe(snapshot: GitTruthSnapshot, task_id: str, branch_ref: str,
                      *, expected_tree: str | None = None) -> tuple[ReviewSubject, str]:
        """Observe the current subject tree without requiring a verifier row.

        A review request pins its expected tree, not a generation or commit.
        The authenticated task/provider adapter verifies subject authorization.
        """
        target = snapshot.for_target(branch_ref)
        if target.observation.error or not target.target_oid:
            raise ValueError("review Git observation is unavailable")
        tree = await target.truth.git.atree_sha(target.observation.store, target.target_oid)
        if expected_tree is not None and tree != expected_tree:
            raise ValueError("reviewed tree changed")
        if not await target.observation.is_fresh():
            raise ValueError("review ref changed")
        return ReviewSubject(target.observation.project_id, target.observation.repository_id,
                             task_id, tree), target.target_oid

    async def record_on(
        self, conn, subject: ReviewSubject, requirements: ReviewRequirements, *,
        reviewer: str, verdict: Literal["approved", "rejected"], decision_id: str,
        reviewed_head_sha: str, source_base: str, provenance: dict,
        review_kind: Literal["leaf", "parent"] = "parent",
        reviewer_task_id: str | None = None, reviewer_session_attempt_id: str | None = None,
    ) -> dict:
        """Append one authenticated decision, idempotently, under the task lock.

        A decision id is an immutable provider review id or reviewer attempt
        decision. A retry cannot rewrite its verdict or reviewed tree. Reopen
        and review-task close remain ordinary task-owner operations.
        """
        if not requirements.allows(reviewer):
            raise ValueError("reviewer is not currently authorized")
        if verdict not in {"approved", "rejected"} or not decision_id:
            raise ValueError("review requires a verdict and immutable decision id")
        if not is_valid_git_oid(reviewed_head_sha) or not is_valid_git_oid(source_base):
            raise ValueError("review provenance requires exact head and base OIDs")
        if review_kind not in {"leaf", "parent"}:
            raise ValueError("invalid review subject kind")
        await self.db.lock_hierarchy_project(conn, subject.project_id)
        project = (await conn.execute(select(tasks.c.project_id).where(
            tasks.c.id == subject.task_id,
            tasks.c.repo_id == subject.repository_id,
        ))).scalar_one_or_none()
        if project != subject.project_id:
            raise ValueError("review subject repository/project changed")
        identity = json.dumps((subject.repository_id, subject.task_id, reviewer, decision_id))
        row_id = "tree-verdict-" + hashlib.sha256(identity.encode()).hexdigest()
        row = {
            "id": row_id, "source_task_id": subject.task_id, "repository_id": subject.repository_id,
            "source_base": source_base, "reviewed_head_sha": reviewed_head_sha,
            "reviewed_tree_sha": subject.tree_sha, "reviewer_identity": reviewer,
            "reviewer_task_id": reviewer_task_id, "reviewer_session_attempt_id": reviewer_session_attempt_id,
            "review_kind": review_kind, "generation": 0, "verdict": verdict,
            "evidence": {**provenance, "decision_path": "tree_review", "decision_id": decision_id},
        }
        existing = (await conn.execute(select(evidence).where(
            evidence.c.id == row_id,
        ))).mappings().one_or_none()
        if existing is not None:
            if any(existing[key] != value for key, value in row.items()):
                raise ValueError("immutable review decision changed")
            return dict(existing)
        latest = (await conn.execute(select(func.max(evidence.c.created_at)).where(
            evidence.c.repository_id == subject.repository_id,
            evidence.c.source_task_id == subject.task_id,
            evidence.c.reviewed_tree_sha == subject.tree_sha,
        ))).scalar_one()
        now = self.clock()
        row["created_at"] = max(now, math.nextafter(latest, math.inf)) if latest else now
        await conn.execute(insert(evidence).values(**row))
        return row

    async def request(
        self, subject: ReviewSubject, requirements: ReviewRequirements, *,
        execute: Callable[[str, dict], Awaitable[dict]], route: dict,
        branch: str, head_sha: str,
    ) -> dict:
        """One ordinary task for an unresolved required tree, across restarts.

        Failed/closed requests are still requests: their owner retries or
        reopens them. Repeated visits never start a review chain. A separate
        per-key advisory lock serializes find/create without nesting the task
        owner's hierarchy lock. No integration request record is created.
        """
        if not requirements.required:
            return {"success": True, "outcome": "optional"}
        if not is_valid_git_oid(head_sha) or not branch:
            raise ValueError("review request requires the observed branch and exact head")
        key = subject.request_key
        lock_id = int.from_bytes(hashlib.sha256((subject.project_id + key).encode()).digest()[:8],
                                 "big", signed=True)
        async with self.db._engine.begin() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock(lock_id)))
            verdict = await self.verdict_on(conn, subject, requirements)
            if verdict.state != "missing":
                return {"success": True, "outcome": verdict.state}
            for table in (tasks, archived_tasks):
                existing = (await conn.execute(select(table.c.id).where(
                    table.c.project_id == subject.project_id, table.c.dedup_key == key,
                ).order_by(table.c.created_at, table.c.id).limit(1))).scalar_one_or_none()
                if existing:
                    return {"success": True, "outcome": "requested", "task_id": existing,
                            "created": False}
            result = await execute("ensure_task", {
                **route, "project_id": subject.project_id, "repo_id": subject.repository_id,
                "dedup_key": key, "title": f"Review tree for {subject.task_id}",
                "task_type": "chore", "discovered_from": subject.task_id, "root": True,
                "_suppress_created_event": True,
                "description": (
                    f"Review subject {subject.task_id} in repository {subject.repository_id}.\n"
                    f"Branch: {branch}\nHead: {head_sha}\nRequired tree: {subject.tree_sha}\n"
                    "Approve or reject this exact tree. Resolve the tree from Git; an unchanged-tree "
                    "rebase preserves this request. Report a changed tree as stale."
                ),
            })
            return {**result, "outcome": "requested" if result.get("success") else "unknown"}
