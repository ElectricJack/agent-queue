"""Database and daemon sources for the reduced integration train.

Everything here is read from durable rows and Git at visit time: the targets a
project's completed work routes to, the exact completion sources not yet in
those targets, and the open batch's frozen inputs. Nothing is journalled. A
batch's ``intent`` is the only operator control; aborting one withholds its
exact (task, source) inputs from that target until the task completes again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from sqlalchemy import and_, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_legacy_deliveries,
    projects,
    repos,
    task_branch_origins,
    task_dependencies,
    tasks,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.batches import Batch, BatchMember, BatchObservation, BatchService, BatchStore
from src.integration.delivery_observer import DeliveryTarget, delivery_targets
from src.integration.delivery_truth import DeliveryState, load_delivery_requests
from src.integration.git_truth import GitTruth, GitTruthSnapshot
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND
from src.integration.subjects import Subject
from src.integration.train import (
    BatchSelection,
    CandidateChecks,
    IntegrationTrain,
    TrainLane,
    TrainTarget,
)

logger = logging.getLogger(__name__)

TRAIN_MODES = ("train", "hierarchy", "development")
TRAIN_HOLDER = "service:integration-train"
# A local branch keeps a candidate reachable from the retained store, so a
# detached validation clone sees it. Never pushed.
RETAINED_CANDIDATE_PREFIX = "refs/heads/aq/train-candidate/"
MEMBER_LIMIT = 200


def batch_id(target: TrainTarget, members: Iterable[BatchMember]) -> str:
    """Deterministic over the target and its exact inputs, independent of order."""
    inputs = sorted((member.task_id, member.source_sha, member.source_base_sha)
                    for member in members)
    digest = hashlib.sha256(repr((target.key, inputs)).encode()).hexdigest()
    return "train-" + digest[:32]


def _branch(ref: str) -> str:
    return "refs/heads/" + str(ref).removeprefix("refs/heads/")


async def _pending_tasks(conn, project_id: str, repository_id: str, *, limit: int | None) -> list[str]:
    """Completed tasks with a live branch origin in the repository, newest first."""
    rows = await conn.execute(
        select(tasks.c.id)
        .select_from(tasks.join(task_branch_origins, and_(
            task_branch_origins.c.task_id == tasks.c.id,
            task_branch_origins.c.repository_id == repository_id,
            task_branch_origins.c.retired_at.is_(None),
        )))
        .where(tasks.c.project_id == project_id, tasks.c.status == "COMPLETED")
        .distinct()
        .order_by(tasks.c.id)
    )
    ids = list(rows.scalars().all())
    if limit is None or len(ids) <= limit:
        return ids
    newest = await conn.execute(
        select(tasks.c.id).where(tasks.c.id.in_(ids))
        .order_by(tasks.c.updated_at.desc(), tasks.c.id).limit(limit)
    )
    return list(newest.scalars().all())


async def project_snapshot(db, target: TrainTarget) -> GitTruthSnapshot | None:
    """Observe the project root through the daemon's isolated delivery store."""
    observer = getattr(db, "_delivery_observer", None)
    if observer is None:
        return None
    repo = await db.get_repo(target.repository_id)
    observed = await observer.snapshot(DeliveryTarget(
        target.project_id, target.repository_id, repo.url, target.target_ref,
    ))
    return observed if isinstance(observed, GitTruthSnapshot) else GitTruthSnapshot(
        GitTruth(observer.git), observed,
    )


