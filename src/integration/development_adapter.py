"""Incremental Development adapter for the shared subject reconciler and ports.

The command owner supplies the existing completion/delivery snapshot reader,
retained repositories, reviewed artifact loader and writer/gate/cleanup ports.
Construction does not activate a project, take a lease or transfer an engine.
Membership and exact-member parks live in the shared append-only journal.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from sqlalchemy import select

from src.database.tables import integration_branch_owners
from src.integration.ci_adapters import CIAdapters
from src.integration.ci_producers import LocalCIProducer, LocalValidationPlan, digest
from src.integration.development_policy import PinnedDevelopmentPolicy
from src.integration.gitops import RetainedRepository
from src.integration.models import BranchKey, Fence
from src.integration.runtime_contracts import (
    CIState,
    JournalMode,
    MemberFacts,
    MergeMembersArgs,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    SealArgs,
    Subject,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    subject_key,
)
from src.playbooks.integration_policy import IntegrationPolicyFacts


if TYPE_CHECKING:
    from src.integration.reconciler import IntegrationReconciler

@dataclass(frozen=True)
class DevelopmentMember:
    facts: MemberFacts
    completed: bool = True
    pushed: bool = True
    completed_at: float = 0
    dependencies: frozenset[str] = frozenset()
    # The existing repair contract + ancestry reader proves these exact heads
    # are ancestors of this completed repair. A mere task-name link is no proof.
    carries: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class DevelopmentFrontier:
    """Existing delivery truth supplies exact provenance, never live branch tips.

    ``satisfied`` includes prerequisites proven contained or explicitly settled
    by the existing delivery reader. Missing/unknown prerequisites are absent.
    Repair replacement ancestry is resolved by that same reader before here.
    """

    members: tuple[DevelopmentMember, ...]
    satisfied: frozenset[str] = frozenset()
    parked: frozenset[tuple[str, str]] = frozenset()


def ordered_development_members(frontier: DevelopmentFrontier, limit: int) -> tuple[MemberFacts, ...]:
    """Dependency-order the proven frontier; a parked member holds only its descendants."""
    if limit <= 0:
        raise ValueError("a development batch cap must be positive")
    candidates = sorted(frontier.members, key=lambda m: (m.completed_at, m.facts.task_id))
    if len({m.facts.task_id for m in candidates}) != len(candidates):
        raise ValueError("development frontier repeats a task identity")
    satisfied = set(frontier.satisfied)
    satisfied.update(m.facts.task_id for m in candidates if m.facts.ancestry == "contained")
    replacements = {
        task_id: repair.facts.task_id for repair in candidates
        if repair.completed and repair.pushed and not repair.facts.held
        and repair.facts.review != "rejected" and not repair.facts.ejected
        and (repair.facts.task_id, repair.facts.head_sha) not in frontier.parked
        for task_id, source in repair.carries
        if any(m.facts.task_id == task_id and m.facts.head_sha == source for m in candidates)
    }
    pending = [
        m for m in candidates
        if m.completed and m.pushed and m.facts.head_sha and m.facts.base_sha
        and m.facts.task_id not in satisfied
        and m.facts.task_id not in replacements
        and not m.facts.held and not m.facts.ejected and m.facts.review != "rejected"
        and (m.facts.task_id, m.facts.head_sha) not in frontier.parked
    ]
    ordered = []
    while pending and len(ordered) < limit:
        def required(member):
            return {replacements.get(task_id, task_id) for task_id in member.dependencies
                    if task_id == member.facts.task_id
                    or replacements.get(task_id, task_id) != member.facts.task_id}

        ready = [m for m in pending if required(m) <= satisfied]
        if not ready:
            break  # Cycles and missing/parked prerequisites remain explicit waits.
        for member in ready:
            ordered.append(member.facts)
            satisfied.add(member.facts.task_id)
            satisfied.update(task_id for task_id, source in member.carries
                             if replacements.get(task_id) == member.facts.task_id)
            pending.remove(member)
            if len(ordered) == limit:
                break
    return tuple(ordered)




PolicyLoader = Callable[[str], Awaitable[PinnedDevelopmentPolicy]]
FrontierReader = Callable[[Subject], Awaitable[DevelopmentFrontier]]
RepositoryResolver = Callable[[Subject], Awaitable[RetainedRepository]]


class _PinnedPolicyRouter:
    def __init__(self, load: PolicyLoader):
        self.load = load

    async def decide(self, subject, facts):
        return (await self.load(subject.policy.artifact_sha256)).compiled.evaluate(subject, facts)

    async def settle(self, subject, decision, outcome, *, now):
        from src.integration.reconciler import CompiledPolicyAdapter

        policy = (await self.load(subject.policy.artifact_sha256)).compiled
        return await CompiledPolicyAdapter(policy).settle(subject, decision, outcome, now=now)


class DevelopmentIntegrationAdapter:
    def __init__(
        self,
        db: Any,
        *,
        observe: Callable[[Subject], Awaitable[SubjectFacts]],
        frontier_for: FrontierReader,
        policy_for: PolicyLoader,
        repository_for: RepositoryResolver,
        shared_ports: PrimitivePorts,
        job_client: Any,
        clock: Callable[[], float] = time.time,
    ):
        self.db, self.observe_existing, self.frontier_for = db, observe, frontier_for
        self.policy_for, self.repository_for = policy_for, repository_for
        self.shared, self.job_client, self.clock = shared_ports, job_client, clock

    async def _pinned(self, subject: Subject) -> PinnedDevelopmentPolicy:
        pinned = await self.policy_for(subject.policy.artifact_sha256)
        if (pinned.compiled.pin != subject.policy
                or pinned.definition.scope.project_id != subject.project_id):
            raise ValueError("development policy does not match subject/project pin")
        return pinned

    def reconciler(self, *, mode: JournalMode = JournalMode.SHADOW,
                   subject_db=None, **options) -> IntegrationReconciler:
        """Install at the existing remote-pass boundary only after rollout evidence."""
        from src.integration.reconciler import IntegrationReconciler

        ports = PrimitivePorts()
        for primitive in self.shared.bound - {Primitive.SEAL, Primitive.GIT_MERGE_MEMBERS,
                                               Primitive.CI_REQUEST, Primitive.CI_OBSERVE,
                                               Primitive.CI_ATTEST}:
            ports.bind(primitive, self.shared.invoke)
        ports.bind(Primitive.SEAL, self.seal)
        ports.bind(Primitive.GIT_MERGE_MEMBERS, self.merge)
        CIAdapters(self.db, self.producer_for, clock=self.clock).bind(ports)
        return IntegrationReconciler(
            subject_db or self.db, self.observe, _PinnedPolicyRouter(self.policy_for), ports,
            mode=mode, kinds=(SubjectKind.ROOT_BATCH, SubjectKind.SOURCE),
            clock=self.clock, **options,
        )

    async def _journal(self, subject: Subject) -> list[dict]:
        rows, after = [], None
        while True:
            page = await self.db.list_integration_subject_journal(
                subject.id, after_seq=after, limit=100, entry_kinds=("action",),
            )
            rows.extend(page)
            if len(page) < 100:
                return rows
            after = page[-1]["seq"]

    async def _record_on(self, conn, subject, key, primitive, outcome, payload):
        return await self.db.append_integration_subject_journal_on(conn, {
            "subject_id": subject.id, "entry_kind": "action", "mode": "active",
            "idempotency_key": key, "policy_artifact_sha256": subject.policy.artifact_sha256,
            "subject_version": subject.version, "phase": subject.phase.value,
            "head_sha": subject.head_sha, "generation": subject.generation,
            "primitive": primitive.value, "outcome": outcome, "payload": payload,
            "recorded_at": self.clock(),
        })

    @staticmethod
    def _manifest(rows) -> Mapping | None:
        return next((row["payload"] for row in rows
                     if row["idempotency_key"] == "development:manifest"), None)

    async def _frontier(self, subject, rows) -> DevelopmentFrontier:
        current = await self.frontier_for(subject)
        manifest = self._manifest(rows)
        if manifest is None:
            return current
        # The immutable manifest is authoritative after seal. Fresh truth may
        # add holds/containment, but never silently substitutes a new revision.
        fresh = {m.facts.task_id: m for m in current.members}
        members = []
        for frozen in manifest["members"]:
            facts = MemberFacts.model_validate(frozen["facts"])
            now = fresh.get(facts.task_id)
            exact = now is not None and (
                now.facts.head_sha, now.facts.base_sha, now.facts.generation
            ) == (facts.head_sha, facts.base_sha, facts.generation)
            if exact:
                facts = facts.model_copy(update={
                    "held": now.facts.held, "ejected": now.facts.ejected,
                    "review": now.facts.review, "ancestry": now.facts.ancestry,
                })
            else:
                facts = facts.model_copy(update={"held": True})
            members.append(DevelopmentMember(
                facts, completed_at=frozen["completed_at"],
                dependencies=frozenset(frozen["dependencies"]),
            ))
        frozen_heads = {(m.facts.task_id, m.facts.head_sha) for m in members}
        members.extend(m for m in current.members if m.carries & frozen_heads
                       and m.facts.task_id not in {f.facts.task_id for f in members})
        parks = frozenset(
            (row["payload"]["member"], row["payload"]["source_sha"])
            for row in rows if row["idempotency_key"].startswith("development:park:")
        )
        return DevelopmentFrontier(tuple(members), current.satisfied, current.parked | parks)

    async def observe(self, subject: Subject) -> IntegrationPolicyFacts:
        facts = await self.observe_existing(subject)
        pinned = await self._pinned(subject)
        if subject.kind is SubjectKind.SOURCE:
            # Source subjects carry independently parked work. Existing writer,
            # gate, stop-proof and ancestry readers remain authoritative.
            frontier = await self.frontier_for(subject)
            value = facts.model_dump(exclude={"publisher_fence"})
            if subject.task_id in frontier.satisfied:
                value["members"] = ()
            else:
                replacements = tuple(m.facts for m in frontier.members
                                     if (subject.task_id, subject.head_sha) in m.carries
                                     and m.completed and m.pushed and not m.facts.held
                                     and not m.facts.ejected and m.facts.review != "rejected"
                                     and (m.facts.task_id, m.facts.head_sha) not in frontier.parked)
                if replacements:
                    value["members"] = replacements
            value["unknown"] = tuple(reason for reason in facts.unknown
                                     if reason != "publisher_fence_unavailable")
            return IntegrationPolicyFacts(**value)
        rows = await self._journal(subject)
        frontier = await self._frontier(subject, rows)
        members = ordered_development_members(frontier, pinned.settings.max_batch_size)
        ci = facts.ci
        if subject.head is not None and pinned.settings.validation != "none":
            observation = await (await self.producer_for(subject)).observe(subject, subject.head)
            ci = (observation,)
        value = facts.model_dump(exclude={"publisher_fence"})
        value.update(members=members, ci=ci, candidate=None, head=subject.head,
                     conflicts=(), budget=subject.budget, writer=subject.writer)
        # Per-member holds/unknowns do not become a project-wide stop. Project
        # holds, remote ambiguity and all other unknown facts stay binding.
        member_ids = {m.facts.task_id for m in frontier.members}
        value["holds"] = tuple(h for h in facts.holds if h.task_id not in member_ids)
        value["unknown"] = tuple(reason for reason in facts.unknown if not reason.startswith((
            "member_head_missing:", "ancestry_unknown:", "publisher_fence_unavailable",
        )))
        async with self.db._engine.connect() as conn:
            owner = (await conn.execute(select(integration_branch_owners).where(
                integration_branch_owners.c.repository_id == subject.repository_id,
                integration_branch_owners.c.ref == subject.target_ref,
            ))).mappings().first()
        fence = None
        if owner and owner["owner_id"] == (subject.writer.task_id or subject.id) and (
            owner["handoff_state"] in {"reserved", "attached"}
            and (owner.get("expires_at") is None or owner["expires_at"] > self.clock())
        ):
            fence = Fence(target=BranchKey(repository_id=subject.repository_id,
                                          branch=subject.target_ref),
                          owner_id=owner["owner_id"], token=owner["fence_token"])
        return IntegrationPolicyFacts(**value, publisher_fence=fence)

    async def producer_for(self, subject: Subject) -> LocalCIProducer:
        pinned = await self._pinned(subject)
        rows = await self._journal(subject)
        # Only distinct infrastructure observations retry execution. They do
        # not consume a conclusive attempt or a repair generation.
        infra = {digest(row["payload"]["evidence"]) for row in rows
                 if row["primitive"] == Primitive.CI_OBSERVE.value
                 and row["outcome"] == CIState.INFRA.value
                 and row["payload"].get("evidence", {}).get("head_sha") == subject.head_sha
                 and "evidence" in row["payload"]}
        settings = pinned.settings
        repo = await self.repository_for(subject)
        return LocalCIProducer(self.db, self.job_client, store=repo.store, plan=LocalValidationPlan(
            version=subject.policy.artifact_sha256,
            attempt_id=f"{subject.generation}:{len(infra)}",
            commands=tuple(settings.commands) if settings.validation != "none" else (),
            queue_seconds=settings.slot_wait_seconds, run_seconds=settings.timeout_seconds,
        ), clock=self.clock)

    async def seal(self, subject: Subject, args: SealArgs) -> PrimitiveOutcome:
        if args.admission.task_kinds or args.admission.include_authorized:
            return PrimitiveOutcome.unknown(args.primitive, "admission_filter_unavailable")
        rows = await self._journal(subject)
        manifest = self._manifest(rows)
        if manifest is None:
            frontier = await self.frontier_for(subject)
            frontier = DevelopmentFrontier(tuple(
                m for m in frontier.members
                if (not args.admission.require_review or m.facts.review == "approved")
                and (not args.admission.require_source_ci or m.facts.ci is CIState.GREEN)
            ), frontier.satisfied, frontier.parked)
            members = ordered_development_members(frontier, args.admission.max_members or 500)
            if not members:
                return PrimitiveOutcome(primitive=args.primitive, outcome="empty")
            by_id = {m.facts.task_id: m for m in frontier.members}
            manifest = {"members": [{
                "facts": m.model_dump(mode="json"),
                "dependencies": sorted(by_id[m.task_id].dependencies),
                "completed_at": by_id[m.task_id].completed_at,
            } for m in members]}
        async with self.db.immediate() as conn:
            row = await self.db.lock_integration_subject_on(conn, subject.id)
            if not row or row["version"] != subject.version or row["engine"] != "reconciler":
                return PrimitiveOutcome.unknown(args.primitive, "subject_changed")
            await self._record_on(conn, subject, "development:manifest", args.primitive,
                                  "sealed", manifest)
        return PrimitiveOutcome(primitive=args.primitive, outcome="sealed")

    async def merge(self, subject: Subject, args: MergeMembersArgs) -> PrimitiveOutcome:
        result = await self.shared.invoke(subject, args)
        if result.outcome == "conflict":
            member = next(m for m in args.members if m.task_id == result.detail["member"])
            now = self.clock()
            key = subject_key(SubjectKind.SOURCE, subject.repository_id, member.task_id,
                              member.head_sha, subject.target_ref)
            parked = Subject(
                id="development-source-" + digest(key)[:32], project_id=subject.project_id,
                repository_id=subject.repository_id, kind=SubjectKind.SOURCE, subject_key=key,
                engine=subject.engine, phase=SubjectPhase.REPAIRING, policy=subject.policy,
                task_id=member.task_id, target_ref=subject.target_ref, head_sha=member.head_sha,
                base_sha=member.base_sha, generation=0,
                schedule=SubjectSchedule.progress(now=now,
                                                  max_wait_seconds=subject.schedule.max_wait_seconds),
                created_at=now, updated_at=now,
            )
            async with self.db.immediate() as conn:
                row = await self.db.lock_integration_subject_on(conn, subject.id)
                if not row or row["version"] != subject.version or row["engine"] != "reconciler":
                    return PrimitiveOutcome.unknown(args.primitive, "subject_changed")
                await self.db.ensure_integration_subject_on(conn, parked.to_row())
                await self._record_on(conn, subject, "development:park:" + digest(key),
                                      args.primitive, "conflict", {
                                          **result.detail, "source_sha": member.head_sha,
                                          "parked_subject_id": parked.id,
                                      })
            return result
        if result.outcome == "merged":
            head = result.detail["head"]
            return result.model_copy(update={"detail": {**result.detail, "subject_values": {
                "head_sha": head, "base_sha": args.base_sha,
                "generation": subject.generation + int(head != subject.head_sha),
                "writer_status": "none", "writer_task_id": None, "writer_fence_token": None,
                "writer_session_id": None, "writer_claimed_at": None,
                "writer_last_push_at": None, "writer_stop_proof": None,
            }}})
        return result
