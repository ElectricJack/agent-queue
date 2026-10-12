"""Observe human GitHub reviews of completed train epics.

Each tick visits one page of completed train roots and asks GitHub only what
can have changed since this process last looked:

* a PR last seen closed is not fetched while it stays out of the repository's
  open-PR list, which one call per repository and tick reads;
* a PR's reviews are re-read only when its fingerprint (``updated_at``, state,
  exact head and base) changed, or after ``review_refresh_seconds``;
* a review verdict or task authorization already stored for the exact source
  identity is not proven against Git again.

Exact-head source CI is still observed on every visit of an open exact PR.
The cache lives in memory and is keyed by the exact source identity, so a
restart or a new identity starts with a full observation, and an observation
that raised or stored nothing is never marked complete. Per-root warnings are
logged once per change of condition, not once per tick.
Repository binding failures back off across ticks and log once per retry window,
without keeping exceptions or rendering their tracebacks for each root.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select

from src.database.tables import projects, repos, tasks
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.integration.source_ancestry import SourceAncestryInvalid, SourceAncestryObservation
from src.integration.source_ci import pull_request_conflicts
from src.models import TaskStatus
from src.projects.github import GitHubError

logger = logging.getLogger(__name__)

_UNREAD = object()

#: GitHub computes a PR's mergeability in the background after a read; the
#: first ``null`` is looked at again this soon rather than after the backoff.
MERGEABILITY_RETRY_SECONDS = 15


def pull_request_mismatch(pull, binding, *, branch, base_ref, head_sha):
    """The PR must propose this repository's exact source to this boundary."""
    head, base = pull.get("head"), pull.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if pull.get("state") not in {"open", "closed"}:
        raise ValueError("GitHub pull request state is malformed")
    if pull.get("state") != "open":
        return "closed"
    if not all(isinstance(item, dict) for item in (head, base, head_repo, base_repo)):
        raise ValueError("GitHub pull request identity is malformed")
    if head.get("ref") != branch or head_repo.get("id") != binding.repository_id:
        return "head branch changed"
    if base.get("ref") != base_ref or base_repo.get("id") != binding.repository_id:
        return "base changed"
    if head.get("sha") != head_sha:
        return "head moved"
    return None


