"""The reduced integration train: level-triggered visits over Git, checks and intent.

Under ``integration.git_first: active`` one :class:`IntegrationTrain` takes the
place of the root, parent and development subject runtimes. It is a reduction
of :class:`~src.integration.reconciler.IntegrationReconciler`, not a second
engine: every visit derives progress from one fetched Git snapshot, the cached
checks of the exact candidate commit and the batch's durable intent, so a
restart or a missed notification is repaired by the next visit. There is no
journal, generation or receipt to replay.

Each admitted target (project, repository, target ref) has its own task. A
tick bounds repository concurrency and admits the least recently started idle
targets without awaiting running visits. Checks are refreshed outside every
lock; the batch gate reads only the cached verdict.

A red candidate is refreshed before a repair is allocated: a green observation
ends the repair with no attempt counted. A red candidate is then measured against
the target commit it was built on, and only a failure the target does not also
fail reaches the repair. An epic with only pre-existing failures can instead
receive one default-branch sync repair when that exact branch has trusted green
checks and train-produced or attested provenance. Otherwise the candidate's own
suites are re-requested under bounded backoff, and the
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
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.git.github_contracts import GitHubAccessError, rate_limit_cause
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
    failing,
    red_brief,
)
from src.integration.checks import ChecksResult, ChecksState, ExactChecks, HybridChecks
from src.integration.git_truth import GitTruthSnapshot
from src.integration.models import RepairPolicy
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope
from src.integration.runtime_contracts import HeadIdentity
from src.logging_config import log_handled

logger = logging.getLogger(__name__)

#: A rate-limited repository waits this long, doubling per consecutive limit,
#: or until GitHub's own retry time when that is later.
RATE_LIMIT_BACKOFF_SECONDS = 60.0
RATE_LIMIT_MAX_BACKOFF_SECONDS = 900.0
#: A promotion held on a named reason waits on a person or on GitHub, so its
#: target is revisited this long after, doubling while the same refusal repeats.
#: A promote command wakes the target at once.
PROMOTION_WAIT_SECONDS = 30.0
PROMOTION_MAX_WAIT_SECONDS = 300.0
REPOSITORY_CONCURRENCY = 4
BLOCKED_WAIT_SECONDS = 10.0
BLOCKED_MAX_WAIT_SECONDS = 60.0

TRAIN_KINDS = ("root", "epic", "development", "promotion")
_PROGRESS: ContextVar[dict | None] = ContextVar("train_visit_progress", default=None)
_TIMING: ContextVar[_VisitTiming | None] = ContextVar("train_visit_timing", default=None)


class _VisitTiming:
    """Monotonic stage durations, including time spent waiting for shared work."""

    def __init__(self) -> None:
        self.started = self.since = time.monotonic()
        self.finished: float | None = None
        self.stage = "lane_setup"
        self.stages: dict[str, float] = {}
        self.selection = SelectionMetrics()

    def advance(self, stage: str) -> None:
        now = time.monotonic()
        self.stages[self.stage] = self.stages.get(self.stage, 0.0) + now - self.since
        self.stage, self.since = stage, now

    def as_dict(self) -> dict:
        now = self.finished if self.finished is not None else time.monotonic()
        stages = dict(self.stages)
        stages[self.stage] = stages.get(self.stage, 0.0) + now - self.since
        return {"elapsed_seconds": now - self.started, "stages_seconds": stages,
                "selection": self.selection.as_dict()}


def _previous_state(last: TrainVisit | None) -> dict[str, str]:
    """The last state a finished visit knew, carried across consecutive timeouts."""
    if last is None:
        return {}
    detail = last.detail or {}
    if detail.get("reason") == "visit_timeout":
        return {"previous_state": detail["previous_state"]} if detail.get("previous_state") else {}
    return {"previous_state": last.state}


def _progress(stage: str, **facts) -> None:
    timing = _TIMING.get()
    if timing is not None:
        timing.advance(stage)
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
    step: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not (self.project_id and self.repository_id and self.target_ref):
            raise ValueError("train target needs project, repository and ref")
        if not self.target_ref.startswith("refs/heads/"):
            raise ValueError("train target must be a fully qualified branch ref")
        if self.kind not in TRAIN_KINDS:
            raise ValueError(f"unknown train target kind: {self.kind}")
        if self.step is not None:
            object.__setattr__(self, "step", deepcopy(self.step))

    @property
    def step_id(self) -> str | None:
        return self.step.get("id") if self.step is not None else None

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
        self, target: TrainTarget, snapshot: GitTruthSnapshot, service: BatchService, *,
        seal_now: bool = False,
    ) -> BatchSelection:
        """The target's open batch with its frozen members, freezing one when due.

        A new batch freezes through ``service.freeze``, which retains every
        exact completion source before the membership becomes immutable.
        ``seal_now`` bypasses admission timing for this call only; it never
        bypasses source, PR or publication gates.
        """

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        """Record that the target contains the batch; idempotent."""


class RepairAllocator(Protocol):
    async def settle_green(self, batch_id: str, head_sha: str) -> None:
        """Clear repair escalation after an exact green or delivered observation."""

    async def recover_reservation(
        self, batch_id: str, *, target_ref: str, head_sha: str,
        authorize: Callable[[], Awaitable[bool]], held: bool = False,
    ) -> dict:
        """Restore only an existing detached repair; never allocate new work."""

    async def allocate(
        self, batch_id: str, *, target_ref: str, head_sha: str,
        green_sha: str | None = None, held: bool = False, review_rejected: bool = False,
        authorize: Callable[[], Awaitable[bool]] | None = None,
        brief: str = "",
        policy: RepairPolicy | None = None,
        completion_blocker: Callable[[str, str], Awaitable[dict | None]] | None = None,
        sync_default_branch: dict[str, Any] | None = None,
    ) -> dict:
        """File (or find) the ordinary repair task for a batch's candidate ref.

        :class:`~src.integration.repair.OrdinaryRepairService` is the production
        allocator.
        """


def conflict_brief(
    detail: Mapping[str, Any] | None, members: Sequence[BatchMember], *, starting_sha: str,
    conflict_scope: str = "member",
) -> str:
    """Plain-English instructions for the repair of one batch merge conflict.

    ``detail`` is the exact ``merge_sources`` conflict result: the partial head
    it reached (``head``), the members it merged into that head (``members``),
    the member whose merge conflicted (``member``), the conflicting ``files``
    and a ``reason``. Only a starting head that *is* that partial head carries
    the members the merge already completed; from any other start every member
    is still owed to the target.

    The repair exists to land every remaining member, not to make the starting
    head green: a repaired head without them cannot close successfully or
    advance the target. A raw detail dict is never an instruction, so nothing else reaches
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
        (f"Conflict scope: {conflict_scope}. " + (
            "Resolve the conflicting member's changes while preserving the other members."
            if conflict_scope == "member" else
            "Resolve conflicts across the whole frozen batch, considering every member."
        )),
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
         "green: a repaired head without every remaining member cannot close "
         "successfully or advance the target."),
        (f"Generated files are regenerated, never hand-merged: resolve a conflict "
         f"in a path carrying the merge=aq-generated attribute in .gitattributes by "
         "taking either side while merging the sources, then running the repository's "
         f"regeneration command ({REGENERATION_COMMAND} by default) once after every "
         "remaining member is merged. Never resolve generated conflict markers by hand."),
        "Before publishing, verify that every frozen source listed above is an ancestor "
        "of HEAD. Keep all merge parents; do not squash, rebase or cherry-pick the inputs. "
        "Run the required checks on the complete repaired candidate. The train still "
        "requires its exact candidate checks and publication gates before advancing the target.",
    ]
    return "\n".join(line for line in lines if line)


