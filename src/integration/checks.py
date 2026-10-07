"""Exact-commit required checks: one refreshable cache per commit.

A row in ``integration_check_evidence`` with ``sha`` set caches the latest
attempt of one required check, on one exact commit, from one trusted
producer.  A refresh overwrites it, so a rerun, a pending attempt or an
unavailable provider replaces an older success instead of inheriting it.
Providers are the existing ``HostedCIProducer`` and ``LocalCIProducer``;
this module only reshapes what they observe into per-check rows.

Refreshing observes the provider before opening any connection and writes in
one short transaction afterwards, so a slow provider never holds a ref lock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, not_, select
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import integration_check_evidence
from src.git.manager import GitError
from src.integration.ci import IntegrationCITrust, IntegrationTrustManifest
from src.integration.ci_producers import (
    HostedCIProducer,
    LocalCIProducer,
    ProducerObservation,
    ProducerRequest,
    digest,
)
from src.integration.subjects import CIState, HeadIdentity

logger = logging.getLogger(__name__)

#: Prefix for local job evidence; the suffix binds its boundary and command plan.
LOCAL_CHECKS_PRODUCER_ID = "local-jobs"


def local_checks_producer_id(*, scope: str, names: tuple[str, ...], version: str,
                             commands: tuple[str, ...], queue_seconds: float,
                             run_seconds: float) -> str:
    """Stable cache identity for one boundary's named command plan.

    Attempts and refs change without invalidating a published head's evidence;
    commands, check versions and execution bounds do invalidate it. Different
    boundaries never overwrite each other's rows on the same commit.
    """
    return LOCAL_CHECKS_PRODUCER_ID + ":" + digest({
        "scope": scope, "names": names, "version": version, "commands": commands,
        "queue_seconds": float(queue_seconds), "run_seconds": float(run_seconds),
    })


def hybrid_checks_producer_id(local_id: str, hosted_id: str) -> str:
    """Completion trust changes when a boundary requires a second producer."""
    return "hybrid:" + digest([local_id, hosted_id])


class Conclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    # The provider finished without producing this required check.
    MISSING = "missing"
    PENDING = "pending"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


FINAL = frozenset({Conclusion.SUCCESS, Conclusion.FAILURE, Conclusion.MISSING})


class ChecksState(StrEnum):
    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    # Cancelled, unavailable or never observed: no verdict until a refresh.
    UNKNOWN = "unknown"


class RequiredChecks(BaseModel):
    """The required check names, their version and the producer trusted for them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    names: tuple[str, ...] = Field(min_length=1)
    producer_id: str = Field(min_length=1)

    @classmethod
    def from_trust(cls, trust: IntegrationCITrust | IntegrationTrustManifest) -> RequiredChecks:
        """Resolve from policy-derived trust (``ci_trust_from_policy``) or a subject manifest."""
        return cls(
            version=trust.required_checks.version,
            names=trust.required_checks.names,
            producer_id=str(trust.ci_producer_app_id)
            if isinstance(trust, IntegrationTrustManifest)
            else trust.producer_id,
        )