class RootPullRequestGate:
    """Read the PR at freeze time; unavailable observations back off, never admit.

    Git alone has already decided this source is pending. These observations
    authorize admission, and have no bearing on delivery or a frozen batch.
    """

    def __init__(self, db, *, repository, checks, clock=time.time, review_requirements=None,
                 failure_handler=None):
        self.db, self.repository, self.checks, self.clock = db, repository, checks, clock
        self.review_requirements = review_requirements
        self.failure_handler = failure_handler
        self._deferred = {}

    async def __call__(self, target, member):
        return await self._observe(target, member)

    async def admit_repaired_source(self, target, member):
        """Authorize a source whose exact repair is admitted in the same batch."""
        return await self._observe(target, member, repaired_source=True)

    async def _observe(self, target, member, *, repaired_source=False):
        from src.integration.reviews import (
            ReviewerPermissionUnavailable,
            observe_pull_request_review_state,
        )
        from src.integration.runtime_contracts import HeadIdentity

        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(
                tasks.c.pr_url, tasks.c.branch_name, projects.c.hierarchical_integration_policy,
                projects.c.hierarchical_integration_generation,
            ).join(projects, projects.c.id == tasks.c.project_id).where(
                tasks.c.id == member.task_id,
            ))).mappings().one()
            request = None
            if self.failure_handler is not None and not repaired_source:
                from src.integration.delivery_truth import load_delivery_requests

                request = (await load_delivery_requests(self.db, [member.task_id],
                    repository_id=target.repository_id, target_ref=target.target_ref,
                    conn=conn, reduced=True)).get(member.task_id)
        policy = row["hierarchical_integration_policy"] or {}
        url = row["pr_url"]

        def blocker(code, **detail):
            return {"code": code, "task_id": member.task_id, "ref": member.task_id,
                    "source_sha": member.source_sha, "pr_url": url,
                    "detail": f"root {member.task_id} PR admission: {code}", **detail}

        if not isinstance(url, str) or not url.strip():
            return blocker("awaiting_pr")
        key = (target.key, member.task_id, member.source_sha, url, row["branch_name"],
               json.dumps(policy, sort_keys=True), request)
        for stale in list(self._deferred):
            if stale[:2] == key[:2] and stale != key:
                del self._deferred[stale]
        previous = self._deferred.get(key)
        now = self.clock()
        if (previous and now < previous["retry_at"]
                and not (repaired_source and previous["code"] == "pr_checks_red")):
            return previous

        def defer(code, *, due_at=None, **detail):
            delay = min((previous or {}).get("retry_seconds", 30) * 2, 600)
            result = blocker(code, retry_at=max(now + delay, due_at or 0),
                             retry_seconds=delay, **detail)
            self._deferred[key] = result
            return result

        def computing():
            # A repeated null falls back to the ordinary backoff.
            if (previous or {}).get("mergeable") == "unknown":
                return defer("awaiting_pr_checks", mergeable="unknown")
            result = blocker("awaiting_pr_checks", mergeable="unknown",
                             retry_at=now + MERGEABILITY_RETRY_SECONDS,
                             retry_seconds=(previous or {}).get("retry_seconds", 30))
            self._deferred[key] = result
            return result

        try:
            binding, client = await self.repository(target)
            number = GitHubAccess.validate_pr_url(binding, url)
            pull = await client.pull_request(url)
            mismatch = pull_request_mismatch(pull, binding,
                branch=str(row["branch_name"]).removeprefix("refs/heads/"),
                base_ref=target.target_ref.removeprefix("refs/heads/"), head_sha=member.source_sha)
            if mismatch:
                return defer("pr_closed" if mismatch == "closed" else "awaiting_pr",
                             reason=mismatch)
            if pull.get("draft"):
                return defer("pr_draft")
            from src.integration.models import integration_ci_policy

            ci = integration_ci_policy(policy)
            # Local member validation uses the trusted integration job lane.
            # Even automatic admission cannot execute unreviewed code there, so
            # a local or hybrid root requires the exact-head approval first.
            local = ci is not None and ci.source_for("root") != "hosted"
            reviewed = (policy.get("root") or {}).get("admission", "reviewed") == "reviewed"

            async def review_state():
                reviews = await client.paged_list(
                    f"/repositories/{binding.repository_id}/pulls/{number}/reviews?per_page=100")
                return await observe_pull_request_review_state(reviews, member.source_sha,
                    client=client, binding=binding, requirements=self.review_requirements)

            if local:
                state = await review_state()
                if state != "approved":
                    return defer(state)
            exact = await self.checks(target, member, policy, binding)
            if exact is None:
                raise ValueError("PR required checks observer is unavailable")
            result = await exact.refresh_if_due(HeadIdentity(repository_id=target.repository_id,
                ref="refs/heads/" + pull["head"]["ref"], sha=member.source_sha, generation=0))
            # A successful authenticated listing can prove that no PR workflow
            # exists for this exact head. GitHub suppresses that workflow for
            # dirty PRs; the frozen candidate owns integration and its own CI.
            # An outage, an executing run or a genuine failure never qualifies.
            suppressed = (pull_request_conflicts(pull) and bool(result.checks)
                          and all(check.detail.get("missing_pr_run") is True
                                  for check in result.checks)
                          and result.state.value in {"pending", "unknown"})
            if not result.green and not suppressed and result.state.value != "red":
                if result.state.value != "unknown" and pull.get("mergeable") is None:
                    return computing()
            if result.state.value == "unknown" and not suppressed:
                return defer("unknown", due_at=result.due_at, reason="PR checks unavailable")
            repaired_red = repaired_source and result.state.value == "red"
            if not result.green and not suppressed and not repaired_red:
                recovery = {}
                if result.state.value == "red" and request is not None:
                    from src.integration.checks import Conclusion
                    from src.integration.root_pr_recovery import RootPRCheckFailure

                    # Workflow-level failure, absent checks and cancellations do
                    # not establish a failed required job that a worker can fix.
                    if any(check.conclusion is Conclusion.FAILURE for check in result.checks):
                        recovery = await self.failure_handler(RootPRCheckFailure(
                            request=request, source_sha=member.source_sha,
                            source_base_sha=member.source_base_sha, pr_url=url,
                            policy_generation=row["hierarchical_integration_generation"],
                            policy=policy, checks=result,
                        ))
                return defer(recovery.get("blocker") or (
                             "pr_checks_red" if result.state.value == "red"
                             else "awaiting_pr_checks"), due_at=result.due_at,
                             **({"recovery": recovery} if recovery else {}))
            if reviewed and not local:
                state = await review_state()
                if state != "approved":
                    return defer(state)
            self._deferred.pop(key, None)
            return None
        except ReviewerPermissionUnavailable as exc:
            return defer("pr_review_permission_unavailable", reason=str(exc))
        except (GitError, GitHubError, GitHubAccessError, OSError, ValueError, KeyError, TypeError) as exc:
            from src.git.github_contracts import rate_limit_cause

            if rate_limit_cause(exc) is not None:
                raise
            return defer("unknown", reason=str(exc))


@dataclass
class _Observation:
    """What one exact source identity has already been observed as."""

    identity: tuple
    open: bool | None = None
    pull: tuple | None = None
    reviews_at: float = 0.0
    recorded: set[tuple] = field(default_factory=set)
    authorized: int | None = None


