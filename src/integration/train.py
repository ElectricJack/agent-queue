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
ends the repair with no attempt counted. The allocator counts attempts when it
files, never per visit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.integration.batches import (
    Batch,
    BatchMember,
    BatchObservation,
    BatchService,
    candidate_ref,
)
from src.integration.checks import ChecksResult, ChecksState, ExactChecks
from src.integration.git_truth import GitTruthSnapshot
from src.integration.subjects import HeadIdentity

logger = logging.getLogger(__name__)

TRAIN_KINDS = ("root", "epic", "development")


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
        self, target: TrainTarget, snapshot: GitTruthSnapshot
    ) -> tuple[Batch, tuple[BatchMember, ...]] | None:
        """The target's open batch with its frozen members, freezing one when due."""

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        """Record that the target contains the batch; idempotent."""


class RepairAllocator(Protocol):
    async def allocate(
        self, batch_id: str, *, target_ref: str, head_sha: str,
        green_sha: str | None = None, held: bool = False,
    ) -> dict:
        """File (or find) the ordinary repair task for a batch on its target."""


@dataclass(frozen=True)
class TrainLane:
    """The per-target mechanism: one batch service and one checks cache.

    ``snapshot`` fetches the target once per visit. ``generation`` names the
    candidate's revision for exact-head checks (the batch's repair count).
    """

    snapshot: Callable[[], Awaitable[GitTruthSnapshot]]
    service: BatchService
    checks: ExactChecks


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
        visit_timeout_seconds: float = 900.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if visit_timeout_seconds <= 0:
            raise ValueError("visit timeout must be positive")
        self.targets, self.batches, self.lane_for = targets, batches, lane_for
        self.repair, self.clock = repair, clock
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
        try:
            visit = await asyncio.wait_for(self.visit(target), self.visit_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            lane.errors += 1
            visit = TrainVisit(target, "unknown", detail={"reason": "visit_timeout"},
                               observed_at=self.clock())
            logger.warning("integration train visit timed out for %s", target.key)
        except Exception as exc:  # one target's failure never stops the train
            lane.errors += 1
            visit = TrainVisit(target, "unknown", detail={"reason": type(exc).__name__,
                                                          "error": str(exc)[:500]},
                               observed_at=self.clock())
            logger.exception("integration train visit failed for %s", target.key)
        lane.visits += 1
        lane.last = visit

    async def visit(self, target: TrainTarget) -> TrainVisit:
        """Observe the target once and take at most one step toward delivery."""
        lane = await self.lane_for(target)
        snapshot = await lane.snapshot()
        opened = await self.batches.open_batch(target, snapshot)
        if opened is None:
            return self._visit(target, "idle", snapshot=snapshot)
        batch, members = opened
        observation = await lane.service.visit(batch, members, snapshot)
        if observation.state == "delivered":
            await self.batches.settle(batch, observation)
            return self._visit(target, "delivered", batch, observation)
        if observation.state == "conflict":
            return await self._repair(target, batch, observation, None)
        if observation.state != "testing" or not observation.candidate_sha:
            # held, moved, source_moved, unknown: the next visit observes again.
            return self._visit(target, observation.state, batch, observation)
        head = candidate_head(batch, observation.candidate_sha)
        result = await self._checks(lane.checks, head)
        if result.green:
            # The gate now reads green; publish within this visit.
            published = await lane.service.visit(batch, members, snapshot)
            if published.state == "delivered":
                await self.batches.settle(batch, published)
            return self._visit(target, published.state, batch, published, result)
        if result.state == ChecksState.RED:
            return await self._repair(target, batch, observation, result)
        return self._visit(target, "testing", batch, observation, result)

    async def _checks(self, checks: ExactChecks, head: HeadIdentity) -> ChecksResult:
        """Request then refresh exact-head checks, outside every lock."""
        await checks.request(head)
        return await checks.refresh(head)

    async def _repair(
        self, target: TrainTarget, batch: Batch, observation: BatchObservation,
        result: ChecksResult | None,
    ) -> TrainVisit:
        head = observation.candidate_sha or observation.target_sha
        if not head:
            return self._visit(target, "unknown", batch, observation, result)
        repair = await self.repair.allocate(batch.id, target_ref=target.target_ref,
                                            head_sha=head, held=batch.intent != "open")
        return self._visit(target, "repair", batch, observation, result, repair=repair)

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
            checks=str(result.state) if result else None,
            repair=repair,
            detail=observation.detail if observation else None,
            observed_at=self.clock(),
        )