def sync_default_branch_brief(
    decision: Mapping[str, Any], members: Sequence[BatchMember], *, starting_sha: str,
    conflict_scope: str = "member",
) -> str:
    """Merge the verified default head without losing any frozen batch input."""
    lines = [
        (f"The epic's pre-existing required-check failures are fixed on the verified "
         f"default branch {decision['ref']} at {decision['sha']}."),
        "Required checks passed on that exact default head:",
        *(f"  - {name}" for name in decision["checks"]),
        (f"Fetch {decision['ref']} and merge the pinned commit {decision['sha']} into "
         f"the candidate starting at {starting_sha}. Merge this exact commit even if "
         "the default branch moves afterwards; do not substitute an unverified head."),
        "Resolve conflicts while preserving every frozen member:",
        *(f"  - {member.task_id} (source {member.source_sha})" for member in members),
        ("Keep the starting candidate and all these sources in the repaired history; "
         "do not reset the candidate to the default branch or drop a member."),
        (f"Generated files are regenerated, never hand-merged: resolve their sources "
         f"and run the repository's regeneration command ({REGENERATION_COMMAND} by "
         "default) for paths marked merge=aq-generated in .gitattributes."),
        ("Publish the repaired candidate with the managed lease. It must pass the "
         "unchanged trusted required checks and attestation on its own exact SHA "
         "before the train can advance the epic. Default-branch evidence only "
         "authorizes this merge; it does not satisfy candidate checks."),
    ]
    if decision.get("conflicting_files"):
        lines.append(conflict_brief({
            "head": starting_sha, "member": decision["ref"], "reason": "default_branch_sync",
            "files": decision["conflicting_files"],
            "members": [{"member": member.task_id} for member in members],
        }, members, starting_sha=starting_sha, conflict_scope=conflict_scope))
    return "\n".join(lines)


