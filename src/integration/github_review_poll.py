"""Observe human GitHub reviews of completed train epics.

Each tick visits one page of completed train roots and asks GitHub only what
can have changed since this process last looked:

* a PR last seen closed is not fetched while it stays out of the repository's
  open-PR list, which one call per repository and tick reads;
* a PR's reviews are re-read only when its fingerprint (``updated_at``, state,
  exact head and base) changed, or after ``review_refresh_seconds``;
* a review verdict or task authorization already stored for the exact source
  identity is not proven against Git again;
* a completed parent whose PR head moved is offered for reverification, but a
  head Git already proved does not preserve the verified aggregate is not
  offered again until the head moves, and the moved head's reviews are read
  (on the same cadence) only while a rejection of the verified head blocks it.

Exact-head source CI is still observed on every visit of an open exact PR.
The cache lives in memory and is keyed by the exact source identity, so a
restart or a new identity starts with a full observation, and an observation
that raised or stored nothing is never marked complete. Per-root warnings are
logged once per change of condition, not once per tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select

from src.database.tables import projects, repos, tasks
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError
from src.integration.source_ancestry import SourceAncestryInvalid, SourceAncestryObservation
from src.models import TaskStatus

logger = logging.getLogger(__name__)

_UNREAD = object()


@dataclass
class _Observation:
    """What one exact source identity has already been observed as."""

    identity: tuple
    open: bool | None = None
    pull: tuple | None = None
    reviews_at: float = 0.0
    recorded: set[tuple] = field(default_factory=set)
    authorized: int | None = None
    #: ``(head, refusal code)`` of the last refused parent reverification.
    refused: tuple[str, str] | None = None
    #: ``(fingerprint, read at, approval)`` of the moved parent head's reviews.
    head_reviews: tuple | None = None


@dataclass
class _Repository:
    """One tick's binding, client and open-PR numbers for a repository URL."""

    binding: Any
    client: Any
    open_numbers: Any = _UNREAD