async def project_delivered(db, ids, *, project_id, repository_id, target_ref, snapshot=None):
    """Current completions already delivered beyond their epic.

    A provenance attestation locates an exact source; only its reachability
    proves delivery. An ordinary worker's provenance marker alone never does.
    Historical delivery attestations must name this project/repository/root
    and cover the current completion, never a later reopened generation.
    """
    requests = await load_delivery_requests(
        db, ids, repository_id=repository_id, target_ref=target_ref, reduced=True,
    )
    requests = {task_id: request for task_id, request in requests.items()
                if (request.project_id, request.repository_id) == (project_id, repository_id)}
    async with db._engine.connect() as conn:
        legacy = (await conn.execute(select(integration_legacy_deliveries).where(
            integration_legacy_deliveries.c.task_id.in_(ids),
            integration_legacy_deliveries.c.project_id == project_id,
            integration_legacy_deliveries.c.repository_id == repository_id,
            integration_legacy_deliveries.c.target_ref.in_(
                (target_ref, target_ref.removeprefix("refs/heads/"))),
        ))).mappings().all()
    delivered = set()
    for row in legacy:
        request = requests.get(row["task_id"])
        if request is None or request.task_status != "COMPLETED":
            continue
        if (request.completed_at is not None and request.completed_at <= row["created_at"]
                or request.completion_id == request.legacy_generation
                and request.task_version <= row["created_at"]):
            delivered.add(request.task_id)
    if (snapshot is None or snapshot.error or not snapshot.target_oid
            or (snapshot.observation.project_id, snapshot.observation.repository_id,
                snapshot.observation.target_ref) != (project_id, repository_id, target_ref)):
        return delivered
    observed = snapshot.observation
    provenance = GitProvenance(observed.git, observed.store,
                               repository_url=observed.repository_url)
    for task_id, request in requests.items():
        if task_id in delivered or request.task_status != "COMPLETED":
            continue
        try:
            record = await provenance.read_completion(CompletionIdentity(
                project_id, repository_id, task_id,
                request.completion_id or request.legacy_generation,
            ), refs=observed.source_heads)
            if record is None:
                continue
            source = record["source_oid"]
            facts = snapshot.truth._pair(observed, source)
            if facts.ancestor is None:
                facts.ancestor = await provenance.ancestor(source, snapshot.target_oid)
            if not record["artifact"] or facts.ancestor:
                delivered.add(task_id)
        except (GitError, OSError, ValueError, KeyError, TypeError):
            # Unknown root evidence never suppresses owed work.
            continue
    return delivered


def _open_batch_rows(project_id: str, repository_id: str):
    return select(integration_batches).where(
        integration_batches.c.project_id == project_id,
        integration_batches.c.repository_id == repository_id,
        integration_batches.c.target_ref.is_not(None),
        integration_batches.c.intent != "aborted",
        integration_batches.c.lifecycle != "promoted",
    )


class DatabaseTargets:
    """Every branch the train owns: each train-mode project's default ref, the
    parent branches its completed work routes to, and any open batch's target."""

    def __init__(self, db, *, limit: int = MEMBER_LIMIT, snapshot=project_snapshot,
                 probe_timeout_seconds: float = 5.0) -> None:
        self.db, self.limit = db, limit
        self.snapshot = snapshot
        self.probe_timeout_seconds = probe_timeout_seconds

    async def targets(self, now: float) -> list[TrainTarget]:
        found: dict[tuple[str, str, str], TrainTarget] = {}
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(projects.c.id, projects.c.hierarchical_integration_mode,
                       repos.c.id.label("repository_id"), repos.c.default_branch)
                .select_from(projects.join(
                    repos, repos.c.id == projects.c.integration_repository_id))
                .where(projects.c.hierarchical_integration_mode.in_(TRAIN_MODES),
                       projects.c.status == "ACTIVE")
                .order_by(projects.c.id)
            )).mappings().all()
        # Root probes run concurrently and outside a held database connection.
        # A stalled probe leaves work owed; visits perform their own root proof.
        for targets in await asyncio.gather(*(self._project_targets(row) for row in rows)):
            for target in targets:
                found.setdefault(target.key, target)
        return [found[key] for key in sorted(found)]

    async def _project_targets(self, row):
        project_id, repository_id = row["id"], row["repository_id"]
        default = _branch(row["default_branch"])
        kind = "development" if row["hierarchical_integration_mode"] == "development" else "root"
        root = TrainTarget(project_id, repository_id, default, kind)
        async with self.db._engine.connect() as conn:
            pending = await _pending_tasks(conn, project_id, repository_id, limit=None)
            routed = await delivery_targets(conn, pending, reduced=True)
            batches = (await conn.execute(
                _open_batch_rows(project_id, repository_id))).mappings().all()
        delivered = set()
        if pending:
            try:
                async with asyncio.timeout(self.probe_timeout_seconds):
                    observed = await self.snapshot(self.db, root)
                    delivered = await project_delivered(
                        self.db, pending, project_id=project_id, repository_id=repository_id,
                        target_ref=default, snapshot=observed,
                    )
            except (TimeoutError, GitError, OSError):
                logger.warning("integration root discovery probe unavailable for %s", root.key)
        refs = {default, *(batch["target_ref"] for batch in batches)}
        refs.update(target.target_ref for task_id, target in routed.items()
                    if task_id not in delivered and
                    (target.project_id, target.repository_id) == (project_id, repository_id))
        return [root if ref == default else TrainTarget(project_id, repository_id, ref, "epic")
                for ref in sorted(refs)]


