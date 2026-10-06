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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, not_, select
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import integration_check_evidence
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
    if conclusions & {Conclusion.FAILURE, Conclusion.MISSING}:
        state = ChecksState.RED
    elif conclusions & {Conclusion.UNAVAILABLE, Conclusion.CANCELLED}:
        state = ChecksState.UNKNOWN
    elif Conclusion.PENDING in conclusions:
        state = ChecksState.PENDING
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
    # No workflow yet: the candidate push triggers one.
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
    batch candidate under ``aq/batches/**``) triggers the workflows, so there
    is nothing to request.
    """

    compares_targets = True

    def __init__(self, producer: HostedCIProducer) -> None:
        self.producer = producer
        self.required = RequiredChecks.from_trust(producer.trust)

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
                )
                for name in self.required.names
            )
        details = observed.details.get("receipt") or observed.details
        runs = {run["check_suite_id"]: run for run in details["workflow_runs"]}
        # A cancelled sibling is the job's own account of what happened: the run
        # conclusion GitHub derives from it says nothing about this check.
        cancelled = any(check["conclusion"] == "cancelled" for check in details["checks"])
        rows = []
        for check in details["checks"]:
            run = runs.get(check["check_suite_id"])
            conclusion = check["conclusion"]
            # A required check passes only within a successful workflow attempt.
            if (
                conclusion == "success"
                and not cancelled
                and run is not None
                and run["conclusion"] != "success"
            ):
                conclusion = run["conclusion"]
            rows.append(
                _check(
                    self.required, head, check["name"], now,
                    conclusion=_HOSTED_CONCLUSIONS.get(conclusion, Conclusion.UNAVAILABLE),
                    classification=observed.classification,
                    reason=None if conclusion in _HOSTED_CONCLUSIONS else conclusion,
                    run_id=str(run["workflow_run_id"]) if run else "",
                    workflow_id=f"suite:{check['check_suite_id']}",
                    attempt=run["run_attempt"] if run else 0,
                    run_url=self._run_url(run) if run else None,
                    detail={
                        key: check[key]
                        for key in ("check_run_id", "check_suite_id")
                        if key in check
                    },
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

    The required check names are the plan's commands; every job is keyed by the
    exact head and plan, and its result must name the same snapshot SHA. A
    development target's own validation jobs are never requested, so there is no
    target baseline to compare against and nothing to re-run for an unmoved head.
    """

    compares_targets = False

    def __init__(self, producer: LocalCIProducer, *, project_id: str) -> None:
        self.producer, self.project_id = producer, project_id
        plan = producer.plan
        self.required = RequiredChecks(
            version=plan.version, names=plan.commands, producer_id="local-jobs"
        )

    def _owner(self, head: HeadIdentity) -> _CommitOwner:
        return _CommitOwner(
            id="checks:" + digest([head.repository_id, head.sha]), project_id=self.project_id
        )

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        return await self.producer.request(self._owner(head), head)

    async def rerequest(self, head: HeadIdentity, suites: tuple[int, ...]) -> tuple[int, ...]:
        """Nothing to re-run: a local job is keyed to its exact head and plan."""
        return ()

    async def observe(self, head: HeadIdentity, *, now: float) -> tuple[CommitCheck, ...]:
        plan = self.producer.plan
        rows = []
        for command, job, (state, classification, _, reason) in await self.producer.job_states(
            self._owner(head), head
        ):
            conclusion = _LOCAL_CONCLUSIONS.get(
                state,
                Conclusion.CANCELLED if classification == "cancelled" else Conclusion.UNAVAILABLE,
            )
            rows.append(
                _check(
                    self.required, head, command, now,
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.provider, self.clock = db, provider, clock
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds

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
        observed = await self.provider.observe(head, now=now)
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

    def _due(self, check: CommitCheck, now: float) -> float | None:
        if check.conclusion in FINAL:
            return None
        if check.conclusion is Conclusion.PENDING:
            return now + self.poll_seconds
        return now + self.retry_seconds


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
    "LocalChecks",
    "RequiredChecks",
    "evaluate",
]
