"""The reduced integration train: level-triggered visits over Git, checks and intent.

Under ``integration.git_first: active`` one :class:`IntegrationTrain` takes the
place of the root, parent and development subject runtimes. It is a reduction
of :class:`~src.integration.reconciler.IntegrationReconciler`, not a second
engine: every visit derives progress from one fetched Git snapshot, the cached
checks of the exact candidate commit and the batch's durable intent, so a
restart or a missed notification is repaired by the next visit. There is no
journal, generation or receipt to replay.

Each target (project, repository, target ref) is visited by its own task. A
tick starts a visit for every target whose previous visit has finished and
never awaits another target, so a slow check, fetch or merge on one target
cannot stall the others. Checks are requested and refreshed outside every
lock; the batch gate reads only the cached verdict.

A red candidate is refreshed before a repair is allocated: a green observation
ends the repair with no attempt counted. A red candidate is then measured against
the target commit it was built on, and only a failure the target does not also
fail reaches the repair; a pre-existing or undecided failure is never repaired,
because a repair for it spends an attempt and a worker on work the batch cannot
do. Such a candidate's own suites are re-requested under bounded backoff, and the
bound ends at a named blocker instead of an endless repair chain. The allocator
counts attempts when it files, never per visit. A merge conflict files one
ordinary repair whose brief names the conflicting member and every member still to
be merged, because a repaired head that lacks one of them lets the target advance
without it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.integration.batches import (
    Batch,
    BatchMember,
    BatchObservation,
    BatchService,
    candidate_ref,
)
from src.integration.candidate_baseline import (
    CandidateBaselineService,
    UnrecordedBaseline,
    red_brief,
)
from src.integration.checks import ChecksResult, ChecksState, ExactChecks
from src.integration.git_truth import GitTruthSnapshot
from src.integration.subjects import HeadIdentity

logger = logging.getLogger(__name__)

TRAIN_KINDS = ("root", "epic", "development")
_PROGRESS: ContextVar[dict | None] = ContextVar("train_visit_progress", default=None)


def _progress(stage: str, **facts) -> None:
    progress = _PROGRESS.get()
    if progress is not None:
        progress.update(stage=stage, **facts)

#: The repository's own regeneration sweep for ``merge=aq-generated`` paths.
REGENERATION_COMMAND = "scripts/regenerate-generated.sh"


@dataclass(frozen=True)
class TrainTarget:
    """One branch the train publishes to; the unit of isolation."""

    project_id: str
    repository_id: str
    target_ref: str
    kind: str = "root"

    def __post_init__(self) -> None:
        if not (self.project_id and self.repository_id and self.target_ref):
            raise ValueError("train target needs project, repository and ref")
        if not self.target_ref.startswith("refs/heads/"):
            raise ValueError("train target must be a fully qualified branch ref")
        if self.kind not in TRAIN_KINDS:
            raise ValueError(f"unknown train target kind: {self.kind}")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.project_id, self.repository_id, self.target_ref)


@dataclass(frozen=True)
class TrainVisit:
    """What one visit observed and did; a projection, never an input."""

    target: TrainTarget
    state: str
    batch_id: str | None = None
    candidate_sha: str | None = None
    target_sha: str | None = None
    tree_sha: str | None = None
    checks: str | None = None
    repair: dict | None = None
    detail: dict | None = None
    observed_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.target.project_id,
            "repository_id": self.target.repository_id,
            "target_ref": self.target.target_ref,
            "kind": self.target.kind,
            "state": self.state,
            "batch_id": self.batch_id,
            "candidate_sha": self.candidate_sha,
            "target_sha": self.target_sha,
            "tree_sha": self.tree_sha,
            "checks": self.checks,
            "repair": self.repair,
            "detail": self.detail,
            "observed_at": self.observed_at,
        }


class TargetSource(Protocol):
    async def targets(self, now: float) -> Sequence[TrainTarget]:
        """Every target the train owns now; read-only."""


class BatchSource(Protocol):
    async def open_batch(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, service: BatchService
    ) -> BatchSelection:
        """The target's open batch with its frozen members, freezing one when due.

        A new batch freezes through ``service.freeze``, which retains every
        exact completion source before the membership becomes immutable.
        """

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        """Record that the target contains the batch; idempotent."""


class RepairAllocator(Protocol):
    async def allocate(
        self, batch_id: str, *, target_ref: str, head_sha: str,
        green_sha: str | None = None, held: bool = False, review_rejected: bool = False,
        authorize: Callable[[], Awaitable[bool]] | None = None,
        brief: str = "",
    ) -> dict:
        """File (or find) the ordinary repair task for a batch's candidate ref.

        :class:`~src.integration.repair.OrdinaryRepairService` is the production
        allocator.
        """


def conflict_brief(
    detail: Mapping[str, Any] | None, members: Sequence[BatchMember], *, starting_sha: str,
) -> str:
    """Plain-English instructions for the repair of one batch merge conflict.

    ``detail`` is the exact ``merge_sources`` conflict result: the partial head
    it reached (``head``), the members it merged into that head (``members``),
    the member whose merge conflicted (``member``), the conflicting ``files``
    and a ``reason``. Only a starting head that *is* that partial head carries
    the members the merge already completed; from any other start every member
    is still owed to the target.

    The repair exists to land every remaining member, not to make the starting
    head green: a repaired head published without them lets the train
    fast-forward the target, and those members conflict again in the next
    batch. A raw detail dict is never an instruction, so nothing else reaches
    the worker's description.
    """
    detail = dict(detail or {})
    rows = [row for row in (detail.get("members") or ()) if isinstance(row, Mapping)]
    carried = ({str(row.get("member")) for row in rows}
               if detail.get("head") == starting_sha else set())
    merged = [member for member in members if member.task_id in carried]
    remaining = [member for member in members if member.task_id not in carried]
    conflict = str(detail.get("member") or "")
    source = next((member.source_sha for member in members if member.task_id == conflict), "")
    reason = str(detail.get("reason") or "") if detail.get("reason") is not None else ""

    def numbered(items: Sequence[BatchMember]) -> list[str]:
        return [f"  {index}. {member.task_id} (source {member.source_sha})"
                for index, member in enumerate(items, 1)]

    files = [str(name) for name in (detail.get("files") or ())]
    lines = [
        f"The batch merge conflicted while building the starting head {starting_sha}.",
        (f"Conflicting member: {conflict}" + (f" (source {source})" if source else "")
         + (f"; reason: {reason}" if reason else "") + ".") if conflict
        else (f"Conflicting merge; reason: {reason or 'not reported by the merge'}."),
        "Conflicting files:" if files else "Conflicting files: not reported by the merge.",
        *(f"  - {name}" for name in files),
        ("Already merged into the starting head (do not merge them again):" if merged
         else "Nothing is merged into the starting head yet."),
        *numbered(merged),
        "Still to merge onto the starting head, in this order:" if remaining
        else "Every frozen member is already merged into the starting head.",
        *numbered(remaining),
        (f"Resolve the conflict, then merge every member listed above onto the "
         f"starting head {starting_sha} in that order. Do not stop once the "
         "conflict is resolved, or once the checks on the starting head are "
         "green: a repaired head published without every remaining member lets "
         "the train fast-forward the target without them, and they conflict "
         "again in the next batch."),
        (f"Generated files are regenerated, never hand-merged: resolve a conflict "
         f"in a path carrying the merge=aq-generated attribute in .gitattributes by "
         f"running the repository's regeneration command ({REGENERATION_COMMAND} by "
         f"default), never by resolving its conflict markers by hand."),
    ]
    return "\n".join(line for line in lines if line)


def candidate_head(batch: Batch, candidate_sha: str) -> HeadIdentity:
    """The exact head whose checks gate a batch's publication."""
    return HeadIdentity(
        repository_id=batch.repository_id,
        ref=candidate_ref(batch.id),
        sha=candidate_sha,
        generation=batch.repair_attempt_count,
    )


