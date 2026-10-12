"""Frozen inputs and one Git-observed build/publish path for every target.

CommandHandler supplies policy, ordinary task identity checks and a managed
publisher. Checks run outside publisher locks. A publication callback must
recheck ``authorize`` inside its per-ref fence before expected-old transport.
There are no persisted candidate, promotion or delivery projections here.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import func, insert, select, union_all, update

from src.database.tables import (
    archived_tasks, integration_batch_members, integration_batches, projects, tasks,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.delivery_truth import DeliveryRequest
from src.integration.git_truth import GitTruthSnapshot
from src.integration.gitops import ZERO, GitOperations, RetainedRepository, branch
from src.integration.provenance import CompletionIdentity, GitProvenance

logger = logging.getLogger(__name__)

def ejection_instruction(batch_identity):
    """Only a batch-owned eject record for its frozen member releases inputs.

    Task metadata is untrusted, including legacy ejection markers. The record
    is written atomically by eject or daemon supersede and survives member
    archival/deletion.
    """
    batch = integration_batches.alias("ejected_batch")
    member = integration_batch_members.alias("ejected_member")
    record = batch.c.ejection_record
    return select(batch.c.id).select_from(batch.join(member,
        member.c.batch_id == batch.c.id)).where(
        batch.c.id == batch_identity,
        batch.c.intent == "aborted",
        record["batch_id"].astext == batch.c.id,
        record["project_id"].astext == batch.c.project_id,
        record["task_id"].astext == member.c.task_id,
        member.c.repository_id == batch.c.repository_id,
        func.length(func.trim(record["operator_id"].astext)) > 0,
        func.length(func.trim(record["reason"].astext)) > 0,
    ).exists()


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

    @property
    def epic_sync(self) -> bool:
        """A closed epic's sync batch still owes a tested candidate publication.

        Its deterministic identity freezes the collected head and exact child
        sources, rather than another delivery of already-contained children.
        """
        return self.id.startswith("train-epic-sync-")

    @property
    def epic_refresh(self) -> bool:
        return self.id.startswith("train-epic-refresh-")

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


class SupersedeMemberUnavailable(ValueError):
    """A refreshed frozen member has no auditable task identity in its batch project."""

    def __init__(self, task_id: str, *, ambiguous: bool = False, foreign_project: bool = False):
        self.task_id = task_id
        if foreign_project:
            self.code = "stack_member_foreign_project"
            detail = "belongs to a different project"
        else:
            self.code = "stack_member_ambiguous" if ambiguous else "stack_member_missing"
            detail = "appears in both tasks and archived_tasks" if ambiguous else "has no task row"
        super().__init__(f"Cannot supersede: refreshed member {task_id} {detail}")


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

    async def freeze(self, batch: Batch, members: Iterable[BatchMember], *,
                     trees: Mapping[str, str], exclusive_target=False,
                     promotion: Mapping | None = None, conn=None):
        """Atomic immutable membership; replay must name exactly the same inputs."""
        members = tuple(members)
        if conn is not None:
            return await self._freeze_locked(conn, batch, members, trees=trees,
                                             exclusive_target=exclusive_target,
                                             promotion=promotion)
        async with self.db.immediate() as owned:
            return await self._freeze_locked(owned, batch, members, trees=trees,
                                             exclusive_target=exclusive_target,
                                             promotion=promotion)

    async def _freeze_locked(self, conn, batch, members, *, trees, exclusive_target, promotion):
        await self._target_lock(conn, batch)
        if exclusive_target:
            await self._exclusive_target(conn, batch)
        return await self._freeze_on(conn, batch, members, trees=trees, promotion=promotion)

    @staticmethod
    async def _target_lock(conn, batch):
        key = repr((batch.project_id, batch.repository_id, batch.target_ref))
        await conn.execute(select(func.pg_advisory_xact_lock(
            func.hashtextextended("git-batch-target:" + key, 0),
        )))

    @staticmethod
    async def _exclusive_target(conn, batch, *, excluding=None):
        other = await conn.scalar(select(integration_batches.c.id).where(
            integration_batches.c.project_id == batch.project_id,
            integration_batches.c.repository_id == batch.repository_id,
            integration_batches.c.target_ref == batch.target_ref,
            integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted",
            integration_batches.c.id != batch.id,
            integration_batches.c.id != (excluding or batch.id),
        ))
        if other:
            raise ValueError("another batch already owns this target")

    async def _freeze_on(self, conn, batch, members, *, trees, promotion=None):
        if tuple(member.order for member in members) != tuple(range(len(members))):
            raise ValueError("membership must have contiguous frozen order")
        if not members:
            raise ValueError("empty work needs no batch")
        if len({member.task_id for member in members}) != len(members):
            raise ValueError("duplicate batch member")
        integration_branch = candidate_ref(batch.id)
        if promotion is not None:
            from src.integration.promotion_steps import promotion_ref

            integration_branch = promotion_ref(promotion["step"], promotion)
        now = self.clock()
        active = (await conn.execute(select(integration_batches.c.id).where(
            integration_batches.c.project_id == batch.project_id,
            integration_batches.c.repository_id == batch.repository_id,
            integration_batches.c.target_ref == batch.target_ref,
            integration_batches.c.id != batch.id,
            integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted",
        ))).scalars().all()
        if active and (batch.epic_refresh or any(
            id_.startswith("train-epic-refresh-") for id_ in active
        )):
            raise ValueError("epic refresh must serialize with its open collection batch")
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
            if promotion is not None and (
                existing["trigger"] != "promotion"
                or existing["request_id"] != promotion["request_id"]
                or existing["policy_snapshot"].get("promotion_step") != promotion["step"]
                or existing["policy_snapshot"].get("promotion_intent") != dict(promotion)
            ):
                raise ValueError("batch id already names a different promotion request")
            return Batch.from_row(existing)
        manifest = hashlib.sha256(repr(members).encode()).hexdigest()
        policy = await conn.scalar(select(projects.c.hierarchical_integration_policy).where(
            projects.c.id == batch.project_id,
        )) or {}
        retention = (policy.get("cleanup") or {}).get("successful_source_refs", "delete")
        await conn.execute(insert(integration_batches).values(
            id=batch.id, project_id=batch.project_id, repository_id=batch.repository_id,
            target_ref=batch.target_ref, intent=batch.intent,
            repair_attempt_count=batch.repair_attempt_count,
            created_at=batch.created_at or now, updated_at=now,
            # Legacy required fields, the immutable cleanup policy snapshot and,
            # for a promotion, its frozen step and intent.
            request_id=batch.id if promotion is None else promotion["request_id"],
            trigger="manual" if promotion is None else "promotion",
            source_manifest_digest=manifest,
            base_sha=members[0].base_sha, integration_branch=integration_branch,
            lifecycle="sealing", policy_snapshot=dict(policy) if promotion is None else {
                **policy,
                "promotion_step": promotion["step"],
                "promotion_intent": dict(promotion),
            }, artifact_snapshot={}, cleanup_state="pending",
        ))
        for member in members:
            tree = trees[member.task_id]
            if not is_valid_git_oid(tree):
                raise ValueError("source tree must be exact")
            source = None
            for table in (tasks, archived_tasks):
                source = (await conn.execute(select(table.c.branch_name, table.c.pr_url).where(
                    table.c.id == member.task_id, table.c.repo_id == batch.repository_id,
                ))).mappings().one_or_none()
                if source is not None:
                    break
            source_ref = ("refs/heads/" + source["branch_name"].removeprefix("refs/heads/")
                          if source and source["branch_name"] else None)
            await conn.execute(insert(integration_batch_members).values(
                batch_id=batch.id, ordinal=member.order, task_id=member.task_id,
                repository_id=batch.repository_id, source_sha=member.source_sha,
                source_base_sha=member.base_sha, reviewed_head_sha=member.source_sha,
                reviewed_tree_sha=tree, review_evidence_id=None, review_evidence={},
                pr_url=source["pr_url"] if source else None,
                source_ref=source_ref,
                source_ref_retention=retention if source_ref else None,
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

    async def eject(self, batch, task_id, replacement, members, *, trees, authorize,
                    dry_run, operator_id, reason):
        """Abort and replace under the publisher's lock; never edit membership.

        The batch owns the operator's release instruction. It changes neither
        the sealed batch identity nor its completion/delivery evidence.
        """
        async with self.db.immediate() as conn:
            await self._target_lock(conn, batch)
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().one()
            if row["intent"] == "aborted" or row["lifecycle"] == "promoted":
                raise ValueError("batch is no longer open or paused")
            if Batch.from_row(row) != batch:
                raise ValueError("batch changed; preview again")
            frozen = (await conn.execute(select(integration_batch_members).where(
                integration_batch_members.c.batch_id == batch.id,
            ).order_by(integration_batch_members.c.ordinal))).mappings().all()
            expected = tuple(replace(BatchMember(r["task_id"], r["source_sha"],
                r["source_base_sha"], r["ordinal"]), order=i)
                for i, r in enumerate(r for r in frozen if r["task_id"] != task_id))
            if not any(r["task_id"] == task_id for r in frozen) or expected != members:
                raise ValueError("frozen batch membership changed")
            source_projects = (await conn.execute(union_all(*(
                select(table.c.project_id).where(table.c.id == task_id)
                for table in (tasks, archived_tasks)
            )))).scalars().all()
            if source_projects != [batch.project_id]:
                raise ValueError("ejected member does not belong to the batch project")
            if not (operator_id or "").strip():
                raise ValueError("ejection requires an operator")
            if not dry_run and not reason.strip():
                raise ValueError("ejection requires a nonblank reason")
            await self._exclusive_target(conn, replacement or batch, excluding=batch.id)
            # Fence reopen/abandon and origin refresh alongside publication.
            ids = sorted(r["task_id"] for r in frozen)
            await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(ids))
                               .order_by(tasks.c.id).with_for_update())
            if not await authorize():
                raise ValueError("batch sources, target or candidate changed; preview again")
            if dry_run:
                return
            instruction = {"batch_id": batch.id, "project_id": batch.project_id,
                           "task_id": task_id,
                           "replacement_batch_id": replacement.id if replacement else None,
                           "operator_id": operator_id, "reason": reason}
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).values(intent="aborted", lifecycle="aborted",
                     human_abort_reason=reason, ejection_record=instruction,
                     cleanup_state="pending", updated_at=self.clock()))
            from src.integration.branch_retirement import request_batch_retirement_on

            await request_batch_retirement_on(conn, row, reason="ejected batch candidate",
                                             now=self.clock())
            if replacement is not None:
                await self._freeze_on(conn, replacement, members, trees=trees)
            import json

            await self.db.log_event("integration.batch_ejected", project_id=batch.project_id,
                task_id=task_id, payload=json.dumps({"batch_id": batch.id,
                    "replacement_batch_id": replacement.id if replacement else None,
                    "operator_id": operator_id, "reason": reason}), conn=conn)

    async def supersede(self, batch: Batch, task_id: str, *, reason: str) -> bool:
        """Abort an open batch a refreshed stack replaced; release its inputs.

        Its frozen candidate can no longer publish the refreshed source, so it
        must not keep the target. Unlike an ordinary abort, its unchanged
        members return to pending. An explicit pause still holds.
        """
        import json

        if not reason.strip():
            raise ValueError("supersede requires a nonblank reason")
        async with self.db.immediate() as conn:
            await self._target_lock(conn, batch)
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().one_or_none()
            if row is None or row["intent"] != "open" or row["lifecycle"] == "promoted":
                return False
            if Batch.from_row(row) != batch:
                return False
            member = await conn.scalar(select(integration_batch_members.c.task_id).where(
                integration_batch_members.c.batch_id == batch.id,
                integration_batch_members.c.task_id == task_id,
                integration_batch_members.c.repository_id == batch.repository_id,
            ))
            source_projects = (await conn.execute(union_all(*(
                select(table.c.project_id).where(table.c.id == task_id)
                for table in (tasks, archived_tasks)
            )))).scalars().all()
            if member is None:
                raise ValueError("superseded member does not belong to the frozen batch project")
            if len(source_projects) != 1:
                raise SupersedeMemberUnavailable(task_id, ambiguous=bool(source_projects))
            if source_projects != [batch.project_id]:
                raise SupersedeMemberUnavailable(task_id, foreign_project=True)
            instruction = {"batch_id": batch.id, "project_id": batch.project_id,
                           "task_id": task_id, "replacement_batch_id": None,
                           "operator_id": "service:integration-train", "reason": reason}
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).values(intent="aborted", lifecycle="aborted",
                     human_abort_reason=reason, ejection_record=instruction,
                     cleanup_state="pending", updated_at=self.clock()))
            from src.integration.branch_retirement import request_batch_retirement_on

            await request_batch_retirement_on(conn, row, reason="superseded batch candidate",
                                             now=self.clock())
            await self.db.log_event("integration.batch_superseded", project_id=batch.project_id,
                task_id=task_id, payload=json.dumps(instruction),
                conn=conn)
        return True

    async def set_intent(self, batch_id: str, intent: str, *, dry_run=False,
                         authorize=None, operator_id=None, reason=""):
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
            if row["lifecycle"] == "promoted" and intent != "aborted":
                raise ValueError("promoted batch intent cannot change")
            if intent == "aborted" and row["lifecycle"] == "promoted":
                raise ValueError("promoted batch cannot be aborted")
            if intent == "paused" and row["intent"] != "open":
                raise ValueError("only an open batch can be paused")
            if intent == "open" and row["intent"] != "paused":
                raise ValueError("only a paused batch can be resumed")
            if authorize is not None and not await authorize():
                raise ValueError("batch target or candidate changed; preview again")
            if dry_run:
                return
            values = {"intent": intent, "updated_at": self.clock()}
            if intent == "aborted":
                # Compatibility guards still read lifecycle. Commit the terminal
                # projection with intent so members can be revised immediately.
                values.update(
                    lifecycle="aborted",
                    human_abort_reason=reason.strip() or row["human_abort_reason"] or "Batch aborted",
                )
                if row["lifecycle"] != "aborted":
                    values["cleanup_state"] = "pending"
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch_id,
            ).values(**values))
            if intent == "aborted":
                from src.integration.branch_retirement import request_batch_retirement_on

                await request_batch_retirement_on(conn, row, reason="aborted batch candidate",
                                                 now=self.clock())
            if operator_id is not None:
                import json

                await self.db.log_event(
                    "integration.batch_intent", project_id=row["project_id"],
                    payload=json.dumps({"batch_id": batch_id, "intent": intent,
                                        "operator_id": operator_id, "reason": reason}), conn=conn,
                )

    async def reconcile_aborted(self, *, target=None, limit=100):
        """Release compatibility seals left by pre-fix aborts; never undo promotion."""
        pending = select(integration_batches.c.id).where(
            integration_batches.c.target_ref.is_not(None),
            integration_batches.c.intent == "aborted",
            integration_batches.c.lifecycle.not_in(("aborted", "promoted")),
        )
        if target is not None:
            pending = pending.where(
                integration_batches.c.project_id == target.project_id,
                integration_batches.c.repository_id == target.repository_id,
                integration_batches.c.target_ref == target.target_ref,
            )
        async with self.db.immediate() as conn:
            ids = list((await conn.execute(pending.order_by(integration_batches.c.id)
                .limit(limit).with_for_update(skip_locked=True))).scalars())
            if ids:
                await conn.execute(update(integration_batches).where(
                    integration_batches.c.id.in_(ids),
                ).values(lifecycle="aborted", cleanup_state="pending",
                    human_abort_reason=func.coalesce(
                        integration_batches.c.human_abort_reason, "Batch aborted"),
                    updated_at=self.clock()))


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
        attest: Callable[[Batch, str], Awaitable[str]] | None = None,
        require_attestation: bool = True,
    ):
        self.store, self.gitops = store, gitops
        self.publish, self.eligible, self.gate = publish, eligible, gate
        # Only hosted targets carry the App attestation ruleset; the local
        # validation lane publishes on its own gate and never attests.
        self.attest, self.require_attestation = attest, require_attestation

    async def _authorized(self, batch, members):
        from src.operator_decisions import OperatorDecisions

        if await OperatorDecisions(self.store.db).holds("batch", batch.id):
            return False
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
        members, trees = await self.freeze_inputs(
            batch, members, requests=requests, snapshot=snapshot, dependencies=dependencies,
        )
        return await self.store.freeze(batch, members, trees=trees, exclusive_target=True)

    async def freeze_inputs(self, batch, members, *, requests, snapshot, dependencies=None):
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
        return members, trees

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
        except (GitError, RuntimeError, ValueError) as exc:
            logger.warning("integration batch %s publication of %s at %s failed: %s",
                           batch.id, ref, sha, exc)
        actual = await self.gitops.remote(repo, ref)
        if actual == sha:
            return "published"
        return "moved" if (actual or ZERO) != expected else "unknown"

    async def repair_authorized(self, batch, members, head_sha):
        """Recheck the exact published repair start under the allocator's ref lock."""
        repo = await self.gitops.repository(batch)
        return (await self._authorized(batch, members) and
                await self.gitops.remote(repo, candidate_ref(batch.id)) == head_sha)

    async def repair_completion_blocker(self, batch, task_id, starting_sha, snapshot):
        """An unchanged close missing this target cannot spend another allocation.

        Use the immutable completion source from the fetched Git snapshot: the
        candidate ref may already have been reset to this rebuild's partial head.
        A reported commit or the mutable candidate cannot prove the worker's delta.
        """
        from src.integration.delivery_truth import load_delivery_requests

        requests = await load_delivery_requests(
            self.store.db, (task_id,), repository_id=batch.repository_id,
            target_ref=batch.target_ref, reduced=True,
        )
        request = requests.get(task_id)
        repo = await self.gitops.repository(batch)
        provenance = GitProvenance(self.gitops.git, str(repo.store),
                                   repository_url=snapshot.observation.repository_url)
        record = None
        if request and request.completion_id:
            record = await provenance.read_completion(
                CompletionIdentity(batch.project_id, batch.repository_id, task_id,
                                   request.completion_id),
                refs=snapshot.observation.source_heads,
            )
        if record is None:
            return {"reason": "repair_completion_unconfirmed", "task_id": task_id,
                    "starting_sha": starting_sha, "target_sha": snapshot.target_oid}
        completed = record["source_oid"]
        if (completed == starting_sha
            and not await self.gitops.is_ancestor(repo, snapshot.target_oid, completed)):
            return {"reason": "repair_no_progress_missing_target", "task_id": task_id,
                    "starting_sha": starting_sha, "completed_sha": completed,
                    "target_sha": snapshot.target_oid}
        return None

    async def visit(self, batch: Batch, members: Iterable[BatchMember], snapshot: GitTruthSnapshot):
        """Derive progress from this visit's fetched refs; never from legacy lifecycle."""
        from src.operator_decisions import OperatorDecisions

        holds = await OperatorDecisions(self.store.db).holds("batch", batch.id)
        if holds:
            return BatchObservation("held", detail={"operator_decisions": holds})
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
            # A published repair start can be the target itself or only a
            # partial merge. Candidate inclusion alone cannot settle members.
            proofs = [await snapshot.contains_source(m.task_id, m.source_sha, m.base_sha)
                      for m in members]
            if members and all(proof is True for proof in proofs) and not (
                batch.epic_sync or batch.epic_refresh
            ):
                return BatchObservation("delivered", candidate, target)
            if any(proof is None for proof in proofs):
                return BatchObservation("unknown", candidate, target)
            if not await self._authorized(batch, members):
                return BatchObservation("held", candidate, target)
            # A repaired candidate is reusable only while it retains this target
            # and contains every frozen source as an ancestor. Source trailers
            # remain historical delivery evidence, never repair completeness.
            retains_target = bool(candidate and await self.gitops.is_ancestor(repo, target, candidate))
            reusable = retains_target
            if reusable:
                reusable = all([
                    await self.gitops.is_ancestor(repo, member.source_sha, candidate)
                    for member in members
                ])
            if not reusable:
                # Continue a partial repair from its resolved overlay while the
                # target is still an ancestor. Rebuilding from the target would
                # discard the repair of the first conflict before later members.
                result = await self.gitops.merge_sources(repo, candidate if retains_target else target,
                                                        members,
                                                        created_at=batch.created_at)
                if result["outcome"] != "merged":
                    logger.warning("integration batch %s build %s on %s: %s",
                                   batch.id, result["outcome"], batch.target_ref, result)
                    if result["outcome"] == "conflict":
                        # A conflict may follow successful member merges. Retain
                        # their exact head as the repair's start, through the same
                        # expected-old managed publisher as a complete candidate.
                        head = result["head"]
                        transferred = await self._transfer(
                            batch, repo, ref, head, candidate or ZERO,
                            lambda: self._authorized(batch, members),
                        )
                        if transferred != "published":
                            return BatchObservation(transferred, None, target, detail={
                                **result, "repair_publication": transferred,
                                "repair_start_sha": head,
                            })
                        if candidate and candidate != head:
                            result = {**result, "replaced_candidate_sha": candidate}
                        candidate = head
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
                        (batch.epic_refresh or
                         await self.gitops.remote(repo, ref) == candidate) and
                        await self.gate(batch, candidate, tree))

            # Explicit expected-old CAS and ancestry enforce the exact final FF.
            if not await self.gitops.is_ancestor(repo, target, candidate):
                return BatchObservation("moved", candidate, target, tree)
            # Provider I/O stays outside the publication fence. The final
            # authorization still rechecks intent, candidate and cached checks.
            if self.require_attestation:
                attestation = "unavailable"
                if self.attest is not None:
                    try:
                        attestation = await self.attest(batch, candidate)
                    except Exception as exc:
                        logger.warning("integration batch %s attestation failed: %s", batch.id, exc)
                if attestation not in {"published", "already_published"}:
                    return BatchObservation("held", candidate, target, tree, detail={
                        "outcome": "attestation_unavailable", "attestation_outcome": attestation,
                    })
            state = await self._transfer(batch, repo, batch.target_ref, candidate, target, authorize)
            if state == "published":
                # The fast-forward left the target at the candidate.
                return BatchObservation("delivered", candidate, candidate, tree)
            return BatchObservation(state, candidate, target, tree)
        except (GitError, OSError, ValueError) as exc:
            logger.warning("integration batch %s observation failed on %s: %s",
                           batch.id, batch.target_ref, exc)
            return BatchObservation("unknown", candidate, target, detail={"reason": str(exc)})