def candidate_head(batch: Batch, candidate_sha: str) -> HeadIdentity:
    """The exact head whose checks gate a batch's publication."""
    return HeadIdentity(
        repository_id=batch.repository_id,
        ref=candidate_ref(batch.id),
        sha=candidate_sha,
        generation=batch.repair_attempt_count,
    )


def exact_gate(checks: ExactChecks | HybridChecks) -> Callable[[Batch, str, str], Awaitable[bool]]:
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
        self, resolve: Callable[[Batch, str], Awaitable[ExactChecks | HybridChecks | None]], *,
        limit: int = 64, advisory: bool = False,
    ) -> None:
        self.resolve, self.limit, self.advisory = resolve, limit, advisory
        self._resolved: dict[tuple[str, str], ExactChecks | HybridChecks | None] = {}

    @classmethod
    def fixed(cls, checks: ExactChecks) -> CandidateChecks:
        async def resolve(batch: Batch, candidate_sha: str) -> ExactChecks:
            return checks

        return cls(resolve)

    async def for_candidate(self, batch: Batch, candidate_sha: str) -> ExactChecks | HybridChecks | None:
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
    An epic's optional ``sync_default_branch`` verifies its current default
    head's own trusted checks and provenance before authorizing a sync repair.
    """

    snapshot: Callable[[], Awaitable[GitTruthSnapshot]]
    service: BatchService
    checks: CandidateChecks
    repair_policy: RepairPolicy | None = None
    complete_epic: Callable[[GitTruthSnapshot], Awaitable[tuple[dict[str, Any], ...]]] | None = None
    sync_default_branch: Callable[
        [Batch, GitTruthSnapshot, str, tuple[str, ...]], Awaitable[dict[str, Any] | None]
    ] | None = None
    sync_closed_epic: Callable[[GitTruthSnapshot], Awaitable[BatchSelection | None]] | None = None
    observe_source: Callable[[GitTruthSnapshot], Awaitable[None]] | None = None


@dataclass(frozen=True)
class BatchSelection:
    """An open batch and any completions whose evidence prevents admission."""

    batch: Batch | None = None
    members: tuple[BatchMember, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] | None = None
    existing: bool = False


@dataclass
class _Lane:
    task: asyncio.Task | None = None
    last: TrainVisit | None = None
    started_at: float = 0.0
    #: The train's count of visit starts when this lane's visit began.
    start: int = 0
    visits: int = 0
    errors: int = 0
    #: No visit starts before this time; :meth:`IntegrationTrain.wake` clears it.
    not_before: float = 0.0
    #: The unchanged refusal identity, and how many visits in a row.
    wait: tuple | None = None
    waits: int = 0
    #: The in-flight visit's progress facts (stage, batch); shown before its first result.
    progress: dict | None = None
    timing: _VisitTiming | None = None


@dataclass
class _RateLimitPause:
    """GitHub's rate limit on one repository, shared by all of its targets.

    Every visit re-verifies the repository with GitHub, so its targets hit the
    limit together. A visit limited after it started counts once per pause, and
    when the pause ends a single probe visit runs while the others wait for it.
    """

    hits: int = 0
    #: Visit starts before the latest limit; a visit numbered at or below it
    #: began before the pause.
    since: int = 0
    retry_at: float = 0.0
    probe: tuple[str, str, str] | None = None


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
        repository_concurrency: int = REPOSITORY_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if visit_timeout_seconds <= 0:
            raise ValueError("visit timeout must be positive")
        if repository_concurrency < 1:
            raise ValueError("repository concurrency must be positive")
        self.targets, self.batches, self.lane_for = targets, batches, lane_for
        self.repair, self.clock = repair, clock
        # Without a baseline service the train cannot compare the target, so it
        # claims nothing and repairs a red candidate exactly as before.
        self.baseline = baseline or UnrecordedBaseline()
        self.visit_timeout_seconds = visit_timeout_seconds
        self.repository_concurrency = repository_concurrency
        self._lanes: dict[tuple[str, str, str], _Lane] = {}
        self._pauses: dict[tuple[str, str], _RateLimitPause] = {}
        self._starts = 0
        self._tick_lock = asyncio.Lock()

    async def tick(
        self, now: float | None = None, *, target: TrainTarget | None = None,
    ) -> dict[str, list[str]]:
        """Bound each repository's visits; give new and longest-waiting targets a turn."""
        now = self.clock() if now is None else now
        if self._tick_lock.locked():
            return {"started": [], "running": [], "skipped": ["tick_in_progress"],
                    "deferred": []}
        async with self._tick_lock:
            started, running, deferred = [], [], []
            targets = [target] if target is not None else await self.targets.targets(now)
            occupied: dict[str, int] = {}
            for key, lane in self._lanes.items():
                if lane.task is not None and not lane.task.done():
                    occupied[key[1]] = occupied.get(key[1], 0) + 1
            # Unvisited targets have start=0. Stable ties retain discovery order.
            targets = sorted(targets, key=lambda t: self._lanes.get(t.key, _Lane()).start)
            for target in targets:
                lane = self._lanes.setdefault(target.key, _Lane())
                label = "/".join(target.key)
                if lane.task is not None and not lane.task.done():
                    running.append(label)
                    continue
                if (lane.not_before > now
                        or occupied.get(target.repository_id, 0) >= self.repository_concurrency
                        or not self._admit(target.key, now)):
                    deferred.append(label)
                    continue
                occupied[target.repository_id] = occupied.get(target.repository_id, 0) + 1
                self._starts += 1
                lane.started_at, lane.start = now, self._starts
                lane.task = asyncio.create_task(
                    self._bounded(target, lane), name=f"integration-train:{label}"
                )
                started.append(label)
            return {"started": started, "running": running, "skipped": [],
                    "deferred": deferred}

    async def request_visit(self, target: TrainTarget) -> TrainVisit | None:
        """Request a bounded visit through the same admission as the daemon tick."""
        scheduled = await self.tick(target=target)
        label = "/".join(target.key)
        lane = self._lanes.get(target.key)
        if label in scheduled["deferred"] or lane is None or lane.task is None:
            return None
        if (label not in scheduled["started"] and label not in scheduled["running"]
                and lane.task.done()):
            return None
        # Concurrent requests join this target's existing visit. Shield it so
        # cancellation of a claim or CLI request cannot stop train delivery.
        await asyncio.shield(lane.task)
        return lane.last

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
            running = lane.task is not None and not lane.task.done()
            row = lane.last.as_dict() if lane.last else {
                "project_id": key[0], "repository_id": key[1], "target_ref": key[2],
                # A first visit already under way is not "unvisited": say so,
                # and how far it has got, until its result replaces this row.
                "state": "visiting" if running else "unvisited",
            }
            detail = row.get("detail") or {}
            if (row.get("state") == "unknown" and detail.get("reason") == "visit_timeout"
                    and detail.get("previous_state")):
                # A visit that ran out of time did not change the target: show
                # its last known state (the timeout stays in detail/blockers).
                row["state"] = detail["previous_state"]
            row["running"] = running
            if lane.timing is not None:
                row["timing"] = lane.timing.as_dict()
            if running and lane.progress:
                row["progress"] = dict(lane.progress)
                row["visit_started_at"] = lane.started_at
            row["visits"], row["errors"] = lane.visits, lane.errors
            if lane.not_before:
                row["deferred_until"] = lane.not_before
            rows.append(row)
        return rows

    def wake(self, project_id: str, repository_id: str | None = None,
             target_ref: str | None = None) -> int:
        """Let the matching targets' next tick start a visit; returns how many waited."""
        woken = 0
        for key, lane in self._lanes.items():
            if (key[0] == project_id and repository_id in (None, key[1])
                    and target_ref in (None, key[2]) and lane.not_before):
                lane.not_before, woken = 0.0, woken + 1
                lane.wait, lane.waits = None, 0
        return woken

    def _wait(self, target: TrainTarget, lane: _Lane, visit: TrainVisit) -> TrainVisit:
        """Delay repeated refusals, without using the result as evidence on retry."""
        reason = (visit.detail or {}).get("reason")
        if target.kind == "promotion" and visit.state == "held" and isinstance(reason, str):
            wait = (visit.batch_id, reason)
            initial, maximum = PROMOTION_WAIT_SECONDS, PROMOTION_MAX_WAIT_SECONDS
        elif visit.state == "blocked":
            wait = (visit.batch_id, visit.target_sha, repr(visit.detail))
            initial, maximum = BLOCKED_WAIT_SECONDS, BLOCKED_MAX_WAIT_SECONDS
        else:
            lane.not_before, lane.wait, lane.waits = 0.0, None, 0
            return visit
        lane.waits = lane.waits + 1 if lane.wait == wait else 1
        lane.wait = wait
        delay = min(initial * 2 ** min(lane.waits - 1, 10), maximum)
        lane.not_before = self.clock() + delay
        return replace(visit, detail={**(visit.detail or {}), "retry_at": lane.not_before})

    async def _bounded(self, target: TrainTarget, lane: _Lane) -> None:
        progress = {"stage": "lane_setup"}
        lane.progress = progress
        token = _PROGRESS.set(progress)
        timing = lane.timing = _VisitTiming()
        timing_token = _TIMING.set(timing)
        try:
            with selection_metrics_scope(timing.selection):
                visit = await asyncio.wait_for(self.visit(target), self.visit_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            lane.errors += 1
            visit = TrainVisit(target, "unknown", batch_id=progress.get("batch_id"),
                               candidate_sha=progress.get("candidate_sha"),
                               target_sha=progress.get("target_sha"),
                               detail={"reason": "visit_timeout", **progress,
                                       "timeout_seconds": self.visit_timeout_seconds,
                                       **_previous_state(lane.last)},
                               observed_at=self.clock())
            logger.warning("integration train visit timed out for %s", target.key)
        except Exception as exc:  # one target's failure never stops the train
            lane.errors += 1
            limit = rate_limit_cause(exc)
            if limit is not None:
                visit = self._defer_rate_limited(target, lane, exc, limit)
            else:
                visit = TrainVisit(target, "unknown", detail={"reason": type(exc).__name__,
                                                              "error": str(exc)[:500]},
                                   observed_at=self.clock())
                logger.exception("integration train visit failed for %s", target.key)
        finally:
            timing.finished = time.monotonic()
            _TIMING.reset(timing_token)
            _PROGRESS.reset(token)
            lane.progress = None
            logging.getLogger(__name__ + ".timing").info(
                "integration train visit timing for %s: %s", target.key, timing.as_dict())
        pause = self._pauses.get(target.key[:2])
        if (pause is not None and lane.start > pause.since
                and (visit.detail or {}).get("reason") != "rate_limited"):
            # A visit begun after the pause got past GitHub: the limit is over.
            del self._pauses[target.key[:2]]
        lane.visits += 1
        lane.last = self._wait(target, lane, visit)

    def _admit(self, key: tuple[str, str, str], now: float) -> bool:
        """Whether the repository's rate-limit pause lets *key* start a visit."""
        pause = self._pauses.get(key[:2])
        if pause is None:
            return True
        if pause.retry_at > now:
            return False
        if pause.probe is not None and pause.probe != key:
            probe = self._lanes.get(pause.probe)
            if probe is not None and probe.task is not None and not probe.task.done():
                return False
        pause.probe = key
        return True

    def _defer_rate_limited(
        self, target: TrainTarget, lane: _Lane, exc: Exception, limit: GitHubAccessError,
    ) -> TrainVisit:
        """Pause the repository past GitHub's limit rather than fail it every tick."""
        now = self.clock()
        pause = self._pauses.setdefault(target.key[:2], _RateLimitPause())
        label = "/".join(target.key)
        if pause.hits and lane.start <= pause.since:
            # A sibling started before the pause began; it is the same limit.
            pause.retry_at = max(pause.retry_at, limit.retry_at or 0.0)
            log_handled(logger, logging.DEBUG,
                        "integration train visit for %s rate limited", label, exc=exc)
        else:
            pause.hits += 1
            backoff = min(RATE_LIMIT_BACKOFF_SECONDS * 2 ** (pause.hits - 1),
                          RATE_LIMIT_MAX_BACKOFF_SECONDS)
            pause.since, pause.probe = self._starts, None
            pause.retry_at = max(now + backoff, limit.retry_at or 0.0)
            log_handled(logger, logging.WARNING,
                        "integration train visit for %s rate limited; repository %s "
                        "deferred %.0fs", label, "/".join(target.key[:2]),
                        pause.retry_at - now, exc=exc)
        return TrainVisit(target, "unknown",
                          detail={"reason": "rate_limited", "error": str(exc)[:500],
                                  "retry_at": pause.retry_at},
                          observed_at=now)

    async def visit(self, target: TrainTarget, *, seal_now: bool = False) -> TrainVisit:
        """Observe the target once and take at most one step toward delivery.

        ``seal_now`` bypasses cadence for this observation only. Admission and
        publication still require their ordinary evidence.
        """
        from src.integration.ci import hosted_observation_scope

        with hosted_observation_scope():
            return await self._visit_target(target, seal_now=seal_now)

    async def _visit_target(self, target: TrainTarget, *, seal_now: bool = False) -> TrainVisit:
        lane = await self.lane_for(target)
        _progress("fetch_snapshot")
        snapshot = await lane.snapshot()
        if lane.observe_source:
            _progress("observe_promotion_source")
            await lane.observe_source(snapshot)
        _progress("select_batch", target_sha=snapshot.target_oid)
        opened = await self.batches.open_batch(
            target, snapshot, lane.service, **({"seal_now": True} if seal_now else {}),
        )
        if (target.kind == "epic" and opened.batch is None
                and not opened.blockers and lane.sync_closed_epic):
            opened = await lane.sync_closed_epic(snapshot) or opened
        if opened.batch is None:
            state = ("unknown" if any(b["code"] == "unknown" for b in opened.blockers)
                     else "blocked" if opened.blockers else "settling" if opened.detail else "idle")
            visit = replace(self._visit(target, state, snapshot=snapshot), detail=opened.detail)
        else:
            visit = await self._visit_batch(target, lane, snapshot, opened.batch, opened.members)
        blockers = opened.blockers
        if target.kind == "epic" and lane.complete_epic and visit.state in {"idle", "delivered"}:
            _progress("settle_epic")
            # Publication moved the ref; readiness must examine the collected
            # head, rather than the snapshot from before the child batch landed.
            if visit.state == "delivered":
                snapshot = await lane.snapshot()
            completion = await lane.complete_epic(snapshot)
            events = [item for item in completion if item.get("blocking") is False]
            blockers += tuple(item for item in completion if item.get("blocking") is not False)
            if events:
                visit = replace(visit, detail={**(visit.detail or {}), "epic_completions": events})
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
            if head := observation.candidate_sha or observation.target_sha:
                await self.repair.settle_green(batch.id, head)
            _progress("settle_batch")
            await self.batches.settle(batch, observation)
            return self._visit(target, "delivered", batch, observation)
        if (batch.repair_attempt_count and observation.candidate_sha
                and observation.state != "held"):
            # A retry can lose its lease on close/recovery. Red/conflict
            # allocation is not visited while CI is pending (or observation
            # is unknown), so restore the existing filing on every such visit.
            async def authorize_reservation():
                return await lane.service.repair_authorized(
                    batch, members, observation.candidate_sha,
                )

            _progress("recover_repair_reservation")
            await self.repair.recover_reservation(
                batch.id, target_ref=candidate_ref(batch.id),
                head_sha=observation.candidate_sha, authorize=authorize_reservation,
                held=batch.intent != "open",
            )
        if observation.state == "conflict":
            async def completion_blocker(task_id, starting_sha):
                return await lane.service.repair_completion_blocker(
                    batch, task_id, starting_sha, snapshot,
                )

            return await self._repair(target, lane, batch, members, observation, None,
                                      completion_blocker=completion_blocker)
        if observation.state != "testing" or not observation.candidate_sha:
            # held, moved, source_moved, unknown, no_regenerator: the next visit
            # observes again. None of these is a member's content conflict, so
            # none allocates a repair.
            return self._visit(target, observation.state, batch, observation)
        head = (await lane.checks.head(batch, observation.candidate_sha)
                if target.kind == "promotion" else candidate_head(batch, observation.candidate_sha))
        _progress("resolve_checks")
        checks = await lane.checks.for_candidate(batch, observation.candidate_sha)
        result = None if checks is None else await self._checks(checks, head)
        if result is None or lane.checks.passes(result):
            if result is None or result.green:
                await self.repair.settle_green(batch.id, observation.candidate_sha)
            # The gate now reads this verdict; publish within this visit.
            _progress("publish_candidate")
            published = await lane.service.visit(batch, members, snapshot)
            if published.state == "delivered":
                if result is not None and not result.green:
                    await self.repair.settle_green(batch.id, observation.candidate_sha)
                _progress("settle_batch")
                await self.batches.settle(batch, published)
            return self._visit(target, published.state, batch, published, result)
        if result.state == ChecksState.RED:
            if target.kind == "promotion":
                return self._visit(target, "held", batch, replace(observation, detail={
                    "reason": "promotion_checks_failed",
                }), result)
            return await self._red(
                target, lane, batch, members, observation, result, head, checks, snapshot
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
            if target.kind != "promotion":
                return await self._repair(target, lane, batch, members, observation, result)
        return self._visit(target, "testing", batch, observation, result)

    async def _red(
        self, target: TrainTarget, lane: TrainLane, batch: Batch,
        members: tuple[BatchMember, ...], observation: BatchObservation, result: ChecksResult,
        head: HeadIdentity, checks: ExactChecks, snapshot: GitTruthSnapshot,
    ) -> TrainVisit:
        """A red candidate is repaired only where the target is not already red.

        A required check that also fails on the target commit this candidate was
        built on is a pre-existing failure, and one the target has not decided is
        nobody's. Only an epic's proven pre-existing failures can authorize a
        bounded merge of a verified default head; all other unrepairable reds
        re-request the candidate's own suites under bounded backoff.
        """
        if not failing(result):
            # A workflow can fail while every required job succeeds. Preserve
            # its publication gate, but do not send a worker an empty repair.
            return self._visit(target, "blocked", batch, replace(observation, detail={
                **(observation.detail or {}),
                "blocker": "candidate_failing_checks_unknown",
                "reason": "red candidate has no identifiable failing required checks",
            }), result)
        _progress("compare_target_baseline")
        baseline = await self.baseline.verdict(
            batch, candidate=result, target_sha=observation.target_sha, checks=checks,
        )
        if baseline.repair:
            visit = await self._repair(
                target, lane, batch, members, observation, result,
                brief=red_brief(baseline, head.sha, candidate=result),
            )
            return replace(visit, detail={**(visit.detail or {}), **baseline.detail()})
        sync = None
        if target.kind == "epic" and baseline.state == "pre_existing" and lane.sync_default_branch:
            _progress("verify_default_branch")
            sync = await lane.sync_default_branch(batch, snapshot, head.sha, baseline.pre_existing)
            if sync:
                visit = await self._repair(
                    target, lane, batch, members, observation, result,
                    brief=sync_default_branch_brief(
                        sync, members, starting_sha=head.sha,
                        conflict_scope=(lane.repair_policy.conflict_scope
                                        if lane.repair_policy else "member"),
                    ),
                    sync_default_branch=sync,
                )
                sync = {**sync, "outcome": (visit.repair or {}).get("outcome")}
                if sync["outcome"] != "sync_exhausted":
                    return replace(visit, detail={
                        **(visit.detail or {}), **baseline.detail(), "sync_default_branch": sync,
                    })
        _progress("rerequest_preexisting", candidate_sha=head.sha)
        re_requested = await self.baseline.re_request(
            batch, candidate=result, baseline=baseline, checks=checks,
        )
        visit = self._visit(target, "preexisting", batch, observation, result)
        return replace(visit, detail={
            **(visit.detail or {}), **baseline.detail(), "re_request": re_requested,
            **({"sync_default_branch": sync} if sync else {}),
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
        completion_blocker: Callable[[str, str], Awaitable[dict | None]] | None = None,
        sync_default_branch: dict[str, Any] | None = None,
    ) -> TrainVisit:
        # Refresh repairs own the epic lease, but must retain the tested merged
        # candidate. A conflict's candidate is the retained partial merge.
        head = observation.candidate_sha
        if not head:
            return self._visit(target, "unknown", batch, replace(observation, detail={
                **(observation.detail or {}), "repair_publication": "unpublished",
            }), result)

        async def authorize():
            if batch.epic_refresh:
                repo = await lane.service.gitops.repository(batch)
                return (await lane.service._authorized(batch, members) and
                        await lane.service.gitops.remote(repo, candidate_ref(batch.id)) == head and
                        await lane.service.gitops.remote(repo, batch.target_ref)
                        == observation.target_sha)
            return await lane.service.repair_authorized(batch, members, head)

        # Keep the publication fence and the complete member instructions, and the
        # scope the target baseline left this repair.
        if not brief and observation.state == "conflict":
            brief = conflict_brief(
                observation.detail, members, starting_sha=head,
                conflict_scope=lane.repair_policy.conflict_scope if lane.repair_policy else "member",
            )
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
        repair = await self.repair.allocate(batch.id, target_ref=(
            batch.target_ref if batch.epic_refresh else candidate_ref(batch.id)),
                                            head_sha=head, held=batch.intent != "open",
                                            authorize=authorize, brief=brief,
                                            policy=lane.repair_policy,
                                            completion_blocker=completion_blocker,
                                            **({"sync_default_branch": sync_default_branch}
                                               if sync_default_branch else {}))
        if repair.get("outcome") in {"blocked", "human_required"}:
            return self._visit(target, "blocked", batch, replace(observation, detail={
                **(observation.detail or {}), **repair,
            }), result, repair=repair)
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