class DatabaseBatches:
    """The open batch of a target, frozen from exact undelivered completions."""

    def __init__(self, db, *, limit: int = MEMBER_LIMIT, clock: Callable[[], float] = time.time):
        self.db, self.limit, self.clock = db, limit, clock

    async def open_batch(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, service: BatchService
    ) -> BatchSelection:
        current = await self.current(target)
        blockers: list[dict[str, Any]] = []
        pending = None
        if not snapshot.error and snapshot.target_oid:
            pending = await self.pending(target, snapshot, blockers=blockers)
        if current is not None:
            members = await service.store.members(current.id)
            # Old frozen inputs also stay out of duplicate epic publication.
            if target.kind == "epic":
                delivered = await self.delivered(target, snapshot, [m.task_id for m in members])
                if delivered:
                    blockers.append({
                        "code": "batch_inputs_delivered_to_project", "ref": current.id,
                        "detail": "batch contains work already delivered to the project root; "
                                  "abort the batch before retiring its origins",
                        "task_ids": sorted(delivered),
                    })
                    return BatchSelection(blockers=tuple(blockers))
            return BatchSelection(current, members, tuple(blockers))
        if pending is None:
            return BatchSelection(blockers=tuple(blockers))
        members, requests, dependencies = pending
        batch = Batch(id=batch_id(target, members), project_id=target.project_id,
                      repository_id=target.repository_id, target_ref=target.target_ref,
                      created_at=self.clock())
        frozen = await service.freeze(batch, members, requests=requests, snapshot=snapshot,
                                      dependencies=dependencies)
        return BatchSelection(frozen, await service.store.members(frozen.id), tuple(blockers))

    async def current(self, target: TrainTarget) -> Batch | None:
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(
                _open_batch_rows(target.project_id, target.repository_id)
                .where(integration_batches.c.target_ref == target.target_ref)
                .order_by(integration_batches.c.created_at, integration_batches.c.id)
                .limit(1)
            )).mappings().first()
        return Batch.from_row(row) if row else None

    async def pending(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, *,
        blockers: list[dict[str, Any]] | None = None,
    ):
        """Exact pending inputs; report unknown delivery that prevents batching."""
        async with self.db._engine.connect() as conn:
            ids = await _pending_tasks(conn, target.project_id, target.repository_id, limit=None)
        delivered_to_project = await self.delivered(target, snapshot, ids)
        ids = [task_id for task_id in ids if task_id not in delivered_to_project]
        async with self.db._engine.connect() as conn:
            routed = await delivery_targets(conn, ids, reduced=True)
            ids = [task_id for task_id in ids if task_id in routed and
                   (routed[task_id].repository_id, routed[task_id].target_ref) ==
                   (target.repository_id, target.target_ref)]
            if len(ids) > self.limit:
                ids = list((await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(ids))
                           .order_by(tasks.c.updated_at.desc(), tasks.c.id)
                           .limit(self.limit))).scalars().all())
            if not ids:
                return None
            withheld = set((await conn.execute(
                select(integration_batch_members.c.task_id, integration_batch_members.c.source_sha)
                .select_from(integration_batch_members.join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id))
                .where(integration_batches.c.project_id == target.project_id,
                       integration_batches.c.repository_id == target.repository_id,
                       integration_batches.c.target_ref == target.target_ref,
                       integration_batches.c.intent == "aborted",
                       integration_batch_members.c.task_id.in_(ids))
            )).all())
            bases: dict[str, str] = {}
            for task_id, base, _ in (await conn.execute(
                select(task_branch_origins.c.task_id, task_branch_origins.c.base_sha,
                       task_branch_origins.c.creation_generation)
                .where(task_branch_origins.c.task_id.in_(ids),
                       task_branch_origins.c.repository_id == target.repository_id,
                       task_branch_origins.c.retired_at.is_(None))
                .order_by(task_branch_origins.c.creation_generation)
            )).all():
                bases[task_id] = base
            edges: dict[str, set[str]] = {}
            for task_id, needs in (await conn.execute(
                select(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id)
                .where(task_dependencies.c.task_id.in_(ids),
                       task_dependencies.c.dep_type == "blocks")
            )).all():
                edges.setdefault(task_id, set()).add(needs)
        requests = await load_delivery_requests(
            self.db, ids, repository_id=target.repository_id, target_ref=target.target_ref,
            reduced=True,
        )
        members: dict[str, BatchMember] = {}
        delivered: set[str] = set()
        for task_id in ids:
            request, base = requests.get(task_id), bases.get(task_id)
            if request is None or not is_valid_git_oid(base or ""):
                continue
            evidence = await snapshot.is_delivered(request, source_base=base)
            if evidence.state is DeliveryState.UNKNOWN and blockers is not None:
                detail = f"task {task_id} delivery is unknown ({evidence.reason})"
                if evidence.error_detail:
                    detail += f": {evidence.error_detail}"
                blockers.append({
                    "code": evidence.reason, "ref": task_id, "task_id": task_id,
                    "detail": detail,
                    "repository_id": target.repository_id, "target_ref": target.target_ref,
                })
            if evidence.satisfied:
                delivered.add(task_id)
                continue
            source = evidence.source_oid
            if (evidence.state is not DeliveryState.PENDING
                    or evidence.reason != "source_not_delivered"
                    or not is_valid_git_oid(source or "") or (task_id, source) in withheld):
                continue
            members[task_id] = BatchMember(task_id, source, base)
        # A member never lands ahead of undelivered work it depends on that
        # this batch does not carry; it waits for a later batch instead.
        blocked = set(ids) - members.keys() - delivered
        changed = True
        while changed:
            changed = False
            for task_id in list(members):
                if edges.get(task_id, set()) & blocked:
                    blocked.add(members.pop(task_id).task_id)
                    changed = True
        if not members:
            return None
        dependencies = {task_id: edges.get(task_id, set()) & members.keys()
                        for task_id in members}
        return tuple(members.values()), {key: requests[key] for key in members}, dependencies

    async def delivered(self, target, snapshot, ids):
        async with self.db._engine.connect() as conn:
            default = await conn.scalar(select(repos.c.default_branch).where(
                repos.c.id == target.repository_id))
        root_ref = _branch(default)
        return await project_delivered(
            self.db, ids, project_id=target.project_id, repository_id=target.repository_id,
            target_ref=root_ref, snapshot=snapshot.for_target(root_ref),
        )

    async def eligible(self, batch: Batch, members: tuple[BatchMember, ...]) -> bool:
        """Ordinary identity is still current: completed, routed to this target."""
        ids = [member.task_id for member in members]
        async with self.db._engine.connect() as conn:
            mode = (await conn.execute(
                select(projects.c.hierarchical_integration_mode, projects.c.status)
                .where(projects.c.id == batch.project_id)
            )).first()
            routed = await delivery_targets(conn, ids, reduced=True)
            live = set((await conn.execute(select(task_branch_origins.c.task_id).where(
                task_branch_origins.c.task_id.in_(ids),
                task_branch_origins.c.repository_id == batch.repository_id,
                task_branch_origins.c.retired_at.is_(None),
            ))).scalars().all())
        if mode is None or mode[0] not in TRAIN_MODES or mode[1] != "ACTIVE":
            return False
        requests = await load_delivery_requests(
            self.db, ids, repository_id=batch.repository_id, target_ref=batch.target_ref,
            reduced=True,
        )
        for task_id in ids:
            request, target = requests.get(task_id), routed.get(task_id)
            if (task_id not in live or request is None or request.task_status != "COMPLETED" or
                    target is None or
                    (target.repository_id, target.target_ref) !=
                    (batch.repository_id, batch.target_ref)):
                return False
        return True

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        async with self.db.immediate() as conn:
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch.id,
                       integration_batches.c.target_ref.is_not(None),
                       integration_batches.c.lifecycle != "promoted")
                .values(lifecycle="promoted", final_main_sha=observation.target_sha,
                        updated_at=self.clock())
            )