class CommitCheck(BaseModel):
    """The latest attempt of one required check on one exact commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    name: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    required_check_version: str = Field(min_length=1)
    conclusion: Conclusion
    classification: str = Field(min_length=1)
    run_id: str = ""
    workflow_id: str = ""
    attempt: int = Field(default=0, ge=0)
    run_url: str | None = None
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: float
    due_at: float | None = None


class ChecksResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    sha: str
    required: RequiredChecks
    state: ChecksState
    checks: tuple[CommitCheck, ...]
    # When an unfinished verdict should be refreshed; ``None`` once final.
    due_at: float | None = None

    @property
    def green(self) -> bool:
        return self.state is ChecksState.GREEN


def evaluate(
    required: RequiredChecks, head: HeadIdentity, rows: Iterable[CommitCheck], *, now: float
) -> ChecksResult:
    """Pure verdict: green only when every required check succeeded on this exact head.

    Rows for another repository, SHA, producer or check version are ignored,
    so they can neither satisfy nor fail the required set.
    """
    by_name = {
        row.name: row
        for row in rows
        if row.repository_id == head.repository_id
        and row.sha == head.sha
        and row.producer_id == required.producer_id
        and row.required_check_version == required.version
    }
    checks = tuple(
        by_name.get(name)
        or CommitCheck(
            repository_id=head.repository_id,
            sha=head.sha,
            name=name,
            producer_id=required.producer_id,
            required_check_version=required.version,
            conclusion=Conclusion.UNAVAILABLE,
            classification="none",
            reason="not_observed",
            observed_at=now,
            due_at=now,
        )
        for name in required.names
    )
    conclusions = {check.conclusion for check in checks}
    workflow_conclusions = {check.detail.get("workflow_conclusion", "success") for check in checks}
    if conclusions & {Conclusion.FAILURE, Conclusion.MISSING}:
        state = ChecksState.RED
    elif conclusions & {Conclusion.UNAVAILABLE, Conclusion.CANCELLED}:
        state = ChecksState.UNKNOWN
    elif Conclusion.PENDING in conclusions:
        state = ChecksState.PENDING
    elif "failure" in workflow_conclusions:
        # Keep workflow-level publication gating without inventing failed jobs.
        # The train blocks repair allocation when no required check actually failed.
        state = ChecksState.RED
    elif workflow_conclusions != {"success"}:
        state = ChecksState.UNKNOWN
    else:
        state = ChecksState.GREEN
    due = [check.due_at for check in checks if check.conclusion not in FINAL and check.due_at]
    return ChecksResult(
        repository_id=head.repository_id,
        sha=head.sha,
        required=required,
        state=state,
        checks=checks,
        due_at=None if state is ChecksState.RED or not due else min(due),
    )


class ChecksProvider(Protocol):
    required: RequiredChecks
    #: Whether a target commit's own checks are an available baseline. Local
    #: validation jobs exist only for candidates, so their lane keeps filing
    #: repairs exactly as before and never claims a pre-existing failure.
    compares_targets: bool

    async def request(self, head: HeadIdentity) -> ProducerRequest: ...

    async def observe(self, head: HeadIdentity, *, now: float) -> tuple[CommitCheck, ...]: ...

    async def rerequest(self, head: HeadIdentity, suites: tuple[int, ...]) -> tuple[int, ...]: ...


def _check(required: RequiredChecks, head: HeadIdentity, name: str, now: float, **values):
    return CommitCheck(
        repository_id=head.repository_id,
        sha=head.sha,
        name=name,
        producer_id=required.producer_id,
        required_check_version=required.version,
        observed_at=now,
        **values,
    )


_HOSTED_DEFERRED = {
    CIState.PENDING: Conclusion.PENDING,
    # Allow a newly published candidate time to acquire a push run. ExactChecks
    # bounds this absence separately from a workflow that is already running.
    CIState.NONE: Conclusion.PENDING,
}
_HOSTED_CONCLUSIONS = {
    "success": Conclusion.SUCCESS,
    "failure": Conclusion.FAILURE,
    "missing": Conclusion.MISSING,
    "cancelled": Conclusion.CANCELLED,
}


class HostedChecks:
    """Per-check rows from the authenticated ``HostedCIProducer`` observation.

    Pushing the candidate ref under ``aq/integration/**`` (or a git-first
    batch candidate under ``aq/batches/**``) is expected to trigger the workflows,
    so there is nothing to dispatch. ExactChecks bounds an absent push run.
    """

    compares_targets = True

    def __init__(
        self, producer: HostedCIProducer, *,
        diagnose: Callable[[HeadIdentity], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.producer, self.diagnose = producer, diagnose
        self.required = RequiredChecks.from_trust(producer.trust)
        if producer.observer.expected_event == "pull_request":
            # A PR run tests the merge ref. It must never satisfy bare-head
            # candidate checks, even when the source OID and names are identical.
            self.required = self.required.model_copy(update={
                "producer_id": self.required.producer_id + ":pull_request",
            })

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        return ProducerRequest(outcome="already_running", reason="candidate_push_triggers_checks")

    async def rerequest(self, head: HeadIdentity, suites: tuple[int, ...]) -> tuple[int, ...]:
        """Re-run this head's own check suites; never raise.

        A re-run is what a candidate whose failures are not its own needs: the
        head has not moved, so only a suite re-request produces new evidence.
        Every suite id here was read from this repository's own cached rows for
        this exact commit, so a re-request cannot reach another repository, commit
        or check. An outage is the expected failure, not a defect: the caller is
        bounded and ends at a named blocker instead of asking forever.
        """
        addressed = []
        for suite in suites:
            try:
                await self.producer.client.rerequest_check_suite(suite)
            except Exception:  # an outage is the expected failure, not a defect
                logger.warning("check suite re-request failed for suite %s of %s",
                               suite, head.sha[:12], exc_info=True)
                continue
            addressed.append(suite)
        return tuple(addressed)

    def _run_url(self, run: dict[str, Any]) -> str:
        return (
            f"https://github.com/{self.producer.trust.full_name}/actions/runs/"
            f"{run['workflow_run_id']}/attempts/{run['run_attempt']}"
        )

    async def observe(self, head: HeadIdentity, *, now: float) -> tuple[CommitCheck, ...]:
        observed: ProducerObservation = await self.producer.observe_head(head)
        if not observed.trusted:
            conclusion = _HOSTED_DEFERRED.get(observed.state, Conclusion.UNAVAILABLE)
            return tuple(
                _check(
                    self.required, head, name, now,
                    conclusion=conclusion,
                    classification=observed.classification,
                    reason=observed.reason,
                    detail=observed.details,
                )
                for name in self.required.names
            )
        details = observed.details.get("receipt") or observed.details
        runs = {run["check_suite_id"]: run for run in details["workflow_runs"]}
        rows = []
        for check in details["checks"]:
            run = runs.get(check["check_suite_id"])
            conclusion = check["conclusion"]
            rows.append(
                _check(
                    self.required, head, check["name"], now,
                    conclusion=_HOSTED_CONCLUSIONS.get(conclusion, Conclusion.UNAVAILABLE),
                    classification=observed.classification,
                    reason=None if conclusion in _HOSTED_CONCLUSIONS else conclusion,
                    run_id=str(run["workflow_run_id"]) if run else "",
                    workflow_id=f"suite:{check['check_suite_id']}",
                    attempt=run["run_attempt"] if run else 0,
                    run_url=check.get("job_url") or (self._run_url(run) if run else None),
                    detail={
                        key: check[key]
                        for key in ("check_run_id", "check_suite_id", "failing_test_ids")
                        if key in check
                    } | ({"workflow_conclusion": run["conclusion"]} if run else {}),
                )
            )
        return tuple(rows)


@dataclass(frozen=True)
class _CommitOwner:
    """Local jobs are owned by the exact commit, not by a subject or policy."""

    id: str
    project_id: str
    policy: None = None


_LOCAL_CONCLUSIONS = {
    CIState.GREEN: Conclusion.SUCCESS,
    CIState.RED: Conclusion.FAILURE,
    CIState.PENDING: Conclusion.PENDING,
}


class LocalChecks:
    """Per-command rows from the detached-snapshot ``LocalCIProducer`` jobs.

    The required check names label the plan's commands; every job is keyed by the
    exact head and plan, and its result must name the same snapshot SHA. A
    development target's own validation jobs are never requested, so there is no
    target baseline to compare against and nothing to re-run for an unmoved head.
    """

    compares_targets = False

    def __init__(
        self, producer: LocalCIProducer, *, project_id: str,
        names: tuple[str, ...] | None = None, version: str | None = None,
        scope: str = "development",
    ) -> None:
        """*names* label the plan's commands, one per command, in order.

        A policy-selected local runner passes the boundary's required check
        names and version, so its rows are the same named checks a hosted
        runner would report. A development source names each check after its
        command, under the pinned artifact's version.
        """
        self.producer, self.project_id = producer, project_id
        plan = producer.plan
        if names is not None and len(names) != len(plan.commands):
            raise ValueError("local check names must label every plan command")
        self.names = plan.commands if names is None else tuple(names)
        self.required = RequiredChecks(
            version=version or plan.version, names=self.names,
            producer_id=local_checks_producer_id(
                scope=scope, names=self.names, version=version or plan.version,
                commands=plan.commands, queue_seconds=plan.queue_seconds,
                run_seconds=plan.run_seconds,
            ),
        )

    def _owner(self, head: HeadIdentity) -> _CommitOwner:
        return _CommitOwner(
            id="checks:" + digest([head.repository_id, head.sha, self.required.producer_id]),
            project_id=self.project_id,
        )

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        return await self.producer.request(self._owner(head), head)

    async def rerequest(self, head: HeadIdentity, suites: tuple[int, ...]) -> tuple[int, ...]:
        """Nothing to re-run: a local job is keyed to its exact head and plan."""
        return ()

    async def observe(self, head: HeadIdentity, *, now: float) -> tuple[CommitCheck, ...]:
        plan = self.producer.plan
        rows = []
        states = await self.producer.job_states(self._owner(head), head)
        for name, (_command, job, (state, classification, _, reason)) in zip(
            self.names, states, strict=True
        ):
            conclusion = _LOCAL_CONCLUSIONS.get(
                state,
                Conclusion.CANCELLED if classification == "cancelled" else Conclusion.UNAVAILABLE,
            )
            rows.append(
                _check(
                    self.required, head, name, now,
                    conclusion=conclusion,
                    classification=classification,
                    reason=reason or (None if job else "not_requested"),
                    run_id=job["id"] if job else "",
                    workflow_id=f"local:{plan.attempt_id}",
                    attempt=1 if job else 0,
                )
            )
        return tuple(rows)


_KEY = ("repository_id", "sha", "check_name", "producer_id")


def _suite_id(check: CommitCheck) -> int | None:
    """The check suite this cached row belongs to, when its producer named one."""
    detail = check.detail.get("check_suite_id")
    if isinstance(detail, bool) or not isinstance(detail, int) or detail <= 0:
        return None
    return detail


class ExactChecks:
    """Read, refresh and request the cached required checks of exact commits."""

    def __init__(
        self,
        db: Any,
        provider: ChecksProvider,
        *,
        poll_seconds: float = 60,
        retry_seconds: float = 300,
        missing_push_seconds: float = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if missing_push_seconds <= 0:
            raise ValueError("missing push grace period must be positive")
        self.db, self.provider, self.clock = db, provider, clock
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.missing_push_seconds = missing_push_seconds

    @property
    def required(self) -> RequiredChecks:
        return self.provider.required

    @property
    def compares_targets(self) -> bool:
        """Whether a target commit's own checks are an available baseline."""
        return bool(getattr(self.provider, "compares_targets", False))

    async def read(self, head: HeadIdentity) -> ChecksResult:
        """The cached verdict for *head*, without contacting the provider."""
        table = integration_check_evidence
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(table).where(
                        table.c.repository_id == head.repository_id,
                        table.c.sha == head.sha,
                        table.c.producer_id == self.required.producer_id,
                        table.c.required_check_version == self.required.version,
                        table.c.check_name.in_(self.required.names),
                    )
                )
            ).mappings().all()
        return evaluate(self.required, head, map(_from_row, rows), now=self.clock())

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        return await self.provider.request(head)

    async def rerequest(self, head: HeadIdentity, names: Sequence[str]) -> tuple[int, ...]:
        """Re-run the named required checks of this exact head, outside every lock.

        Only rows this cache already holds for ``head`` name the suites, so the
        re-request cannot reach another commit, repository or check, and it
        returns what it actually addressed rather than what it intended.
        """
        wanted = set(names)
        rows = (await self.read(head)).checks
        suites = sorted({
            suite
            for check in rows if check.name in wanted
            for suite in [_suite_id(check)]
            if suite is not None
        })
        return await self.provider.rerequest(head, tuple(suites))

    async def refresh(self, head: HeadIdentity) -> ChecksResult:
        """Observe the provider, overwrite each check's row, and return the verdict.

        The observation starts before the write, so a refresh that began
        later always wins over an older one that finishes after it.
        """
        now = self.clock()
        previous = (
            {check.name: check for check in (await self.read(head)).checks}
            if isinstance(self.provider, HostedChecks) else {}
        )
        observed = await self.provider.observe(head, now=now)
        if isinstance(self.provider, HostedChecks):
            observed = await self._bound_missing_push(head, observed, previous, now)
        required = set(self.required.names)
        rows = [
            check.model_copy(update={"due_at": self._due(check, now)})
            for check in observed
            if check.name in required
        ]
        if rows:
            async with self.db._engine.begin() as conn:
                for check in rows:
                    await conn.execute(_upsert(check))
        return await self.read(head)

    async def refresh_if_due(self, head: HeadIdentity) -> ChecksResult:
        """Reuse unfinished evidence until due; final checks get a fresh observation.

        Explicit refresh remains available for reruns and publication proofs.
        Final PR checks must be refreshed to detect reruns on an unchanged head.
        """
        cached = await self.read(head)
        if cached.due_at is not None and self.clock() < cached.due_at:
            return cached
        return await self.refresh(head)

    async def _bound_missing_push(self, head, observed, previous, now):
        """Retain the grace period in the exact-head cache across visits/restarts.

        Missing runs are infrastructure, never conclusive failed checks. A
        later real push run replaces this diagnostic through the normal refresh.
        """
        rows, diagnostic = [], None
        for check in observed:
            old = previous.get(check.name)
            since = old.detail.get("missing_push_since") if old else None
            if not check.detail.get("missing_push_run"):
                # Transport failures do not reset a known missing-run deadline.
                if since is not None and check.conclusion is Conclusion.UNAVAILABLE:
                    check = check.model_copy(update={
                        "detail": {**check.detail, "missing_push_since": since},
                    })
                rows.append(check)
                continue
            since = now if since is None else since
            detail = {**check.detail, "missing_push_since": since}
            values = {"detail": detail}
            if now - since >= self.missing_push_seconds:
                if diagnostic is None:
                    diagnostic = {}
                    if self.provider.diagnose is not None:
                        try:
                            diagnostic = await self.provider.diagnose(head)
                        except (GitError, OSError, ValueError) as exc:
                            diagnostic = {"reason": f"workflow inspection unavailable: {exc}"}
                reason = (f"No push workflow run for exact head {head.sha} on {head.ref} "
                          f"after {self.missing_push_seconds:g} seconds.")
                if diagnostic.get("reason"):
                    reason += " " + diagnostic["reason"]
                values.update(
                    conclusion=Conclusion.UNAVAILABLE, classification="ci_not_triggered",
                    reason=reason, detail={**detail, **diagnostic},
                )
            rows.append(check.model_copy(update=values))
        return tuple(rows)

    def _due(self, check: CommitCheck, now: float) -> float | None:
        if check.conclusion in FINAL:
            return None
        if check.conclusion is Conclusion.PENDING:
            return now + self.poll_seconds
        return now + self.retry_seconds


class RequestedChecks(ExactChecks):
    """Request before every refresh, for a consumer that only refreshes.

    The root PR gate refreshes a member head's checks and never requests them:
    a hosted head's push requests its own. A local head's jobs exist only once
    requested, and its sequential plan advances one request at a time.
    """

    async def refresh(self, head: HeadIdentity) -> ChecksResult:
        await self.request(head)
        return await super().refresh(head)


class HybridChecks:
    """Require both local and hosted exact-head evidence without merging caches.

    The projected trust binds both producers. For each name the
    worse verdict wins, so neither green runner masks the other's failure or
    absence. Hosted workflow failures without a failed job still block.
    """

    compares_targets = False

    def __init__(self, local: ExactChecks, hosted: ExactChecks | None, *,
                 hosted_producer_id: str = "unavailable") -> None:
        if hosted is not None and (local.required.names != hosted.required.names
                or local.required.version != hosted.required.version):
            raise ValueError("hybrid runners must require the same checks and version")
        self.local, self.hosted = local, hosted
        self.provider = local.provider
        self.required = local.required.model_copy(update={
            "producer_id": hybrid_checks_producer_id(local.required.producer_id,
                hosted.required.producer_id if hosted is not None else hosted_producer_id),
        })

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        if self.hosted is not None:
            await self.hosted.request(head)
        return await self.local.request(head)

    async def rerequest(self, head: HeadIdentity, names: Sequence[str]) -> tuple[int, ...]:
        return await self.hosted.rerequest(head, names) if self.hosted is not None else ()

    async def refresh(self, head: HeadIdentity) -> ChecksResult:
        await self.local.refresh(head)
        if self.hosted is not None:
            await self.hosted.refresh(head)
        return await self.read(head)

    async def read(self, head: HeadIdentity) -> ChecksResult:
        local = await self.local.read(head)
        hosted = (await self.hosted.read(head) if self.hosted is not None
                  else evaluate(self.required, head, (), now=self.local.clock()))
        rank = {Conclusion.FAILURE: 0, Conclusion.MISSING: 0, Conclusion.UNAVAILABLE: 1,
                Conclusion.CANCELLED: 1, Conclusion.PENDING: 2, Conclusion.SUCCESS: 3}
        checks = tuple(min(pair, key=lambda check: rank[check.conclusion]).model_copy(
            update={"producer_id": self.required.producer_id})
            for pair in zip(local.checks, hosted.checks, strict=True))
        states = (ChecksState.RED, ChecksState.UNKNOWN, ChecksState.PENDING, ChecksState.GREEN)
        state = next(state for state in states if state in {local.state, hosted.state})
        due = [result.due_at for result in (local, hosted) if result.due_at is not None]
        return ChecksResult(repository_id=head.repository_id, sha=head.sha,
                            required=self.required, state=state, checks=checks,
                            due_at=None if state is ChecksState.RED or not due else min(due))


def _upsert(check: CommitCheck):
    table = integration_check_evidence
    values = {
        "id": "check:" + digest([check.repository_id, check.sha, check.name, check.producer_id]),
        "repository_id": check.repository_id,
        "sha": check.sha,
        "check_name": check.name,
        "producer_id": check.producer_id,
        "required_check_version": check.required_check_version,
        "workflow_id": check.workflow_id,
        "run_id": check.run_id,
        "attempt": check.attempt,
        "run_url": check.run_url,
        "conclusion": check.conclusion.value,
        "classification": check.classification,
        "checks": {"reason": check.reason, **check.detail},
        "observed_at": check.observed_at,
        "due_at": check.due_at,
    }
    statement = insert(table).values(**values)
    excluded = statement.excluded
    return statement.on_conflict_do_update(
        index_elements=list(_KEY),
        index_where=table.c.sha.isnot(None),
        set_={key: excluded[key] for key in values if key not in {"id", *_KEY}},
        # Never let an older observation, or an earlier attempt of the same
        # run, overwrite a newer one.
        where=and_(
            excluded.observed_at >= table.c.observed_at,
            not_(
                and_(
                    excluded.run_id != "",
                    excluded.run_id == table.c.run_id,
                    excluded.attempt < table.c.attempt,
                )
            ),
        ),
    )


def _from_row(row: Any) -> CommitCheck:
    detail = dict(row["checks"] or {})
    reason = detail.pop("reason", None)
    return CommitCheck(
        repository_id=row["repository_id"],
        sha=row["sha"],
        name=row["check_name"],
        producer_id=row["producer_id"],
        required_check_version=row["required_check_version"],
        conclusion=Conclusion(row["conclusion"]),
        classification=row["classification"],
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        attempt=row["attempt"],
        run_url=row["run_url"],
        reason=reason,
        detail=detail,
        observed_at=row["observed_at"],
        due_at=row["due_at"],
    )


__all__ = [
    "ChecksProvider",
    "ChecksResult",
    "ChecksState",
    "CommitCheck",
    "Conclusion",
    "ExactChecks",
    "HostedChecks",
    "HybridChecks",
    "LocalChecks",
    "RequiredChecks",
    "evaluate",
    "hybrid_checks_producer_id",
    "local_checks_producer_id",
]
