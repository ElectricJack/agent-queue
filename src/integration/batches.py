"""Frozen inputs and one Git-observed build/publish path for every target.

CommandHandler supplies policy, ordinary task identity checks and a managed
publisher. Checks run outside publisher locks. A publication callback must
recheck ``authorize`` inside its per-ref fence before expected-old transport.
There are no persisted candidate, promotion or delivery projections here.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from contextlib import asynccontextmanager
from typing import Protocol

from sqlalchemy import func, insert, select, update

from src.database.tables import integration_batch_members, integration_batches
from src.git.manager import GitError, is_valid_git_oid
from src.integration.delivery_truth import DeliveryRequest
from src.integration.git_truth import GitTruthSnapshot
from src.integration.gitops import ZERO, GitOperations, RetainedRepository, branch
from src.integration.provenance import CompletionIdentity, GitProvenance


def candidate_ref(batch_id: str) -> str:
    """Stable across visits, restarts, repair attempts and target movement."""
    if not batch_id:
        raise ValueError("batch id is required")
    return "refs/heads/aq/batches/" + hashlib.sha256(batch_id.encode()).hexdigest()


@dataclass(frozen=True)
class Batch:
    id: str
    project_id: str
    repository_id: str
    target_ref: str
    intent: str = "open"
    repair_attempt_count: int = 0
    created_at: float = 0

    def __post_init__(self):
        branch(self.target_ref)
        if self.intent not in {"open", "paused", "aborted"} or self.repair_attempt_count < 0:
            raise ValueError("invalid batch intent/counter")
        if self.target_ref == candidate_ref(self.id):
            raise ValueError("candidate cannot target itself")

    @classmethod
    def from_row(cls, row):
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class BatchMember:
    task_id: str
    source_sha: str
    source_base_sha: str
    order: int = 0

    def __post_init__(self):
        if not self.task_id or self.order < 0 or not all(
            is_valid_git_oid(oid) for oid in (self.source_sha, self.source_base_sha)
        ):
            raise ValueError("member requires a task, exact source/base and nonnegative order")

    @property
    def head_sha(self):
        return self.source_sha

    @property
    def base_sha(self):
        return self.source_base_sha


def ordered_members(
    members: Iterable[BatchMember], dependencies: Mapping[str, Iterable[str]] | None = None,
) -> tuple[BatchMember, ...]:
    """Stable topological order, including dependencies among this batch only."""
    members = tuple(members)
    remaining = {member.task_id: member for member in members}
    if len(remaining) != len(members):
        raise ValueError("duplicate batch member")
    edges = {key: set((dependencies or {}).get(key, ())) & remaining.keys() for key in remaining}
    result = []
    while remaining:
        ready = sorted(key for key in remaining if not edges[key] & remaining.keys())
        if not ready:
            raise ValueError("batch dependency cycle")
        for key in ready:
            result.append(replace(remaining.pop(key), order=len(result)))
    return tuple(result)


class BatchStore:
    """Intent/input writes invoked by commands; legacy columns retire at stage four."""

    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock

    async def get(self, batch_id: str) -> Batch | None:
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
                integration_batches.c.target_ref.is_not(None),
            ))).mappings().first()
        return Batch.from_row(row) if row else None

    async def members(self, batch_id: str) -> tuple[BatchMember, ...]:
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(integration_batch_members).where(
                integration_batch_members.c.batch_id == batch_id,
            ).order_by(integration_batch_members.c.ordinal))).mappings().all()
        return tuple(BatchMember(row["task_id"], row["source_sha"],
                                 row["source_base_sha"], row["ordinal"]) for row in rows)

    async def freeze(self, batch: Batch, members: Iterable[BatchMember], *, trees: Mapping[str, str]):
        """Atomic immutable membership; replay must name exactly the same inputs."""
        members = tuple(members)
        if tuple(member.order for member in members) != tuple(range(len(members))):
            raise ValueError("membership must have contiguous frozen order")
        if not members:
            raise ValueError("empty work needs no batch")
        if len({member.task_id for member in members}) != len(members):
            raise ValueError("duplicate batch member")
        now = self.clock()
        async with self.db.immediate() as conn:
            # Serialize absent-id creation too. No Git or check work under this lock.
            await conn.execute(select(func.pg_advisory_xact_lock(
                func.hashtextextended("git-batch:" + batch.id, 0),
            )))
            existing = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().first()
            if existing:
                frozen = (await conn.execute(select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch.id,
                ).order_by(integration_batch_members.c.ordinal))).mappings().all()
                identity = (existing["project_id"], existing["repository_id"], existing["target_ref"])
                wanted = (batch.project_id, batch.repository_id, batch.target_ref)
                inputs = tuple(BatchMember(r["task_id"], r["source_sha"],
                                          r["source_base_sha"], r["ordinal"]) for r in frozen)
                if identity != wanted or inputs != members:
                    raise ValueError("batch id already names different frozen inputs")
                return Batch.from_row(existing)
            manifest = hashlib.sha256(repr(members).encode()).hexdigest()
            await conn.execute(insert(integration_batches).values(
                id=batch.id, project_id=batch.project_id, repository_id=batch.repository_id,
                target_ref=batch.target_ref, intent=batch.intent,
                repair_attempt_count=batch.repair_attempt_count,
                created_at=batch.created_at or now, updated_at=now,
                # Compatibility-only required fields, never read by this engine.
                request_id=batch.id, source_manifest_digest=manifest,
                base_sha=members[0].base_sha, integration_branch=candidate_ref(batch.id),
                lifecycle="sealing", policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
            ))
            for member in members:
                tree = trees[member.task_id]
                if not is_valid_git_oid(tree):
                    raise ValueError("source tree must be exact")
                await conn.execute(insert(integration_batch_members).values(
                    batch_id=batch.id, ordinal=member.order, task_id=member.task_id,
                    repository_id=batch.repository_id, source_sha=member.source_sha,
                    source_base_sha=member.base_sha, reviewed_head_sha=member.source_sha,
                    reviewed_tree_sha=tree, review_evidence_id=None, review_evidence={},
                ))
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).values(lifecycle="sealed"))
        return replace(batch, created_at=batch.created_at or now)

    @asynccontextmanager
    async def publication(self, batch_id: str):
        """Serialize explicit batch intent with the bounded managed transport.

        Lock order is batch intent, then managed ref. Check refresh/build work
        happens before this section. set_intent shares this row lock.
        """
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(integration_batches.c.intent).where(
                integration_batches.c.id == batch_id,
                integration_batches.c.target_ref.is_not(None),
            ).with_for_update())).scalar_one_or_none()
            yield row == "open"

    async def set_intent(self, batch_id: str, intent: str):
        """Abort before intentional candidate deletion; an abort is irreversible."""
        if intent not in {"open", "paused", "aborted"}:
            raise ValueError("invalid batch intent")
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
                integration_batches.c.target_ref.is_not(None),
            ).with_for_update())).mappings().one()
            if row["intent"] == "aborted" and intent != "aborted":
                raise ValueError("aborted batch cannot reopen")
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch_id,
            ).values(intent=intent, updated_at=self.clock()))


class ManagedPublish(Protocol):
    async def __call__(
        self, repo: RetainedRepository, ref: str, *, expected_old_oid: str, new_oid: str,
        authorize: Callable[[], Awaitable[bool]],
    ) -> object:
        """Recheck authorization under the current per-ref holder/fence/expiry lock."""


@dataclass(frozen=True)
class BatchObservation:
    state: str
    candidate_sha: str | None = None
    target_sha: str | None = None
    tree_sha: str | None = None
    detail: dict | None = None


class BatchService:
    """Root, child and development batches use this same mechanism.

    ``eligible`` reloads ordinary identities, holds and authorization; it must
    reject reopened/rebound work. ``gate`` refreshes trusted exact-SHA checks
    and current required tree verdicts. The managed publisher calls the final
    authorization predicate while holding its short fence lock. Gates do no
    network/job work in that critical section; implementations cache the visit's
    observation and revalidate current evidence there.
    """

    def __init__(
        self, store: BatchStore, gitops: GitOperations, *, publish: ManagedPublish,
        eligible: Callable[[Batch, tuple[BatchMember, ...]], Awaitable[bool]],
        gate: Callable[[Batch, str, str], Awaitable[bool]],
    ):
        self.store, self.gitops = store, gitops
        self.publish, self.eligible, self.gate = publish, eligible, gate

    async def _authorized(self, batch, members):
        current = await self.store.get(batch.id)
        return bool(current and current.intent == "open" and
                    (current.project_id, current.repository_id, current.target_ref) ==
                    (batch.project_id, batch.repository_id, batch.target_ref) and
                    await self.store.members(batch.id) == members and
                    await self.eligible(current, members))

    async def freeze(
        self, batch: Batch, members: Iterable[BatchMember], *,
        requests: Mapping[str, DeliveryRequest], snapshot: GitTruthSnapshot,
        dependencies: Mapping[str, Iterable[str]] | None = None,
    ) -> Batch:
        """Retain current exact completion sources before freezing their inputs."""
        repo = await self.gitops.repository(batch)
        await self.gitops.validate_repository(repo)
        members = ordered_members(members, dependencies)
        if (snapshot.observation.project_id, snapshot.observation.repository_id,
            snapshot.observation.target_ref) != (batch.project_id, batch.repository_id,
                                                 batch.target_ref):
            raise ValueError("snapshot belongs to another batch target")
        if not await self.eligible(batch, members) or snapshot.observation.error:
            raise ValueError("batch inputs are not currently eligible")
        trees = {}
        provenance = GitProvenance(self.gitops.git, str(repo.store),
                                   repository_url=snapshot.observation.repository_url)
        for member in members:
            request = requests[member.task_id]
            if (request.task_status != "COMPLETED" or
                (request.project_id, request.repository_id, request.target_ref) !=
                (batch.project_id, batch.repository_id, batch.target_ref)):
                raise ValueError("source task identity or target changed")
            identity = CompletionIdentity(batch.project_id, batch.repository_id, member.task_id,
                                          request.completion_id or request.legacy_generation)
            record = await provenance.read_completion(identity, refs=snapshot.observation.source_heads)
            if record is None or record["source_oid"] != member.source_sha:
                raise ValueError("exact completion source is not retained")
            await self.gitops.exact(repo, member.source_sha)
            if not await self.gitops.is_ancestor(repo, member.base_sha, member.source_sha):
                raise ValueError("source base is not retained source history")
            trees[member.task_id] = await self.gitops.git.atree_sha(str(repo.store), member.source_sha)
        # Completion provenance refs already pin source and all base ancestors.
        # Never write a replacement delivery record or a new journal at seal.
        if not await self.eligible(batch, members) or not await snapshot.observation.is_fresh():
            raise ValueError("source identity/target moved while sealing")
        return await self.store.freeze(batch, members, trees=trees)

    async def _transfer(self, batch, repo, ref, sha, expected, authorize):
        """Read-back also settles an exception after a successful authenticated push."""
        if not await authorize():
            return "held"
        actual = await self.gitops.remote(repo, ref)
        if actual == sha:
            return "published"
        if (actual or ZERO) != expected:
            return "moved"
        try:
            async with self.store.publication(batch.id) as opened:
                if not opened:
                    return "held"
                await self.publish(repo, ref, expected_old_oid=expected, new_oid=sha,
                                   authorize=authorize)
        except (GitError, RuntimeError, ValueError):
            pass
        actual = await self.gitops.remote(repo, ref)
        if actual == sha:
            return "published"
        return "moved" if (actual or ZERO) != expected else "unknown"

    async def visit(self, batch: Batch, members: Iterable[BatchMember], snapshot: GitTruthSnapshot):
        """Derive progress from this visit's fetched refs; never from legacy lifecycle."""
        members = tuple(members)
        current = await self.store.get(batch.id)
        if (current is None or
            (current.project_id, current.repository_id, current.target_ref) !=
            (batch.project_id, batch.repository_id, batch.target_ref) or
            await self.store.members(batch.id) != members):
            return BatchObservation("unknown", detail={"reason": "frozen_inputs_changed"})
        repo = await self.gitops.repository(batch)
        await self.gitops.validate_repository(repo)
        observed = snapshot.observation
        if (observed.project_id, observed.repository_id, observed.target_ref) != (
            batch.project_id, batch.repository_id, batch.target_ref,
        ):
            raise ValueError("snapshot belongs to another batch target")
        if observed.error or snapshot.target_oid is None:
            return BatchObservation("unknown", detail={"reason": observed.error})
        target = snapshot.target_oid
        ref = candidate_ref(batch.id)
        candidate = observed.source_heads.get("refs/remotes/origin/" + branch(ref))
        try:
            # Historical inclusion settles even a held/aborted intent. It does
            # not authorize another write, and stale progress never blocks it.
            if candidate and await self.gitops.is_ancestor(repo, candidate, target):
                return BatchObservation("delivered", candidate, target)
            proofs = [await snapshot.contains_source(m.task_id, m.source_sha, m.base_sha)
                      for m in members]
            if members and all(proof is True for proof in proofs):
                return BatchObservation("delivered", candidate, target)
            if any(proof is None for proof in proofs):
                return BatchObservation("unknown", candidate, target)
            if not await self._authorized(batch, members):
                return BatchObservation("held", candidate, target)
            # A repaired candidate is reusable only while it retains this target
            # and proves every complete source via ancestry or exact AQ trailers.
            reusable = bool(candidate and await self.gitops.is_ancestor(repo, target, candidate))
            if reusable:
                trailers = await self.gitops.run(repo, "log", "--format=%B", f"{target}..{candidate}")
                from src.integration.source_trailer import parse_source_trailers

                identities = parse_source_trailers(trailers)
                from src.integration.source_trailer import SourceIdentity

                reusable = all([
                    await self.gitops.is_ancestor(repo, member.source_sha, candidate) or
                    SourceIdentity(member.task_id, member.source_sha) in identities for member in members
                ])
            if not reusable:
                result = await self.gitops.merge_sources(repo, target, members,
                                                        created_at=batch.created_at)
                if result["outcome"] != "merged":
                    return BatchObservation(result["outcome"], candidate, target, detail=result)
                head = result["head"]
                transferred = await self._transfer(batch, repo, ref, head, candidate or ZERO,
                                                  lambda: self._authorized(batch, members))
                if transferred != "published":
                    return BatchObservation(transferred, head, target)
                candidate = head
            tree = await self.gitops.git.atree_sha(str(repo.store), candidate)
            if not await self.gate(batch, candidate, tree):
                return BatchObservation("testing", candidate, target, tree)

            async def authorize():
                return (await self._authorized(batch, members) and
                        await self.gitops.remote(repo, ref) == candidate and
                        await self.gate(batch, candidate, tree))

            # Explicit expected-old CAS and ancestry enforce the exact final FF.
            if not await self.gitops.is_ancestor(repo, target, candidate):
                return BatchObservation("moved", candidate, target, tree)
            state = await self._transfer(batch, repo, batch.target_ref, candidate, target, authorize)
            return BatchObservation("delivered" if state == "published" else state,
                                    candidate, target, tree)
        except (GitError, OSError, ValueError) as exc:
            return BatchObservation("unknown", candidate, target, detail={"reason": str(exc)})