class LeasedPublish:
    """``ManagedPublish`` over the per-ref lease: acquire, fenced push, release.

    A ref leased by anyone else (a repair worker on the candidate ref) refuses
    with ``BranchBusy``, which the batch service settles by reading the remote.
    """

    def __init__(self, db, git, *, holder: str = TRAIN_HOLDER, ttl_seconds: float = 120.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.locks, self.git = BranchLock(db, clock=clock), git
        self.holder, self.ttl_seconds = holder, ttl_seconds

    async def __call__(
        self, repo: RetainedRepository, ref: str, *, expected_old_oid: str, new_oid: str,
        authorize: Callable[[], Awaitable[bool]],
    ) -> str:
        fence = await self.locks.acquire(
            BranchKey(repository_id=repo.repository_id, branch=ref), self.holder,
            ttl_seconds=self.ttl_seconds, role="integration",
        )
        try:
            return await self.locks.fenced_push(
                fence, git=self.git, checkout_path=str(repo.store), repository=repo.binding,
                tip_oid=new_oid, expected_old_oid=expected_old_oid, authorize=authorize,
            )
        finally:
            await self.locks.release(fence)


async def retain_train_candidate(git, store, batch: Batch, candidate_sha: str) -> None:
    """Make the candidate reachable to a clone of the retained store; local only."""
    result = await git.arun_git_result(
        ["update-ref", RETAINED_CANDIDATE_PREFIX + batch.id, candidate_sha], cwd=str(store)
    )
    if result.returncode:
        raise ValueError(f"candidate retention failed: {result.stderr.strip()}")


async def _never_trusted(subject, sha) -> bool:
    return False


class DaemonLanes:
    """Per-target lanes over the daemon's git, retained stores and checks.

    The store is the repository's retained clone. A project with a pinned
    development source validates its root candidates with that source's local
    jobs and rebuilds generated files with its command; everything else reads
    the hosted checks the project requires at the target's boundary.
    """

    def __init__(self, orchestrator, *, batches: DatabaseBatches,
                 clock: Callable[[], float] = time.time) -> None:
        self.orchestrator, self.batches, self.clock = orchestrator, batches, clock
        self.db, self.git = orchestrator.db, orchestrator.git
        self.truth = GitTruth(orchestrator.git)
        self.store = BatchStore(self.db, clock=clock)
        self.publish = LeasedPublish(self.db, self.git, clock=clock)

    async def __call__(self, target: TrainTarget) -> TrainLane:
        from src.integration.development_runtime import development_repository

        policy = await self._policy(target.project_id)
        settings, version = await self._settings(policy)
        repo_row = await self.db.get_repo(target.repository_id)
        binding = await self.orchestrator.github_repository_binding_resolver(repo_row)
        primitives = self.orchestrator.development_integration
        if settings is not None:
            retained = await development_repository(primitives, repo_row, binding, settings)
        else:
            # A non-development project still merges generated artifacts; the
            # lane rebuilds them with the project's own regenerator.
            retained = RetainedRepository(
                repository_id=target.repository_id, store=await primitives.store(repo_row),
                binding=binding, default_branch=repo_row.default_branch,
                regenerate=DEFAULT_REGENERATE_COMMAND,
            )

        async def repository(subject: Subject) -> RetainedRepository:
            if subject.repository_id != target.repository_id:
                raise ValueError("lane serves one repository")
            return retained

        # The batch service only merges and validates; it never runs a subject
        # primitive, so the mandatory subject authority trusts nothing.
        gitops = GitOperations(
            self.db, git=self.git, repository=repository,
            authority=SubjectGitAuthority(self.db, trusted_green=_never_trusted),
        )
        local = settings is not None and target.kind != "epic"

        async def resolve(batch: Batch, candidate_sha: str):
            if local:
                return await self._local(target, retained, settings, version, batch,
                                         candidate_sha)
            return await self._hosted(policy, binding, target, batch, candidate_sha)

        checks = CandidateChecks(
            resolve, advisory=local and settings.validation == "advisory"
        )

        async def attest(batch: Batch, candidate_sha: str) -> str:
            from src.integration.ci_producers import HostedCIProducer, LocalCIProducer
            from src.integration.main_promotion import RootAttestationSubject

            attestation = getattr(self.orchestrator, "integration_attestation_service", None)
            if attestation is None:
                return "unavailable"
            exact = await checks.for_candidate(batch, candidate_sha)
            producer = None if exact is None else exact.provider.producer
            if isinstance(producer, LocalCIProducer) or not isinstance(producer, HostedCIProducer):
                return "unavailable"
            subject = RootAttestationSubject(
                repository_numeric_id=binding.repository_id, repository_full_name=binding.full_name,
                operation_id=batch.id, batch_id=batch.id, revision=batch.repair_attempt_count,
                candidate_sha=candidate_sha, required_check_version=exact.required.version,
            )
            return (await attestation.publish(subject, producer=producer)).outcome

        service = BatchService(self.store, gitops, publish=self.publish,
                               eligible=self.batches.eligible, gate=checks.gate,
                               attest=None if local else attest, require_attestation=not local)

        async def snapshot() -> GitTruthSnapshot:
            return await self.truth.snapshot(
                str(retained.store), project_id=target.project_id,
                repository_id=target.repository_id, repository_url=repo_row.url,
                target_ref=target.target_ref,
            )

        return TrainLane(snapshot=snapshot, service=service, checks=checks)

    async def _policy(self, project_id: str) -> dict:
        async with self.db._engine.connect() as conn:
            policy = (await conn.execute(
                select(projects.c.hierarchical_integration_policy)
                .where(projects.c.id == project_id)
            )).scalar_one_or_none()
        return policy if isinstance(policy, dict) else {}

    async def _settings(self, policy: dict) -> tuple[Any, str | None]:
        from src.integration.development_runtime import (
            load_pinned_development_policy,
            project_development_pin,
        )

        pin = project_development_pin({"hierarchical_integration_policy": policy})
        if pin is None:
            return None, None
        sha = pin.artifact_sha256
        definition = await asyncio.to_thread(self.orchestrator._load_playbook_artifact, sha)
        pinned = await asyncio.to_thread(
            load_pinned_development_policy, self.orchestrator.config, sha, definition
        )
        return pinned.settings, sha

    async def _local(self, target, retained, settings, version, batch, candidate_sha):
        from src.integration.checks import ExactChecks, LocalChecks
        from src.integration.ci_producers import LocalCIProducer, LocalValidationPlan
        from src.jobs.adapters import PublisherJobs

        if settings.validation == "none" or not settings.commands:
            return None
        await retain_train_candidate(self.git, retained.store, batch, candidate_sha)
        plan = LocalValidationPlan(
            version=version, attempt_id=f"{batch.repair_attempt_count}:0",
            commands=tuple(settings.commands),
            queue_seconds=settings.slot_wait_seconds, run_seconds=settings.timeout_seconds,
        )
        producer = LocalCIProducer(
            self.db, PublisherJobs(lambda: self.orchestrator._command_handler),
            store=retained.store, plan=plan, clock=self.clock,
        )
        return ExactChecks(self.db, LocalChecks(producer, project_id=target.project_id))

    async def _hosted(self, policy, binding, target, batch, candidate_sha):
        from src.integration.checks import ExactChecks, HostedChecks
        from src.integration.ci_producers import HostedCIProducer

        boundary = "parent" if target.kind == "epic" else "root"
        required = dict((policy.get(boundary) or {}).get("required_checks") or {})
        state = {
            "project_id": target.project_id,
            "canonical_repository_id": target.repository_id,
            "repository_numeric_id": binding.repository_id,
            "repository_full_name": binding.full_name,
            "batch_id": batch.id,
            "revision": batch.repair_attempt_count,
            "candidate_sha": candidate_sha,
            "operation_id": batch.id,
            "policy_snapshot": {boundary: {"required_checks": required}},
        }
        attestation = getattr(self.orchestrator, "integration_attestation_service", None)
        if attestation is None:
            return None
        trust, client = await attestation._load_trust(state, boundary=boundary)
        return ExactChecks(self.db, HostedChecks(HostedCIProducer(client, trust)))


class TrainCommandDriver:
    """Drive the train through ``integration_train_tick`` as the daemon.

    Every state change goes through CommandHandler, so the integration service
    holds this driver rather than the train itself.
    """

    def __init__(self, train: IntegrationTrain, commands: Callable[[], Any]):
        self.train, self._commands = train, commands

    async def tick(self, now: float | None = None) -> dict[str, Any]:
        from src.commands.principal import ExecutionPrincipal, principal_context

        commands = self._commands()
        if commands is None:
            return {"success": False, "outcome": "unavailable",
                    "error": "no command handler is attached yet"}
        with principal_context(ExecutionPrincipal.service("integration-train")):
            return await commands.execute("integration_train_tick", {"now": now})

    async def stop(self) -> None:
        await self.train.stop()


def train_for(orchestrator, *, clock: Callable[[], float] = time.time) -> IntegrationTrain | None:
    """The train when ``integration.git_first`` is ``active``; otherwise None."""
    config = orchestrator.config.integration
    if getattr(config, "git_first", "shadow") != "active":
        return None
    from src.integration.repair import OrdinaryRepairService

    batches = DatabaseBatches(orchestrator.db, clock=clock)
    return IntegrationTrain(
        targets=DatabaseTargets(orchestrator.db),
        batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches, clock=clock),
        repair=OrdinaryRepairService(orchestrator.db, clock=clock),
        clock=clock,
    )
