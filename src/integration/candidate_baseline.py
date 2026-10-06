"""The target's own checks are the baseline a red candidate is judged against.

A red candidate is not automatically the batch's fault.  A required check that
also fails on the target commit the candidate was built on is a pre-existing
failure: no change in this batch can turn it green, so a repair filed for it spends
an attempt and a worker on work the target already owed.  Only a failing check the
target does not fail is repairable.

The target's verdict for its exact sha comes from the same per-commit check cache
as the candidate's, so the comparison is over rows written by one trusted producer
under one required-check version.  A target with no final verdict for its sha
cannot decide anything: the baseline is requested and observed once before it is
classified, and checks it has still not decided are unproven rather than assumed
to be the batch's.

A required check marked MISSING on the target is not an observed failure. The
target may never have run the required workflow, as with a hand-pushed main whose
only push workflow is attestation. Such a target has no comparable baseline, so
the candidate's failing checks remain repairable. Missing candidate checks still
fail the required set, and publication still requires trusted exact-candidate
success.

An unproven or pre-existing failure is never repaired.  The candidate's failing
check suites are re-requested under bounded exponential backoff, exactly as
source-CI infrastructure handling is, and the bound names the
``candidate_pre_existing_failure`` blocker for a human instead of filing repairs
forever.  The counters are durable per exact candidate, so a repaired or moved
candidate starts from zero, like its repair lineage.

Comparison is a property of the checks provider and the exact target's evidence,
not of the batch: hosted checks can compare targets that ran the required checks,
while local validation jobs only ever exist for candidates. A lane whose provider
cannot compare a target keeps filing repairs exactly as before.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from src.database.tables import integration_batches
from src.integration.batches import candidate_ref
from src.integration.checks import ChecksResult, ChecksState, Conclusion, ExactChecks
from src.integration.subjects import HeadIdentity

logger = logging.getLogger(__name__)

#: The conclusions that make a candidate's required check red. ``MISSING`` fails
#: the candidate's required set, but cannot establish a pre-existing target failure.
RED_CONCLUSIONS = frozenset({Conclusion.FAILURE, Conclusion.MISSING})

#: Bounded re-requests of one exact candidate's checks, then a named blocker.
#: Mirrors the source-CI infrastructure bound
#: (``root.repair.source_ci_infra_*``); it is deliberately mechanism-side, because
#: a target that cannot be compared must not depend on a policy edit to be noticed.
BASELINE_ATTEMPTS = 3
BASELINE_BACKOFF_SECONDS = 300.0
BASELINE_BACKOFF_MAX_SECONDS = 3600.0

#: The blocker a spent bound names instead of filing another repair.
BASELINE_BLOCKER = "candidate_pre_existing_failure"


def failing(result: ChecksResult) -> tuple[str, ...]:
    """The required checks that failed outright on this exact head."""
    return tuple(check.name for check in result.checks if check.conclusion in RED_CONCLUSIONS)


@dataclass(frozen=True)
class Baseline:
    """How the target's own verdict classifies a red candidate's failures.

    ``unavailable`` means the provider cannot compare the target, or a required
    target check is missing, so no claim is made about the candidate: every
    failure stays repairable. Otherwise
    each failing check is ``repairable`` (the target succeeded it),
    ``pre_existing`` (the target failed it too) or ``unproven`` (the target has
    not decided it).
    """

    target_sha: str | None
    target_state: str
    repairable: tuple[str, ...]
    pre_existing: tuple[str, ...]
    unproven: tuple[str, ...]
    unavailable: bool = False
    missing_target_checks: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if self.unavailable:
            return "unavailable"
        if self.repairable:
            return "repairable"
        return "pre_existing" if not self.unproven else "unproven"

    @property
    def repair(self) -> bool:
        """Whether a repair is filed for this visit."""
        return self.unavailable or bool(self.repairable)

    @property
    def failing(self) -> tuple[str, ...]:
        """The failing checks this batch is asked to re-request instead of repair."""
        return tuple(sorted({*self.pre_existing, *self.unproven}))

    def detail(self) -> dict[str, Any]:
        return {
            "baseline": {
                "state": self.state,
                "target_sha": self.target_sha,
                "target_checks": self.target_state,
                "repairable_checks": list(self.repairable),
                "pre_existing_checks": list(self.pre_existing),
                "unproven_checks": list(self.unproven),
                **({
                    "reason": "target_required_checks_missing",
                    "missing_target_checks": list(self.missing_target_checks),
                } if self.missing_target_checks else {}),
            },
        }


def compare(candidate: ChecksResult, target: ChecksResult) -> Baseline:
    """Classify the candidate's failing checks against the target's own verdict.

    Pure, and comparable only because both results were produced by one trusted
    producer under one required-check version.
    """
    missing = tuple(sorted(
        check.name for check in target.checks
        if check.name in target.required.names and check.conclusion is Conclusion.MISSING
    ))
    if missing:
        # A completed push suite can establish absence without having run the
        # required workflow. Do not infer that the target failed those checks.
        return Baseline(
            target_sha=target.sha,
            target_state=str(target.state),
            repairable=tuple(sorted(failing(candidate))),
            pre_existing=(),
            unproven=(),
            unavailable=True,
            missing_target_checks=missing,
        )
    rows = {check.name: check for check in target.checks}
    repairable, pre_existing, unproven = [], [], []
    for name in failing(candidate):
        row = rows.get(name)
        if row is None:
            unproven.append(name)
        elif row.conclusion is Conclusion.FAILURE:
            pre_existing.append(name)
        elif row.conclusion is Conclusion.SUCCESS:
            repairable.append(name)
        else:
            # Pending, cancelled, unavailable or never observed: the target has
            # not decided this check, so it is not the batch's failure to fix.
            unproven.append(name)
    return Baseline(
        target_sha=target.sha,
        target_state=str(target.state),
        repairable=tuple(sorted(repairable)),
        pre_existing=tuple(sorted(pre_existing)),
        unproven=tuple(sorted(unproven)),
    )


def candidate_head(batch, candidate_sha: str) -> HeadIdentity:
    """The exact candidate commit whose required checks are compared."""
    return HeadIdentity(
        repository_id=batch.repository_id,
        ref=candidate_ref(batch.id),
        sha=candidate_sha,
        generation=batch.repair_attempt_count,
    )


def target_head(batch, target_sha: str) -> HeadIdentity:
    """The exact target commit a candidate was built on, in the candidate's lane."""
    return HeadIdentity(
        repository_id=batch.repository_id,
        ref=batch.target_ref,
        sha=target_sha,
        generation=batch.repair_attempt_count,
    )