class GitHubReviewPoller:
    """Read a bounded page through AQ's configured repository credential."""

    def __init__(
        self, db: Any, evidence_producer: Any, git_manager: Any, *,
        interval_seconds: float = 30.0, page_size: int = 20,
        review_refresh_seconds: float = 600.0,
        source_ci_handler=None,
        ancestry_handler=None,
        parent_head_handler=None,
    ) -> None:
        if interval_seconds <= 0 or page_size <= 0 or review_refresh_seconds <= 0:
            raise ValueError("review poll interval, page size and refresh must be positive")
        self.db = db
        self.producer = evidence_producer
        self.git = git_manager
        self.interval_seconds = interval_seconds
        self.page_size = page_size
        self.review_refresh_seconds = review_refresh_seconds
        self.next_due_at = 0.0
        self.after_id: str | None = None
        self.source_ci_handler = source_ci_handler
        self.ancestry_handler = ancestry_handler
        self.parent_head_handler = parent_head_handler
        self._observed: dict[str, _Observation] = {}
        self._reported: dict[str, tuple] = {}
        self._cycle_seen: set[str] = set()
        self._repositories: dict[str, _Repository | Exception] = {}

    async def tick(self, now: float) -> None:
        if now < self.next_due_at:
            return
        self.next_due_at = now + self.interval_seconds
        self._repositories = {}
        rows = await self._page(self.after_id)
        if not rows and self.after_id is not None:
            self.after_id = None
            rows = await self._page(None)
        if self.after_id is None:
            self._start_cycle()
        for row in rows:
            self.after_id = row["id"]
            self._cycle_seen.add(row["id"])
            try:
                await self._poll(row, now)
            except Exception as exc:  # noqa: BLE001 - logged once per root and failure
                self._report(
                    row["id"], ("failed", type(exc).__name__, str(exc)), logging.ERROR,
                    "GitHub PR review poll failed for epic %s", row["id"], exc_info=True,
                )

    def _start_cycle(self) -> None:
        """Forget roots the finished cycle no longer listed; they re-observe in full."""
        for cache in (self._observed, self._reported):
            for task_id in set(cache) - self._cycle_seen:
                del cache[task_id]
        self._cycle_seen = set()

    def _report(
        self, task_id: str, condition: tuple, level: int = logging.DEBUG,
        message: str | None = None, *args: Any, exc_info: Any = None,
    ) -> None:
        """Log one root's condition once per change, not once per tick."""
        if self._reported.get(task_id) == condition:
            return
        self._reported[task_id] = condition
        if message is not None:
            logger.log(level, message, *args, exc_info=exc_info)

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

    async def _poll(self, row: dict[str, Any], now: float = 0.0) -> None:
        withdrawn = None
        async with self.db._engine.connect() as conn:
            source = await self.producer._pull_request_source_on(conn, row["id"])
            if source is not None:
                withdrawn = await self.producer.ancestry_rejection_on(conn, row["id"], source)
        if source is None:
            self._observed.pop(row["id"], None)
            self._report(
                row["id"], ("no_source", row["pr_url"]), logging.WARNING,
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
            await self._observe(row, source, now)
        except SourceAncestryInvalid as exc:
            await self._repair_ancestry(exc.observation)

    async def _repair_ancestry(self, observation: SourceAncestryObservation) -> None:
        identity = observation.identity()
        if self.ancestry_handler is None:
            self._report(
                observation.task_id, ("unrouted", identity, observation.reason),
                logging.WARNING, "Train root %s cannot be admitted: %s",
                observation.task_id, observation.reason,
            )
            return
        result = await self.ancestry_handler(observation)
        if not (result or {}).get("success", False):
            error = (result or {}).get("error") or result
            self._report(
                observation.task_id, ("repair_incomplete", identity, str(error)),
                logging.WARNING, "Source ancestry repair for %s did not complete: %s",
                observation.task_id, error,
            )
            return
        self._report(observation.task_id, ("withdrawn", identity))

    async def _repository(self, url: str) -> _Repository:
        """Bind each repository once per tick; a failed bind fails its roots alike."""
        found = self._repositories.get(url)
        if found is None:
            try:
                binding = await self.git.bind_github_repository(url)
                found = _Repository(binding, self.git._github_client(binding))
            except Exception as exc:
                self._repositories[url] = exc
                raise
            self._repositories[url] = found
        if isinstance(found, Exception):
            raise found
        return found

    async def _still_closed(self, repository: _Repository, number: int) -> bool:
        """Whether this tick's open-PR list proves a closed PR is still closed."""
        if repository.open_numbers is _UNREAD:
            repository.open_numbers = await self._open_numbers(repository)
        return repository.open_numbers is not None and number not in repository.open_numbers

    async def _open_numbers(self, repository: _Repository) -> frozenset[int] | None:
        """Open PR numbers, or None when unreadable so each closed PR is fetched."""
        try:
            pulls = await repository.client.paged_list(
                f"/repositories/{repository.binding.repository_id}/pulls?state=open&per_page=100"
            )
        except Exception:
            logger.debug("Open pull request list unavailable", exc_info=True)
            return None
        numbers = set()
        for pull in pulls:
            number = pull.get("number")
            if (
                isinstance(number, bool) or not isinstance(number, int) or number <= 0
                or pull.get("state") != "open"
            ):
                return None
            numbers.add(number)
        return frozenset(numbers)

    async def _observe(self, row: dict[str, Any], source: dict[str, Any], now: float) -> None:
        identity = (row["url"], row["default_branch"], tuple(sorted(source.items())))
        seen = self._observed.get(row["id"])
        if seen is None or seen.identity != identity:
            seen = self._observed[row["id"]] = _Observation(identity)
        repository = await self._repository(row["url"])
        binding, client = repository.binding, repository.client
        number = GitHubAccess.validate_pr_url(binding, source["pr_url"])
        if seen.open is False and await self._still_closed(repository, number):
            return
        pull = await client.pull_request(source["pr_url"])
        seen.open = pull.get("state") == "open"
        head, base = pull.get("head"), pull.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if not seen.open:
            unobservable = "closed"
        elif not all(isinstance(item, dict) for item in (head, base, head_repo, base_repo)):
            unobservable = "malformed"
        elif head.get("ref") != source["branch"] or head_repo.get("id") != binding.repository_id:
            unobservable = "head branch changed"
        elif base.get("ref") != row["default_branch"] or base_repo.get("id") != binding.repository_id:
            unobservable = "base changed"
        elif head.get("sha") != source["head"]:
            unobservable = "head moved"
        else:
            unobservable = None
        if unobservable is not None:
            if (unobservable == "head moved" and source["review_kind"] == "parent"
                    and self.parent_head_handler is not None):
                await self._offer_parent_head(row, source, pull, client, binding, number, seen, now)
                return
            self._report(
                row["id"], ("unobservable", unobservable),
                logging.DEBUG if unobservable == "closed" else logging.INFO,
                "Train root %s PR %s does not show its exact source head (%s); "
                "its GitHub reviews are not recorded",
                row["id"], source["pr_url"], unobservable,
            )
            return
        updated_at = pull.get("updated_at")
        fingerprint = (
            (updated_at, head["sha"], head["ref"], base["ref"])
            if isinstance(updated_at, str) and updated_at else None
        )
        if (
            fingerprint is None
            or fingerprint != seen.pull
            or now - seen.reviews_at >= self.review_refresh_seconds
        ) and await self._record_reviews(row, source, binding, client, number, seen):
            seen.pull, seen.reviews_at = fingerprint, now
        if self.source_ci_handler is not None:
            from src.integration.source_ci import observe_source_ci
            await observe_source_ci(
                row=row, source=source, client=client, handler=self.source_ci_handler,
                pull=pull,
            )
        policy = row.get("hierarchical_integration_policy") or {}
        generation = row["hierarchical_integration_generation"]
        if (policy.get("root", {}).get("admission") == "authorized"
                and seen.authorized != generation):
            evidence = await self.producer.snapshot_authorized(
                row["id"], reviewed_sha=source["head"], policy_generation=generation,
            )
            if evidence is None:
                self._report(
                    row["id"], ("authorization_blocked", identity, generation), logging.WARNING,
                    "Completed train root %s has no authorized exact %s review; "
                    "inspect admission, holds and gates (aq doctor --check integration.unadmitted_parents)",
                    row["id"], source["review_kind"],
                )
                return
            seen.authorized = generation
        self._report(row["id"], ("observed",))

    async def _offer_parent_head(
        self, row: dict[str, Any], source: dict[str, Any], pull: dict[str, Any], client: Any,
        binding: Any, number: int, seen: _Observation, now: float,
    ) -> None:
        """Ask the daemon to reverify a completed parent whose PR head moved."""
        from src.integration.parent_source import (
            REFUSED_UNTIL_HEAD_MOVES,
            ParentHeadObservation,
        )

        moved = pull["head"].get("sha")
        if seen.refused is not None and seen.refused[0] != moved:
            seen.refused = seen.head_reviews = None
        if seen.refused is not None and seen.refused[1] in REFUSED_UNTIL_HEAD_MOVES:
            return
        approval = None
        if seen.refused == (moved, "rejected"):
            approval = await self._head_approval(pull, client, binding, number, seen, now)
        result = await self.parent_head_handler(ParentHeadObservation(
            task_id=row["id"], source=source, head_sha=moved,
            policy_generation=row["hierarchical_integration_generation"],
            approval=approval,
        )) or {}
        if result.get("success"):
            self._observed.pop(row["id"], None)
            self._report(row["id"], ("reverifying", moved))
            return
        seen.refused = (moved, str(result.get("outcome")))
        self._report(
            row["id"], ("reverification_blocked", moved, str(result)),
            logging.WARNING, "Parent %s PR head advanced; reverification blocked: %s",
            row["id"], result.get("error") or result,
        )

    async def _head_approval(
        self, pull: dict[str, Any], client: Any, binding: Any, number: int,
        seen: _Observation, now: float,
    ) -> dict[str, Any] | None:
        """The latest human approval of exactly the PR head, unless changes are requested.

        Each reviewer's newest verdict counts; one who still requests changes
        (on any commit) withholds approval, as GitHub's own merge rule does.
        Reviews are reread when the PR changes or after ``review_refresh_seconds``.
        """
        moved = pull["head"].get("sha")
        updated_at = pull.get("updated_at")
        fingerprint = (updated_at, moved) if isinstance(updated_at, str) and updated_at else None
        cached = seen.head_reviews
        if (fingerprint is not None and cached is not None and cached[0] == fingerprint
                and now - cached[1] < self.review_refresh_seconds):
            return cached[2]
        reviews = await client.paged_list(
            f"/repositories/{binding.repository_id}/pulls/{number}/reviews?per_page=100"
        )
        latest: dict[str, dict[str, Any]] = {}
        for review in sorted(reviews, key=lambda item: item.get("id", -1)):
            user = review.get("user")
            if (review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
                    or not isinstance(user, dict) or user.get("type") != "User"
                    or not isinstance(user.get("login"), str) or not user["login"].strip()):
                continue
            latest[user["login"]] = review
        approvals = [
            review for review in latest.values()
            if review["state"] == "APPROVED" and review.get("commit_id") == moved
        ]
        approval = None
        if approvals and all(review["state"] != "CHANGES_REQUESTED" for review in latest.values()):
            newest = max(approvals, key=lambda review: review["id"])
            approval = {"review_id": newest["id"], "reviewer_login": newest["user"]["login"]}
        seen.head_reviews = (fingerprint, now, approval)
        return approval

    async def _record_reviews(
        self, row: dict[str, Any], source: dict[str, Any], binding: Any, client: Any,
        number: int, seen: _Observation,
    ) -> bool:
        """Store each new human verdict on the exact head; False if one was not stored."""
        reviews = await client.paged_list(
            f"/repositories/{binding.repository_id}/pulls/{number}/reviews?per_page=100"
        )
        complete = True
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
            approved = state == "APPROVED"
            verdict = (review_id, approved, login)
            if verdict in seen.recorded:
                continue
            body = review.get("body")
            note = body[:4000] if isinstance(body, str) else ""
            evidence = await self.producer.snapshot_from_pull_request(
                row["id"],
                verdict="approved" if approved else "rejected",
                reviewer_login=login,
                reviewed_sha=source["head"],
                summary=note if approved else "",
                feedback="" if approved else note or state.lower(),
                github_review_id=review_id,
            )
            if evidence is None:
                complete = False
            else:
                seen.recorded.add(verdict)
        return complete