@dataclass
class _Repository:
    """One tick's binding, client and open-PR numbers for a repository URL."""

    binding: Any
    client: Any
    open_numbers: Any = _UNREAD


@dataclass
class _RepositoryFailure:
    """Retry state without retaining exception tracebacks and their locals."""

    retry_at: float
    delay: float


class GitHubReviewPoller:
    """Read a bounded page through AQ's configured repository credential."""

    def __init__(
        self, db: Any, evidence_producer: Any, git_manager: Any, *,
        interval_seconds: float = 30.0, page_size: int = 20,
        review_refresh_seconds: float = 600.0,
        repository_retry_seconds: float = 60.0,
        repository_retry_max_seconds: float = 600.0,
        source_ci_handler=None,
        ancestry_handler=None,
        parent_head_handler=None,
    ) -> None:
        if interval_seconds <= 0 or page_size <= 0 or review_refresh_seconds <= 0:
            raise ValueError("review poll interval, page size and refresh must be positive")
        if repository_retry_seconds <= 0 or repository_retry_max_seconds < repository_retry_seconds:
            raise ValueError("repository retry delays must be positive and ordered")
        self.db = db
        self.producer = evidence_producer
        self.git = git_manager
        self.interval_seconds = interval_seconds
        self.page_size = page_size
        self.review_refresh_seconds = review_refresh_seconds
        self.repository_retry_seconds = repository_retry_seconds
        self.repository_retry_max_seconds = repository_retry_max_seconds
        self.next_due_at = 0.0
        self.after_id: str | None = None
        self.source_ci_handler = source_ci_handler
        self.ancestry_handler = ancestry_handler
        self.parent_head_handler = parent_head_handler
        self._observed: dict[str, _Observation] = {}
        self._reported: dict[str, tuple] = {}
        self._cycle_seen: set[str] = set()
        self._repositories: dict[str, _Repository | None] = {}
        self._repository_failures: dict[str, _RepositoryFailure] = {}

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
            except (GitHubAccessError, GitError) as exc:
                self._report(
                    row["id"], ("failed", type(exc).__name__, str(exc)), logging.WARNING,
                    "GitHub PR review poll failed for epic %s in %s: %s",
                    row["id"], row["url"], " ".join(str(exc).split())[:1000],
                )
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
                "inspect its recorded checkpoint and branch origin",
                row["id"], row["pr_url"],
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

    async def _repository(self, url: str, now: float) -> _Repository | None:
        """Share failed binds across roots and ticks, retrying with capped backoff."""
        if url in self._repositories:
            return self._repositories[url]
        failure = self._repository_failures.get(url)
        if failure is not None and now < failure.retry_at:
            self._repositories[url] = None
            return None
        try:
            binding = await self.git.bind_github_repository(url)
            found = _Repository(binding, self.git._github_client(binding))
        except Exception as exc:  # noqa: BLE001 - repository adapter failures back off
            delay = min(
                failure.delay * 2 if failure else self.repository_retry_seconds,
                self.repository_retry_max_seconds,
            )
            self._repository_failures[url] = _RepositoryFailure(now + delay, delay)
            self._repositories[url] = None
            logger.warning(
                "GitHub PR review repository bind failed for %s; retry in %.0fs: %s",
                url, delay, " ".join(str(exc).split())[:1000],
            )
            return None
        self._repository_failures.pop(url, None)
        self._repositories[url] = found
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
        except (GitHubAccessError, GitError) as exc:
            logger.debug(
                "Open pull request list unavailable for %s: %s",
                repository.binding.full_name, " ".join(str(exc).split())[:1000],
            )
            return None
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
        repository = await self._repository(row["url"], now)
        if repository is None:
            return
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
                from src.integration.parent_source import ParentHeadObservation

                result = await self.parent_head_handler(ParentHeadObservation(
                    task_id=row["id"], source=source, head_sha=head.get("sha"),
                    policy_generation=row["hierarchical_integration_generation"],
                ))
                if (result or {}).get("success"):
                    self._observed.pop(row["id"], None)
                    self._report(row["id"], ("reverifying", head.get("sha")))
                    return
                self._report(
                    row["id"], ("reverification_blocked", head.get("sha"), str(result)),
                    logging.WARNING, "Parent %s PR head advanced; reverification blocked: %s",
                    row["id"], (result or {}).get("error") or result,
                )
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

    async def _record_reviews(
        self, row: dict[str, Any], source: dict[str, Any], binding: Any, client: Any,
        number: int, seen: _Observation,
    ) -> bool:
        """Store only trusted human verdicts on the exact head."""
        from src.integration.reviews import trusted_pull_request_reviewers

        reviews = await client.paged_list(
            f"/repositories/{binding.repository_id}/pulls/{number}/reviews?per_page=100"
        )
        trusted = await trusted_pull_request_reviewers(
            reviews, client=client, binding=binding)
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
            if login.casefold() not in trusted:
                continue
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