async def target_baseline(
    batch, *, candidate: ChecksResult, target_sha: str | None, checks: ExactChecks | None
) -> Baseline:
    """The target's own verdict for its exact sha, obtained once when undecided."""
    red = failing(candidate)
    if not target_sha or checks is None or not checks.compares_targets:
        return Baseline(None, "unavailable", red, (), (), unavailable=True)
    head = target_head(batch, target_sha)
    result = await checks.read(head)
    if result.state not in (ChecksState.GREEN, ChecksState.RED):
        # The target has no verdict for this sha yet: ask for one run and observe
        # it, exactly as a candidate's own checks are obtained. Provider I/O stays
        # outside every lock.
        await checks.request(head)
        result = await checks.refresh(head)
    return compare(candidate, result)


class UnrecordedBaseline:
    """The comparison without its durable bound, for a train built without one.

    Every failure stays repairable, so nothing about the previous behaviour
    changes on a train that was not given a baseline service.
    """

    async def verdict(
        self, batch, *, candidate: ChecksResult, target_sha: str | None,
        checks: ExactChecks | None,
    ) -> Baseline:
        return Baseline(None, "unavailable", failing(candidate), (), (), unavailable=True)

    async def re_request(
        self, batch, *, candidate: ChecksResult, baseline: Baseline, checks: ExactChecks
    ) -> dict[str, Any]:
        return {"outcome": "unavailable", "reason": "baseline_unrecorded",
                "observations": 0, "reruns": 0, "due_at": None}