def exact_gate(checks: ExactChecks) -> Callable[[Batch, str, str], Awaitable[bool]]:
    """A batch gate that reads cached exact-SHA checks; no job or network work.

    ``BatchService`` calls its gate inside the publisher's fence lock, so the
    gate must only read what a visit already refreshed.
    """

    async def gate(batch: Batch, candidate_sha: str, tree_sha: str) -> bool:
        return (await checks.read(candidate_head(batch, candidate_sha))).green

    return gate


class CandidateChecks:
    """The exact-head checks of one lane's candidates.

    ``resolve`` builds the checks cache for one candidate, always outside every
    lock: hosted trust may read the candidate's tree. The batch gate runs inside
    the publisher's fence lock, so it reads only a candidate this lane already
    resolved, and an unresolved candidate is not green. ``advisory`` checks
    still run to a verdict, but a red one publishes instead of filing a repair.
    ``resolve`` returns ``None`` when the target requires no checks at all.
    """

    def __init__(
        self, resolve: Callable[[Batch, str], Awaitable[ExactChecks | None]], *,
        limit: int = 64, advisory: bool = False,
    ) -> None:
        self.resolve, self.limit, self.advisory = resolve, limit, advisory
        self._resolved: dict[tuple[str, str], ExactChecks | None] = {}

    @classmethod
    def fixed(cls, checks: ExactChecks) -> CandidateChecks:
        async def resolve(batch: Batch, candidate_sha: str) -> ExactChecks:
            return checks

        return cls(resolve)

    async def for_candidate(self, batch: Batch, candidate_sha: str) -> ExactChecks | None:
        checks = await self.resolve(batch, candidate_sha)
        self._resolved.pop((batch.id, candidate_sha), None)
        self._resolved[(batch.id, candidate_sha)] = checks
        while len(self._resolved) > self.limit:
            self._resolved.pop(next(iter(self._resolved)))
        return checks

    def passes(self, result: ChecksResult) -> bool:
        """Whether a verdict lets publication proceed."""
        return result.green or (self.advisory and result.state == ChecksState.RED)

    async def gate(self, batch: Batch, candidate_sha: str, tree_sha: str) -> bool:
        key = (batch.id, candidate_sha)
        if key not in self._resolved:
            return False
        checks = self._resolved[key]
        return checks is None or self.passes(
            await checks.read(candidate_head(batch, candidate_sha))
        )