class CandidateBaselineService:
    """The comparison plus its durable bounded re-request bookkeeping.

    The provider observation happens before any transaction opens and the
    re-request is issued after it closes, so a slow provider never holds the batch
    row lock that the publication fence and the repair allocator also take first.
    """

    def __init__(
        self, db, *, clock=time.time, attempts: int = BASELINE_ATTEMPTS,
        backoff_seconds: float = BASELINE_BACKOFF_SECONDS,
        backoff_max_seconds: float = BASELINE_BACKOFF_MAX_SECONDS,
    ) -> None:
        if attempts <= 0 or backoff_seconds <= 0 or backoff_max_seconds < backoff_seconds:
            raise ValueError("baseline bound must be positive and ordered")
        self.db, self.clock = db, clock
        self.attempts = attempts
        self.backoff_seconds, self.backoff_max_seconds = backoff_seconds, backoff_max_seconds

    async def verdict(
        self, batch, *, candidate: ChecksResult, target_sha: str | None,
        checks: ExactChecks | None,
    ) -> Baseline:
        return await target_baseline(
            batch, candidate=candidate, target_sha=target_sha, checks=checks
        )

    async def re_request(
        self, batch, *, candidate: ChecksResult, baseline: Baseline, checks: ExactChecks,
    ) -> dict[str, Any]:
        """Count one observation of an unrepairable red and re-run its suites if due."""
        decision = await self._record(batch.id, candidate.sha, batch.repair_attempt_count,
                                      self.clock())
        if decision["outcome"] != "requested":
            if decision.get("blocker"):
                logger.warning(
                    "integration batch %s candidate %s: %s after %s consecutive observations",
                    batch.id, candidate.sha[:12], decision["blocker"], decision["observations"],
                )
            return decision
        suites = await checks.rerequest(candidate_head(batch, candidate.sha), baseline.failing)
        return {**decision, "check_suites": list(suites)}

    async def _record(
        self, batch_id: str, candidate_sha: str, generation: int, now: float
    ) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            row = (await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == batch_id,
                    integration_batches.c.target_ref.is_not(None),
                ).with_for_update()
            )).mappings().one_or_none()
            if row is None:
                return {"outcome": "unavailable", "reason": "missing_batch",
                        "observations": 0, "reruns": 0, "due_at": None}
            # The counters describe one exact candidate at one repair generation; a
            # repaired or moved head is a new identity and starts from zero.
            same = (row["baseline_candidate_sha"] == candidate_sha
                    and row["baseline_generation"] == generation)
            observations = (row["baseline_observations"] if same else 0) + 1
            reruns = row["baseline_reruns"] if same else 0
            due_at = row["baseline_rerun_at"] if same else None
            values: dict[str, Any] = {
                "baseline_candidate_sha": candidate_sha, "baseline_generation": generation,
                "baseline_observations": observations,
            }
            if observations >= self.attempts:
                values["baseline_rerun_at"] = None
                outcome = {"outcome": "blocked", "blocker": BASELINE_BLOCKER}
            elif due_at is not None and now < due_at:
                outcome = {"outcome": "waiting", "due_at": due_at}
            else:
                values["baseline_reruns"] = reruns + 1
                values["baseline_rerun_at"] = now + min(
                    self.backoff_seconds * (2 ** reruns), self.backoff_max_seconds
                )
                outcome = {"outcome": "requested", "due_at": values["baseline_rerun_at"]}
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch_id,
                integration_batches.c.target_ref.is_not(None),
            ).values(**values))
            return {**outcome, "observations": observations, "reruns": reruns,
                    "due_at": values.get("baseline_rerun_at", due_at)}


def red_brief(baseline: Baseline, candidate_sha: str) -> str:
    """Plain-English scope for a repair that owns only this batch's own failures.

    Empty when no comparison was possible: a repair filed without a baseline
    knows nothing about the target, so it must not claim it owns one check and
    not another.
    """
    if baseline.unavailable:
        return ""
    lines = [
        (f"Required checks are red on candidate {candidate_sha}, built on target "
         f"{baseline.target_sha}."),
        "Repair these failing required checks, which the target does not also fail:",
        *(f"  - {name}" for name in baseline.repairable),
    ]
    if baseline.pre_existing:
        lines += [
            ("These failing required checks also fail on the target commit above. They are "
             "pre-existing failures this repair does not own: no change in this batch can turn "
             "them green, so do not spend the repair on them."),
            *(f"  - {name}" for name in baseline.pre_existing),
        ]
    if baseline.unproven:
        lines += [
            ("These failing required checks have no final verdict on the target commit above, "
             "so nothing is attributed to this batch for them either:"),
            *(f"  - {name}" for name in baseline.unproven),
        ]
    return "\n".join(lines)


__all__ = [
    "BASELINE_ATTEMPTS",
    "BASELINE_BACKOFF_MAX_SECONDS",
    "BASELINE_BACKOFF_SECONDS",
    "BASELINE_BLOCKER",
    "RED_CONCLUSIONS",
    "Baseline",
    "CandidateBaselineService",
    "UnrecordedBaseline",
    "candidate_head",
    "compare",
    "failing",
    "red_brief",
    "target_baseline",
    "target_head",
]