@dataclass(frozen=True)
class TrainLane:
    """The per-target mechanism: one batch service and its candidates' checks.

    ``snapshot`` fetches the target once per visit. The service's gate should be
    ``checks.gate`` so publication reads the verdict this visit refreshed.
    """

    snapshot: Callable[[], Awaitable[GitTruthSnapshot]]
    service: BatchService
    checks: CandidateChecks
    complete_epic: Callable[[GitTruthSnapshot], Awaitable[tuple[dict[str, Any], ...]]] | None = None


@dataclass(frozen=True)
class BatchSelection:
    """An open batch and any completions whose evidence prevents admission."""

    batch: Batch | None = None
    members: tuple[BatchMember, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()


@dataclass
class _Lane:
    task: asyncio.Task | None = None
    last: TrainVisit | None = None
    started_at: float = 0.0
    visits: int = 0
    errors: int = 0


class IntegrationTrain:
    """Level-triggered visits, one isolated task per target."""

    def __init__(
        self,
        *,
        targets: TargetSource,
        batches: BatchSource,
        lane_for: Callable[[TrainTarget], Awaitable[TrainLane]],
        repair: RepairAllocator,
        baseline: CandidateBaselineService | None = None,
        visit_timeout_seconds: float = 900.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if visit_timeout_seconds <= 0:
            raise ValueError("visit timeout must be positive")
        self.targets, self.batches, self.lane_for = targets, batches, lane_for
        self.repair, self.clock = repair, clock
        # Without a baseline service the train cannot compare the target, so it
        # claims nothing and repairs a red candidate exactly as before.
        self.baseline = baseline or UnrecordedBaseline()
        self.visit_timeout_seconds = visit_timeout_seconds
        self._lanes: dict[tuple[str, str, str], _Lane] = {}
        self._tick_lock = asyncio.Lock()

    async def tick(self, now: float | None = None) -> dict[str, list[str]]:
        """Start one visit per idle target; never wait on a running one."""
        now = self.clock() if now is None else now
        if self._tick_lock.locked():
            return {"started": [], "running": [], "skipped": ["tick_in_progress"]}
        async with self._tick_lock:
            started, running = [], []
            for target in await self.targets.targets(now):
                lane = self._lanes.setdefault(target.key, _Lane())
                label = "/".join(target.key)
                if lane.task is not None and not lane.task.done():
                    running.append(label)
                    continue
                lane.started_at = now
                lane.task = asyncio.create_task(
                    self._bounded(target, lane), name=f"integration-train:{label}"
                )
                started.append(label)
            return {"started": started, "running": running, "skipped": []}

    async def drain(self) -> None:
        """Wait for every running visit; for tests and orderly shutdown."""
        tasks = [lane.task for lane in self._lanes.values() if lane.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        tasks = [lane.task for lane in self._lanes.values()
                 if lane.task is not None and not lane.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> list[dict[str, Any]]:
        """The latest visit per target, plus whether one is running now."""
        rows = []
        for key, lane in sorted(self._lanes.items()):
            row = lane.last.as_dict() if lane.last else {
                "project_id": key[0], "repository_id": key[1], "target_ref": key[2],
                "state": "unvisited",
            }
            row["running"] = lane.task is not None and not lane.task.done()
            row["visits"], row["errors"] = lane.visits, lane.errors
            rows.append(row)
        return rows

    async def _bounded(self, target: TrainTarget, lane: _Lane) -> None:
        progress = {"stage": "lane_setup"}
        token = _PROGRESS.set(progress)
        try:
            visit = await asyncio.wait_for(self.visit(target), self.visit_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            lane.errors += 1
            visit = TrainVisit(target, "unknown", batch_id=progress.get("batch_id"),
                               candidate_sha=progress.get("candidate_sha"),
                               target_sha=progress.get("target_sha"),
                               detail={"reason": "visit_timeout", **progress,
                                       "timeout_seconds": self.visit_timeout_seconds},
                               observed_at=self.clock())
            logger.warning("integration train visit timed out for %s", target.key)
        except Exception as exc:  # one target's failure never stops the train
            lane.errors += 1
            visit = TrainVisit(target, "unknown", detail={"reason": type(exc).__name__,
                                                          "error": str(exc)[:500]},
                               observed_at=self.clock())
            logger.exception("integration train visit failed for %s", target.key)
        finally:
            _PROGRESS.reset(token)
        lane.visits += 1
        lane.last = visit

    async def visit(self, target: TrainTarget) -> TrainVisit:
        """Observe the target once and take at most one step toward delivery."""
        lane = await self.lane_for(target)
        _progress("fetch_snapshot")
        snapshot = await lane.snapshot()
        _progress("select_batch", target_sha=snapshot.target_oid)
        opened = await self.batches.open_batch(target, snapshot, lane.service)
        if opened.batch is None:
            state = "blocked" if opened.blockers else "idle"
            visit = self._visit(target, state, snapshot=snapshot)
        else:
            visit = await self._visit_batch(target, lane, snapshot, opened.batch, opened.members)
        blockers = opened.blockers
        if target.kind == "epic" and lane.complete_epic and visit.state in {"idle", "delivered"}:
            _progress("settle_epic")
            # Publication moved the ref; readiness must examine the collected
            # head, rather than the snapshot from before the child batch landed.
            if visit.state == "delivered":
                snapshot = await lane.snapshot()
            blockers += await lane.complete_epic(snapshot)
            if blockers and visit.state == "idle":
                visit = replace(visit, state="blocked")
        if blockers:
            visit = replace(visit, detail={
                **(visit.detail or {}), "blockers": list(blockers),
            })
        return visit

    async def _visit_batch(
        self, target: TrainTarget, lane: TrainLane, snapshot: GitTruthSnapshot,
        batch: Batch, members: tuple[BatchMember, ...],
    ) -> TrainVisit:
        _progress("build_or_publish", batch_id=batch.id)
        observation = await lane.service.visit(batch, members, snapshot)
        _progress("observe_candidate", candidate_sha=observation.candidate_sha,
                  target_sha=observation.target_sha)
        if observation.state == "delivered":
            _progress("settle_batch")
            await self.batches.settle(batch, observation)
            return self._visit(target, "delivered", batch, observation)
        if observation.state == "conflict":
            return await self._repair(target, lane, batch, members, observation, None)
        if observation.state != "testing" or not observation.candidate_sha:
            # held, moved, source_moved, unknown, no_regenerator: the next visit
            # observes again. None of these is a member's content conflict, so
            # none allocates a repair.
            return self._visit(target, observation.state, batch, observation)
        head = candidate_head(batch, observation.candidate_sha)
        _progress("resolve_checks")
        checks = await lane.checks.for_candidate(batch, observation.candidate_sha)
        result = None if checks is None else await self._checks(checks, head)
        if result is None or lane.checks.passes(result):
            # The gate now reads this verdict; publish within this visit.
            _progress("publish_candidate")
            published = await lane.service.visit(batch, members, snapshot)
            if published.state == "delivered":
                _progress("settle_batch")
                await self.batches.settle(batch, published)
            return self._visit(target, published.state, batch, published, result)
        if result.state == ChecksState.RED:
            return await self._red(
                target, lane, batch, members, observation, result, head, checks
            )
        missing_push = [check for check in getattr(result, "checks", ())
                        if check.classification == "ci_not_triggered"]
        if missing_push:
            diagnostic = missing_push[0]
            observation = replace(observation, detail={
                **(observation.detail or {}), "ci_not_triggered": {
                    **diagnostic.detail, "reason": diagnostic.reason,
                },
            })
            return await self._repair(target, lane, batch, members, observation, result)
        return self._visit(target, "testing", batch, observation, result)

    async def _red(
        self, target: TrainTarget, lane: TrainLane, batch: Batch,
        members: tuple[BatchMember, ...], observation: BatchObservation, result: ChecksResult,
        head: HeadIdentity, checks: ExactChecks,
    ) -> TrainVisit:
        """A red candidate is repaired only where the target is not already red.

        A required check that also fails on the target commit this candidate was
        built on is a pre-existing failure, and one the target has not decided is
        nobody's: neither is repairable, so this visit re-requests the candidate's
        own failing suites under bounded backoff and files no repair.
        """
        _progress("compare_target_baseline")
        baseline = await self.baseline.verdict(
            batch, candidate=result, target_sha=observation.target_sha, checks=checks,
        )
        if baseline.repair:
            return await self._repair(
                target, lane, batch, members, observation, result,
                brief=red_brief(baseline, head.sha),
            )
        _progress("rerequest_preexisting", candidate_sha=head.sha)
        re_requested = await self.baseline.re_request(
            batch, candidate=result, baseline=baseline, checks=checks,
        )
        visit = self._visit(target, "preexisting", batch, observation, result)
        return replace(visit, detail={
            **(visit.detail or {}), **baseline.detail(), "re_request": re_requested,
        })

    async def _checks(self, checks: ExactChecks, head: HeadIdentity) -> ChecksResult:
        """Request then refresh exact-head checks, outside every lock."""
        _progress("request_checks")
        await checks.request(head)
        _progress("refresh_checks")
        return await checks.refresh(head)

    async def _repair(
        self, target: TrainTarget, lane: TrainLane, batch: Batch,
        members: tuple[BatchMember, ...], observation: BatchObservation,
        result: ChecksResult | None, brief: str = "",
    ) -> TrainVisit:
        # The repair works on the batch's candidate ref, never the target: the
        # train alone fast-forwards the target once the repaired head is green.
        head = observation.candidate_sha
        if not head:
            return self._visit(target, "unknown", batch, replace(observation, detail={
                **(observation.detail or {}), "repair_publication": "unpublished",
            }), result)

        async def authorize():
            return await lane.service.repair_authorized(batch, members, head)

        # Keep the publication fence and the complete member instructions, and the
        # scope the target baseline left this repair.
        brief = brief or (conflict_brief(observation.detail, members, starting_sha=head)
                          if observation.state == "conflict" else "")
        missing_push = (observation.detail or {}).get("ci_not_triggered")
        if missing_push:
            source = missing_push.get("repair_source_ref", "the repository's default branch")
            brief = (
                f"CI did not trigger on the exact candidate {head}. {missing_push['reason']}\n"
                f"Fetch {source} and review its .github/workflows changes against this candidate. "
                "Merge or apply the missing workflow changes as an ordinary commit on the "
                "candidate, preserving intentional branch-specific changes and every frozen "
                "batch member. Ensure the workflows providing the required checks trigger on "
                f"push to {candidate_ref(batch.id)}. Publish the new candidate head with the "
                "managed lease so GitHub creates a push run for that exact SHA. "
                "Keep the trusted producer, required check names and attestation unchanged; "
                "workflow_dispatch and checks on another SHA cannot satisfy this repair. "
                "If the workflows already allow the candidate ref, diagnose path filters, "
                "push credentials or GitHub availability before changing workflow content."
            )
        _progress("allocate_repair")
        repair = await self.repair.allocate(batch.id, target_ref=candidate_ref(batch.id),
                                            head_sha=head, held=batch.intent != "open",
                                            authorize=authorize, brief=brief)
        state = "repair" if repair.get("outcome") in {"filed", "exists"} else "unknown"
        return self._visit(target, state, batch, observation, result, repair=repair)

    def _visit(
        self, target: TrainTarget, state: str, batch: Batch | None = None,
        observation: BatchObservation | None = None, result: ChecksResult | None = None, *,
        snapshot: GitTruthSnapshot | None = None, repair: dict | None = None,
    ) -> TrainVisit:
        target_sha = observation.target_sha if observation else None
        if target_sha is None and snapshot is not None:
            target_sha = snapshot.target_oid
        return TrainVisit(
            target, state,
            batch_id=batch.id if batch else None,
            candidate_sha=observation.candidate_sha if observation else None,
            target_sha=target_sha,
            tree_sha=observation.tree_sha if observation else None,
            checks=str(result.state) if result else None,
            repair=repair,
            detail=observation.detail if observation else None,
            observed_at=self.clock(),
        )
