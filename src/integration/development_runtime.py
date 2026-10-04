"""Durable Development frontier, shared Git ports and active reconciler visits.

Retained operations remain observation facts. New subjects pin reviewed policy
and own publication through their durable engine and branch fences.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from sqlalchemy import select

from src.database import tables as t
from src.database.queries.blocked_state import OBSOLETE_META_KEY
from src.git.github_contracts import GitHubRepositoryBinding
from src.integration.delivery_branches import delete_branches, remote_heads
from src.integration.development import (
    _carried_sources,
    _manifest_members,
    operation_rows_on,
)
from src.integration.development_adapter import (
    DevelopmentFrontier,
    DevelopmentIntegrationAdapter,
    DevelopmentMember,
)
from src.integration.development_policy import PinnedDevelopmentPolicy
from src.integration.engine import RootEngineOwnership
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.ownership import BranchOwnershipError
from src.integration.subjects import (
    CleanupArgs,
    GateArgs,
    HeadIdentity,
    JournalMode,
    MemberFacts,
    PolicyArtifactPin,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectEngine,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WriterBudget,
    WriterFileArgs,
    WriterLease,
    WriterStatus,
    budget_values,
    subject_key,
    writer_values,
)
from src.jobs.adapters import PublisherJobs

logger = logging.getLogger(__name__)

#: The existing Development integration mode. Train/hierarchy/parent projects
#: keep their own wiring; this module never considers them.
DEVELOPMENT_MODE = "development"

#: Kinds this runtime owns, and the only rows a per-project transfer moves.
DEVELOPMENT_SUBJECT_KINDS = (SubjectKind.ROOT_BATCH.value, SubjectKind.SOURCE.value)

#: A source that ended without delivering is settled for good: a failed task, or
#: a terminal task the existing readers archived once delivery settled it. A
#: merely ``COMPLETED`` task is the ordinary Development source shape — work is
#: completed and still owed delivery.
SETTLED_STATUSES = frozenset({"FAILED", "ARCHIVED"})

#: Only a release condition orders admission. Provenance edges record where
#: work came from and must not hold a member's successors.
RELEASE_EDGES = frozenset({"blocks"})

#: The existing ``development_repair_sources`` contract written by repair
#: filing. It names revisions; only real ancestry proves a carry.
REPAIR_CONTRACT_KEY = "development_repair_sources"

#: The subject-journal key the adapter seals its frozen manifest under.
MANIFEST_KEY = "development:manifest"

#: Local-only retention ref namespace for a subject's built candidate.
CANDIDATE_REF = "refs/heads/aq/development-subject/"


#: The existing writer's own park state. An in-flight row is not yet a park and
#: a landed one is not either.
PARKED_STATES = frozenset({"parked"})


def parked_revisions(rows) -> set[tuple[str, str]]:
    """Exact (task, source sha) pairs the existing journal already parked.

    Read-only migration of the old publisher's durable rows: a parked revision
    stays parked under the new engine and its source branch is untouched.
    """
    parked: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("state") not in PARKED_STATES:
            continue
        for member in _manifest_members(row.get("manifest")):
            source = member.get("source_sha")
            if isinstance(source, str) and source:
                parked.add((member["task_id"], source))
    return parked


#: The existing writer records a landed batch as ``finished``; delivery itself
#: is proven by git, never by that state alone.
LANDED_STATES = frozenset({"finished", "delivered"})


def landed_revisions(rows) -> set[tuple[str, str]]:
    """Exact revisions the existing journal says finished against a target."""
    landed: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("state") not in LANDED_STATES:
            continue
        for member in _manifest_members(row.get("manifest")):
            source = member.get("source_sha")
            if isinstance(source, str) and source:
                landed.add((member["task_id"], source))
    return landed


class DevelopmentFrontierReader:
    """Existing completion, dependency and delivery truth; no live branch tips.

    Every answer is read from rows the old engine already wrote: completed
    tasks with their checkpointed source base, the ``task_dependencies``
    release graph, delivery receipts, retired delivery proofs and the durable
    ``development.operation`` journal. A member counts as ``pushed`` only when
    the recorded head is what its recorded branch actually holds, and a repair
    ``carries`` an exact parked revision only when the existing repair contract
    names it *and* the repair's head really contains it. An unreadable remote is
    not a push: an unproven member holds only its own descendants.
    """

    def __init__(
        self,
        db,
        *,
        git=None,
        ancestry: Callable[[str, str], Awaitable[bool]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.git, self.ancestry, self.clock = db, git, ancestry, clock

    async def _remote(self, repository: dict, ref: str) -> str | None:
        if self.git is None or not ref:
            return None
        try:
            head = await self.git.remote_head(repository, ref)
        except Exception:
            logger.debug("development frontier remote read failed", exc_info=True)
            return None
        return head.sha if head.state == "present" else None

    async def _contains(self, ancestor: str, descendant: str) -> bool:
        if not ancestor or not descendant:
            return False
        probe = self.ancestry
        if probe is None:
            probe = self.git.is_ancestor if self.git is not None else None
        if probe is None:
            return False
        try:
            return bool(await probe(None, ancestor, descendant))
        except Exception:
            logger.debug("development frontier ancestry probe failed", exc_info=True)
            return False

    async def __call__(self, subject: Subject) -> DevelopmentFrontier:
        project_id, repository_id = subject.project_id, subject.repository_id
        async with self.db._engine.connect() as conn:

            async def rows(table, *conditions):
                return tuple(
                    dict(row)
                    for row in (await conn.execute(select(table).where(*conditions))).mappings()
                )

            repositories = await rows(t.repos, t.repos.c.id == repository_id)
            checkpoints = {
                row["task_id"]: row
                for row in await rows(
                    t.task_integration_checkpoints,
                    t.task_integration_checkpoints.c.repository_id == repository_id,
                )
            }
            origins = {
                row["task_id"]: row
                for row in await rows(
                    t.task_branch_origins,
                    t.task_branch_origins.c.repository_id == repository_id,
                    t.task_branch_origins.c.retired_at.is_(None),
                )
            }
            tasks = {
                row["id"]: row
                for row in await rows(
                    t.tasks, t.tasks.c.project_id == project_id, t.tasks.c.id.in_(checkpoints)
                )
            }
            links = await rows(t.task_dependencies, t.task_dependencies.c.task_id.in_(checkpoints))
            metadata = {
                (row["task_id"], row["key"]): json.loads(row["value"])
                for row in await rows(t.task_metadata, t.task_metadata.c.task_id.in_(checkpoints))
            }
            labels = await rows(t.task_labels, t.task_labels.c.task_id.in_(checkpoints))
            receipts = await rows(
                t.task_delivery_receipts,
                t.task_delivery_receipts.c.repository_id == repository_id,
                t.task_delivery_receipts.c.source_task_id.in_(checkpoints),
            )
            retired = await rows(
                t.integration_legacy_deliveries,
                t.integration_legacy_deliveries.c.project_id == project_id,
                t.integration_legacy_deliveries.c.repository_id == repository_id,
            )
            operations = await operation_rows_on(conn, [project_id])
        repository = repositories[0] if repositories else {}
        default_ref = "refs/heads/" + str(repository.get("default_branch", "main"))
        target_tip = await self._remote(repository, default_ref)
        dependencies: dict[str, set[str]] = {}
        for link in links:
            if link.get("dep_type") in RELEASE_EDGES | {None}:
                dependencies.setdefault(link["task_id"], set()).add(link["depends_on_task_id"])
        held = {
            task_id
            for task_id in checkpoints
            if metadata.get((task_id, "manual_pause"))
            or metadata.get((task_id, "object_experiment"))
        }
        held |= {label["task_id"] for label in labels if str(label["label"]).startswith("hold:")}
        satisfied = {row["source_task_id"] for row in receipts if row.get("source_task_id")}
        # Obsolete work releases its dependents without entering publication.
        satisfied |= {
            task_id for task_id in checkpoints if (task_id, OBSOLETE_META_KEY) in metadata
        }
        satisfied |= {row["task_id"] for row in retired if row.get("proof") == "abandoned"}
        parked = parked_revisions(operations)
        # A landed revision counts as delivered only when the target still holds
        # it: the existing state is read, never trusted as a delivery answer.
        landed = landed_revisions(operations)
        contracts = {
            task_id: metadata[(task_id, REPAIR_CONTRACT_KEY)]
            for task_id in checkpoints
            if metadata.get((task_id, REPAIR_CONTRACT_KEY))
        }
        members = []
        for task_id in sorted(checkpoints):
            task, checkpoint = tasks.get(task_id), checkpoints[task_id]
            origin = origins.get(task_id, {})
            base, head = origin.get("base_sha"), checkpoint.get("checkpoint_sha")
            branch = checkpoint.get("branch") or origin.get("branch_name")
            completed = task is not None and task["status"] == "COMPLETED"
            pushed = bool(head) and (await self._remote(repository, branch or "")) == head
            if pushed and (task_id, head) in landed and bool(
                target_tip and await self._contains(head, target_tip)
            ):
                # Existing durable truth: the old publisher landed this exact
                # revision. It is a satisfied prerequisite, not a new member.
                satisfied.add(task_id)
            if task_id in satisfied:
                continue
            members.append(
                DevelopmentMember(
                    MemberFacts(
                        task_id=task_id,
                        head_sha=head,
                        base_sha=base,
                        generation=checkpoint.get("generation") or 0,
                        held=(
                            task_id in held or task is None or task["status"] in SETTLED_STATUSES
                        ),
                    ),
                    completed=completed,
                    pushed=pushed,
                    completed_at=float((task or {}).get("updated_at") or 0),
                    dependencies=frozenset(dependencies.get(task_id, ())),
                    carries=await self._verified_carries(
                        task_id, head or "", contracts, checkpoints
                    ),
                )
            )
        return DevelopmentFrontier(tuple(members), frozenset(satisfied), frozenset(parked))

    async def _verified_carries(self, repair_id, head, contracts, checkpoints):
        """Carried revisions whose source is genuinely an ancestor of *head*."""
        verified = set()
        for pair in self._contract_carries(repair_id, contracts, checkpoints):
            if await self._contains(pair[1], head):
                verified.add(pair)
        return frozenset(verified)

    @staticmethod
    def _contract_carries(repair_id, contracts, checkpoints) -> set[tuple[str, str]]:
        """Exact parked revisions the existing repair contract names.

        A contract alone is a task-name link; the caller additionally proves
        real ancestry. A revision the member no longer holds is dropped here.
        """
        if not repair_id.startswith("development-repair-"):
            return set()
        return {
            (task_id, source)
            for task_id, source in _carried_sources(repair_id, contracts).items()
            if isinstance(source, str)
            and source
            and checkpoints.get(task_id, {}).get("checkpoint_sha") == source
        }


async def retain_candidate(git, store, subject: Subject) -> str | None:
    """Keep the subject's built candidate reachable in its retained store.

    The shared merge primitive pins a built candidate in this store at
    ``refs/aq/subjects/*`` and never pushes it before publication. The existing
    ``job_submit_integration`` path clones the retained store and checks the
    candidate out by SHA, and a clone copies only what its own refs reach, so a
    candidate-only ref would be invisible to local validation. A local retention
    branch makes that exact object reachable for the snapshot.

    It is local only: nothing is pushed, ``HEAD`` is never moved (the old engine
    reads it), and no fence, journal or delivery record is written.
    """
    if not subject.head_sha:
        return None
    ref = CANDIDATE_REF + subject.id
    result = await git.arun_git_result(["update-ref", ref, subject.head_sha], cwd=str(store))
    if result.returncode:
        raise ValueError(f"candidate retention failed: {result.stderr.strip()}")
    return ref


class DevelopmentTrustedGreen:
    """Publication's exact-head green resolver over the pinned local plan.

    Local validation is detached job evidence for one exact head, one
    generation and one pinned plan version. A pending, missing or earlier-head
    receipt is never green, so the default-branch publisher keeps its
    exact-SHA trusted-green requirement and no mode can manufacture success.
    """

    def __init__(self, producer_for: Callable[[Subject], Awaitable[Any]]) -> None:
        self.producer_for = producer_for

    async def __call__(self, subject: Subject, sha: str) -> bool:
        if sha != subject.head_sha:
            return False
        producer = await self.producer_for(subject)
        if producer is None:
            return False
        observation = await producer.observe(
            subject,
            HeadIdentity(
                repository_id=subject.repository_id,
                ref=subject.target_ref,
                sha=sha,
                generation=subject.generation,
                base_sha=subject.base_sha,
            ),
        )
        return (
            observation.head_sha == sha
            and observation.required_check_version == producer.required_check_version
            and observation.state.value == "green"
            and observation.classification == "conclusive"
        )


class DevelopmentPrimitiveAdapters:
    """Development-specific ports over the existing command owner and Git.

    Every primitive here is an existing mechanism: the shared retained-clone
    Git operations (rows 3-7), the existing ``development_repair_sources``
    repair filing, the existing ``gate_create`` gate command and the existing
    backed-up, lease-checked branch deletion. No journal, engine, stall
    detector or recovery path is added here, and no port answers without the
    subject it was asked about still being its exact self.
    """

    def __init__(
        self,
        db,
        *,
        development: Any,
        git_operations: GitOperations,
        commands: Callable[[], Any],
        backup_dir,
        frontier_for=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.development, self.git = db, development, git_operations
        self.commands, self.backup_dir, self.clock = commands, backup_dir, clock
        self.frontier_for = frontier_for

    def bind(self, ports: PrimitivePorts) -> PrimitivePorts:
        self.git.bind(ports)
        ports.bind(Primitive.WRITER_FILE, self.file_writer)
        ports.bind(Primitive.GATE, self.gate)
        ports.bind(Primitive.CLEANUP, self.cleanup)
        return ports

    async def _current(self, subject: Subject) -> bool:
        row = await self.db.get_integration_subject(subject.id)
        return (
            row is not None
            and row["version"] == subject.version
            and row["engine"] == SubjectEngine.RECONCILER.value
            and row["phase"] == subject.phase.value
        )

    # ------------------------------------------------------------------ writer

    async def file_writer(self, subject: Subject, args: WriterFileArgs) -> PrimitiveOutcome:
        """File or reuse the existing bounded Development repair for this batch.

        The manifest is the subject's own frozen sources: the exact parked
        revision for a source subject, or the failing aggregate members for a
        root batch. Filing is replay-safe through the existing repair identity,
        so a repeated visit answers ``exists`` instead of starting a second
        repair or spending another generation.
        """
        if not await self._current(subject):
            return PrimitiveOutcome.unknown(args.primitive, "subject_changed")
        if subject.writer.task_id or subject.budget is not None:
            return PrimitiveOutcome(primitive=args.primitive, outcome="exists")
        manifest = await self._manifest(subject)
        if not manifest:
            return PrimitiveOutcome(
                primitive=args.primitive,
                outcome="configuration_blocked",
                reason="no_repairable_sources",
            )
        identity = self.development._repair_identity(manifest)
        filed = await self.development.resolve_task(identity) is None
        if filed:
            filed = await self.development.ensure_repair(
                subject.project_id,
                subject.repository_id,
                manifest,
                subject.head_sha or manifest[0]["source_sha"],
                reason=args.brief,
            )
        if not filed:
            return PrimitiveOutcome(
                primitive=args.primitive, outcome="exists", detail={"repair_task_id": identity}
            )
        return PrimitiveOutcome(
            primitive=args.primitive,
            outcome="filed",
            detail={"repair_task_id": identity, "sources": [m["task_id"] for m in manifest]},
        )

    async def _owed_manifest(self, subject: Subject) -> list[dict[str, str]]:
        """The frozen revisions this subject still owes a repair for.

        A revision a verified repair already carries is replaced by that
        repair, so a later failing rebuild files the next generation rather than
        replaying a closed repair. The frozen manifest is never rewritten; this
        is the subset the existing frontier still shows as owed.
        """
        manifest = await self._manifest(subject)
        if self.frontier_for is None or subject.kind is not SubjectKind.SOURCE:
            return manifest
        frontier = await self.frontier_for(subject)
        carried = {
            task_id
            for member in frontier.members
            for task_id, source in member.carries
            if (task_id, source) in {(m["task_id"], m["source_sha"]) for m in manifest}
        }
        replacements = {
            member.facts.task_id: member.facts.head_sha
            for member in frontier.members
            if any(task_id in carried for task_id, _ in member.carries)
        }
        owed = [
            entry
            for entry in manifest
            if entry["task_id"] not in replacements
            or replacements[entry["task_id"]] != entry["source_sha"]
        ]
        if len(owed) == len(manifest):
            return owed
        # The repair itself is the work this rebuild still owes.
        return [
            {"task_id": task_id, "source_sha": sha} for task_id, sha in sorted(replacements.items())
        ]

    async def _manifest(self, subject: Subject) -> list[dict[str, str]]:
        """The exact revisions this subject owns, from its frozen manifest.

        A root batch's manifest was sealed once and never replaced; a source
        subject carries exactly its own frozen revision. A live tip is never
        substituted for a frozen one.
        """
        rows = await self.db.list_integration_subject_journal(
            subject.id, limit=100, entry_kinds=("action",)
        )
        sealed = next(
            (row["payload"] for row in rows if row["idempotency_key"] == MANIFEST_KEY), None
        )
        if sealed is not None:
            return [
                {"task_id": member["facts"]["task_id"], "source_sha": member["facts"]["head_sha"]}
                for member in sealed["members"]
            ]
        if subject.kind is SubjectKind.SOURCE:
            return [{"task_id": subject.task_id, "source_sha": subject.head_sha}]
        return []

    # -------------------------------------------------------------------- gate

    async def gate(self, subject: Subject, args: GateArgs) -> PrimitiveOutcome:
        """Create the named human gate through the existing gate command."""
        if not await self._current(subject):
            return PrimitiveOutcome.unknown(args.primitive, "subject_changed")
        result = await self.commands().execute(
            "gate_create",
            {
                "project_id": subject.project_id,
                "gate_type": "decision",
                "title": args.question[:200],
                "question": args.question,
                "waiter_task_ids": [task for task in (subject.task_id,) if task],
            },
        )
        if not result.get("success"):
            return PrimitiveOutcome.unknown(args.primitive, result.get("error", "gate_refused"))
        return PrimitiveOutcome(
            primitive=args.primitive,
            outcome="created",
            detail={"gate_id": result["gate"]["id"]},
        )

    # ----------------------------------------------------------------- cleanup

    async def cleanup(self, subject: Subject, args: CleanupArgs) -> PrimitiveOutcome:
        """Delete only this subject's delivered source branches, safely.

        Delivery truth decides which branches are obsolete: a member branch
        whose exact recorded head is already contained in the published target.
        Deletion goes through the existing backup, deletion-log and
        ``--force-with-lease`` machinery, so a moved branch is left alone and
        every deletion stays restorable. A member that is parked, or whose work
        is not yet on the target, keeps its branch, and the visit answers
        ``pending`` until nothing remains to collect.
        """
        if not await self._current(subject):
            return PrimitiveOutcome.unknown(args.primitive, "subject_changed")
        manifest = await self._manifest(subject)
        if not manifest:
            return PrimitiveOutcome(primitive=args.primitive, outcome="clean")
        repository = await self.git.repository(subject.repository_id)

        async def run_git(store, *args):
            return await self.git.run(repository, *args)

        try:
            async with self.git.exclusion(subject.repository_id, subject):
                store = repository.store
                heads = await remote_heads(run_git, store)
                main_head = heads.get(repository.default_branch)
                if main_head is None:
                    return PrimitiveOutcome.unknown(args.primitive, "target_head_unknown")
                targets, retained = {}, []
                for member in manifest:
                    branch, sha = await self._branch(member["task_id"]), member["source_sha"]
                    delivered = (
                        bool(sha) and await self.git.is_ancestor(repository, sha, main_head)
                    )
                    if not args.delete_successful_sources or not branch or not delivered:
                        # Still parked, or not yet on the target: its branch is
                        # the only copy of that work and it stays.
                        retained.append({"task_id": member["task_id"], "branch": branch})
                        continue
                    if heads.get(branch) != sha:
                        # A branch that moved is not this subject's to delete.
                        retained.append({"task_id": member["task_id"], "branch": branch})
                        continue
                    targets[branch] = {
                        "head": sha,
                        "reason": f"delivered by development subject {subject.id}",
                    }
                outcomes: dict[str, Any] = {}
                if targets:
                    deletion = await delete_branches(
                        self.git.git,
                        run_git,
                        store,
                        targets,
                        default_branch=repository.default_branch,
                        main_head=main_head,
                        backup_dir=self.backup_dir,
                        repository_id=subject.repository_id,
                        now=self.clock(),
                    )
                    outcomes = deletion.get("outcomes", {})
        except (OSError, ValueError, BranchOwnershipError) as exc:
            return PrimitiveOutcome.unknown(args.primitive, str(exc))
        remaining = await remote_heads(run_git, repository.store)
        outstanding = sorted(branch for branch in targets if remaining.get(branch) is not None)
        return PrimitiveOutcome(
            primitive=args.primitive,
            outcome="pending" if outstanding else "clean",
            detail={
                "outcomes": outcomes,
                "outstanding": outstanding,
                "retained": retained,
            },
        )

    async def _branch(self, task_id: str) -> str | None:
        async with self.db._engine.connect() as conn:
            return await conn.scalar(
                select(t.task_integration_checkpoints.c.branch).where(
                    t.task_integration_checkpoints.c.task_id == task_id
                )
            )


async def _transfer_on(
    db,
    project_id: str,
    engine: SubjectEngine,
    expected_versions: dict[str, int],
    reason: str,
    evidence: tuple[str, ...],
    operator_id: str | None,
    dry_run: bool,
    clock: Callable[[], float],
) -> dict:
    """Move the project's subjects under row locks, on the caller's fence."""
    async with db.immediate() as conn:
        rows = (
            (
                await conn.execute(
                    select(t.integration_subjects)
                    .where(
                        t.integration_subjects.c.project_id == project_id,
                        t.integration_subjects.c.kind.in_(DEVELOPMENT_SUBJECT_KINDS),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        versions = {row["id"]: row["version"] for row in rows}
        if dry_run:
            return {
                "outcome": "preview",
                "project_id": project_id,
                "engine": engine.value,
                "subject_ids": sorted(versions),
                "expected_versions": versions,
                "current_engines": {row["id"]: row["engine"] for row in rows},
            }
        if not rows or versions != expected_versions:
            raise ValueError("development subject set/version changed")
        if unresolved := await _unresolved_publications(conn, rows):
            raise ValueError(
                "unresolved development publication prevents engine transfer: "
                + ", ".join(unresolved)
            )
        now = clock()
        for row in rows:
            subject = Subject.from_row({**row, "engine": "reconciler"})
            await db.append_integration_subject_journal_on(
                conn,
                {
                    "subject_id": subject.id,
                    "entry_kind": "action",
                    "mode": "active",
                    "idempotency_key": f"engine:{subject.version}:{engine.value}",
                    "policy_artifact_sha256": subject.policy.artifact_sha256,
                    "subject_version": subject.version,
                    "phase": subject.phase.value,
                    "head_sha": subject.head_sha,
                    "generation": subject.generation,
                    "primitive": Primitive.RECORD_DECISION.value,
                    "outcome": "recorded",
                    "recorded_at": now,
                    "payload": {
                        "from": row["engine"],
                        "to": engine.value,
                        "reason": reason,
                        "evidence": list(evidence),
                        "operator_id": operator_id,
                        "command": "development_engine_transfer",
                    },
                },
            )
            values = {"engine": engine.value}
            if subject.is_live and not subject.schedule.gate_id:
                values.update(next_due_at=now, due_set_at=now, wait_reason=None)
            if (
                await db.update_integration_subject_on(
                    conn,
                    subject_id=subject.id,
                    expected_version=subject.version,
                    values=values,
                    now=now,
                )
                is None
            ):
                raise ValueError("development subject changed during transfer")
    return {
        "outcome": "transferred",
        "project_id": project_id,
        "engine": engine.value,
        "subject_ids": sorted(versions),
    }


#: Outcomes that answer one specific publish intent. ``applied`` is the shared
#: publisher's own confirmation row for its intent key; the rest are the
#: reconciler's answers to a visit.
CONFIRMED_PUBLISH_OUTCOMES = frozenset(
    {"applied", "published", "target_moved", "unknown_after_push"}
)

#: The suffix the shared publisher appends to an intent key when it applies it.
APPLIED_SUFFIX = ":applied"


def publish_intent(entry) -> str | None:
    """The exact intent key one publish journal row speaks about.

    The shared publisher journals its prepare under the intent key itself and
    its confirmation under that key plus :data:`APPLIED_SUFFIX`; a reconciler
    action row names the intent in its result detail. A row that names none is
    not evidence about any intent.
    """
    payload = entry["payload"] or {}
    detail = (payload.get("result") or {}).get("detail") or {}
    intent = detail.get("intent")
    if intent:
        return intent
    key = entry["idempotency_key"] or ""
    if entry["outcome"] == "applied" and key.endswith(APPLIED_SUFFIX):
        return key[: -len(APPLIED_SUFFIX)]
    if entry["outcome"] == "prepared":
        return key
    return None


async def _unresolved_publications(conn, rows) -> list[str]:
    """Subjects whose newest journalled publish intent was never confirmed.

    Correlation is by intent key *and* journal order, never by subject. An
    intent confirmed earlier says nothing about a later prepare, so a
    subject-level "it published once" answer would hide exactly the write a
    crashed publisher left open. Only the newest prepare per subject counts, and
    only a confirmation of that same intent recorded after it resolves it.
    """
    ids = [row["id"] for row in rows]
    if not ids:
        return []
    entries = (
        (
            await conn.execute(
                select(
                    t.integration_subject_journal.c.seq,
                    t.integration_subject_journal.c.subject_id,
                    t.integration_subject_journal.c.idempotency_key,
                    t.integration_subject_journal.c.outcome,
                    t.integration_subject_journal.c.payload,
                )
                .where(
                    t.integration_subject_journal.c.subject_id.in_(ids),
                    t.integration_subject_journal.c.entry_kind == "action",
                    t.integration_subject_journal.c.primitive == Primitive.GIT_PUBLISH.value,
                )
                .order_by(t.integration_subject_journal.c.seq)
            )
        )
        .mappings()
        .all()
    )
    newest: dict[str, tuple[int, str]] = {}
    confirmed: dict[str, list[tuple[int, str]]] = {}
    for entry in entries:
        intent = publish_intent(entry)
        if intent is None:
            continue
        if entry["outcome"] == "prepared":
            previous = newest.get(entry["subject_id"])
            if previous is None or entry["seq"] > previous[0]:
                newest[entry["subject_id"]] = (entry["seq"], intent)
        elif entry["outcome"] in CONFIRMED_PUBLISH_OUTCOMES:
            confirmed.setdefault(entry["subject_id"], []).append((entry["seq"], intent))
    return sorted(
        subject_id
        for subject_id, (seq, intent) in newest.items()
        if not any(
            confirmed_seq > seq and confirmed_intent == intent
            for confirmed_seq, confirmed_intent in confirmed.get(subject_id, ())
        )
    )


async def transfer_development_engine(
    db,
    project_id: str,
    *,
    engine: SubjectEngine | str,
    expected_versions: dict[str, int],
    reason: str,
    evidence: tuple[str, ...] = (),
    operator_id: str | None = None,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Audit one project's Development subjects, or preview the exact change.

    The transfer runs inside the *exclusive* repository engine fence, the same
    lock identity :func:`publisher_exclusion` shares for its whole publication.
    A publisher already outside a transaction holds that shared lock, so an
    audited transfer waits for it instead of racing it: row versions alone
    cannot fence a publisher that is mid-push.

    Activation requires evidence and exact versions. Unconfirmed publication
    refuses activation; disabling reconciliation preserves durable ownership.
    """
    engine = SubjectEngine(engine)
    if not dry_run and (
        not reason.strip()
        or not expected_versions
        or (engine is SubjectEngine.RECONCILER and not evidence)
    ):
        raise ValueError("development transfer needs exact versions, reason and cutover evidence")
    async with AsyncExitStack() as fences:
        for repository_id in sorted(await _project_repository_ids(db, project_id)):
            # The same lock identity a publisher holds: exclusive here, shared
            # there. Sorted order keeps two transfers from deadlocking.
            await fences.enter_async_context(
                RootEngineOwnership(db, clock=clock).exclusion(repository_id)
            )
        return await _transfer_on(
            db,
            project_id,
            engine,
            expected_versions,
            reason,
            evidence,
            operator_id,
            dry_run,
            clock,
        )


async def _project_repository_ids(db, project_id: str) -> set[str]:
    """Every repository this project's Development subjects name."""
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(t.integration_subjects.c.repository_id)
                    .join(
                        t.repos, t.repos.c.id == t.integration_subjects.c.repository_id
                    )
                    .where(
                        t.integration_subjects.c.project_id == project_id,
                        t.integration_subjects.c.kind.in_(DEVELOPMENT_SUBJECT_KINDS),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    return set(rows)


#: Statuses the existing task lifecycle uses for work that has not ended.
OPEN_TASK_STATUSES = frozenset(
    {"DEFINED", "READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT", "PAUSED", "BLOCKED"}
)

#: Terminal statuses. A terminal task with a live session or a locked checkout
#: is still a writer: nothing may take its branch fence from a live process.
TERMINAL_TASK_STATUSES = frozenset({"COMPLETED", "FAILED", "ARCHIVED"})

#: One bounded repair-generation budget, from the pinned table's own budget.
REPAIR_BUDGET_SECONDS = 3600
REPAIR_INTELLIGENCE_CLASS = "standard-high"


class DevelopmentWriterProjection:
    """Mirror the filed repair's existing lifecycle onto its subject's row.

    The adapter observes ``subject.writer`` and ``subject.budget``, so the
    durable writer columns are this projection's job. Every input is the
    existing substrate the old engine already writes: the subject's own journal
    names the repair task the ``writer_file`` primitive filed, and the task,
    session and workspace rows say what that repair has done. Nothing is
    inferred from a live process, and a terminal task with a live session or a
    locked checkout keeps its lease until that is proved otherwise.
    """

    def __init__(self, db, *, clock: Callable[[], float] = time.time) -> None:
        self.db, self.clock = db, clock

    async def filed_repairs(self, subject: Subject) -> list[str]:
        """Every repair task this subject filed, in filing order.

        The reconciler journals each ``writer_file`` outcome, so the subject's
        own append-only journal is the durable record of which repair generation
        is which.
        """
        rows = await self.db.list_integration_subject_journal(
            subject.id, limit=100, entry_kinds=("action",)
        )
        filed = []
        for row in rows:
            if row["primitive"] != Primitive.WRITER_FILE.value:
                continue
            detail = ((row["payload"] or {}).get("result") or {}).get("detail") or {}
            task_id = detail.get("repair_task_id")
            if task_id and task_id not in filed:
                filed.append(task_id)
        return filed

    async def writer_task(self, subject: Subject) -> str | None:
        """The repair task this subject is currently waiting on, or ``None``."""
        filed = await self.filed_repairs(subject)
        return filed[-1] if filed else None

    async def adopted_after_filing(self, subject: Subject, task_id: str) -> bool:
        """True when a merged build after this repair already took its work.

        The reconciler journals every merge it committed. A ``merged`` action
        recorded after the ``writer_file`` action is the subject adopting that
        repair's work, so the repair is no longer the subject's writer; this is
        the same proof the root observer reads, not a liveness guess.
        """
        rows = await self.db.list_integration_subject_journal(
            subject.id, limit=100, entry_kinds=("action",)
        )
        filed_seq = max(
            (
                row["seq"]
                for row in rows
                if row["primitive"] == Primitive.WRITER_FILE.value
                and ((row["payload"] or {}).get("result") or {}).get("detail", {}).get(
                    "repair_task_id"
                )
                == task_id
            ),
            default=None,
        )
        if filed_seq is None:
            return False
        return any(
            row["seq"] > filed_seq
            and row["primitive"] == Primitive.GIT_MERGE_MEMBERS.value
            and row["outcome"] == "merged"
            for row in rows
        )

    async def project(self, subject: Subject) -> bool:
        """Rewrite the subject's writer and budget columns; report a change."""
        task_id = await self.writer_task(subject)
        if task_id is None:
            return False
        async with self.db._engine.connect() as conn:

            async def rows(table, *conditions):
                return tuple(
                    dict(row)
                    for row in (await conn.execute(select(table).where(*conditions))).mappings()
                )

            tasks = await rows(t.tasks, t.tasks.c.id == task_id)
            sessions = await rows(t.sessions, t.sessions.c.task_id == task_id)
            workspaces = await rows(t.workspaces, t.workspaces.c.locked_by_task_id == task_id)
        task = tasks[0] if tasks else None
        status = (task or {}).get("status")
        latest = max(sessions, key=lambda row: row["started_at"] or 0, default=None)
        live = [
            row
            for row in sessions
            if row["state"] in {"starting", "running", "sleeping", "quarantined"}
        ]
        locked = [row for row in workspaces if row["enabled"] and row["locked_at"]]
        claimed_at = max((row["started_at"] or 0) for row in sessions) if sessions else None
        stopped = task is not None and status in TERMINAL_TASK_STATUSES and not live and not locked
        if stopped:
            lease = WriterLease(
                status=WriterStatus.STOPPED,
                task_id=task_id,
                session_id=(latest or {}).get("id"),
                claimed_at=claimed_at,
                stop_proof={
                    "stop_proof": {
                        "kind": "task_terminal",
                        "task_id": task_id,
                        "status": status,
                        "confirmed_at": (latest or {}).get("ended_at")
                        or (latest or {}).get("started_at")
                        or self.clock(),
                    }
                },
            )
        elif live or locked or status in {"IN_PROGRESS", "ASSIGNED"}:
            lease = WriterLease(
                status=WriterStatus.WORKING if claimed_at else WriterStatus.CLAIMED,
                task_id=task_id,
                session_id=(latest or {}).get("id"),
                claimed_at=claimed_at,
            )
        elif status in OPEN_TASK_STATUSES:
            lease = WriterLease(status=WriterStatus.FILED, task_id=task_id)
        else:
            # An unknown or vanished task is not proof of anything: hold the
            # writer and let the table's capacity or gate rule answer.
            lease = WriterLease(status=WriterStatus.UNKNOWN, task_id=task_id)
        ordinal = len(await self.filed_repairs(subject))
        budget = WriterBudget(
            ordinal=ordinal,
            intelligence_class=REPAIR_INTELLIGENCE_CLASS,
            started_at=subject.updated_at,
            deadline_at=subject.updated_at + REPAIR_BUDGET_SECONDS,
        )
        if lease.status is WriterStatus.STOPPED and await self.adopted_after_filing(
            subject, task_id
        ):
            # The subject rebuilt on top of this repair, so its lease is
            # released. The generation budget stays: it is what makes a third
            # failing rebuild reach the table's named human gate.
            lease = WriterLease()
        if subject.writer == lease and subject.budget == budget:
            return False
        async with self.db.immediate() as conn:
            row = await self.db.lock_integration_subject_on(conn, subject.id)
            if row is None:
                return False
            current = Subject.from_row(row)
            if current.writer == lease and current.budget == budget:
                return False
            changed = await self.db.update_integration_subject_on(
                conn,
                subject_id=subject.id,
                expected_version=current.version,
                values={**writer_values(lease), **budget_values(budget)},
                now=self.clock(),
            )
        return changed is not None


class DevelopmentSubjectRuntime:
    """Service-owned active visits over durable Development subjects."""

    def __init__(
        self,
        db,
        adapter: DevelopmentIntegrationAdapter,
        *,
        policy: Callable[[str], Awaitable[PinnedDevelopmentPolicy]] | None = None,
        active: bool = False,
        shadow: bool = False,
        page_size: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not (active or shadow):
            raise ValueError("a development runtime needs at least one loop")
        self.db, self.adapter, self.policy, self.clock = db, adapter, policy, clock
        self.cursor, self.page_size = "", page_size
        self.loops = [
            adapter.reconciler(mode=mode)
            for enabled, mode in ((active, JournalMode.ACTIVE),)
            if enabled
        ]

    def subscribe(self, bus) -> None:
        for loop in self.loops:
            loop.subscribe(bus)

    async def stop(self) -> None:
        for loop in self.loops:
            await loop.stop()

    async def tick(self, now: float, *, background: bool = False) -> None:
        await self.seed(now)
        await self.project_writers()
        gate_ids = await self._resolved_gates()
        if gate_ids:
            await self.db.wake_integration_subjects(now=now, gate_ids=gate_ids)
        for loop in self.loops:
            await loop.tick(now, background=background)

    async def project_writers(self) -> None:
        """Mirror each live subject's filed repair before its visit reads it."""
        projection = DevelopmentWriterProjection(self.db, clock=self.clock)
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(t.integration_subjects.c.id)
                        .where(
                            t.integration_subjects.c.kind.in_(DEVELOPMENT_SUBJECT_KINDS),
                            t.integration_subjects.c.phase != SubjectPhase.DONE.value,
                        )
                        .order_by(t.integration_subjects.c.id)
                        .limit(self.page_size)
                    )
                )
                .scalars()
                .all()
            )
        for subject_id in rows:
            row = await self.db.get_integration_subject(subject_id)
            if row is None:
                continue
            subject = Subject.from_row(row)
            try:
                await projection.project(subject)
            except (ValueError, LookupError):
                logger.debug("development writer projection skipped %s", subject_id)

    async def _resolved_gates(self) -> list[str]:
        async with self.db._engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        select(t.gates.c.id)
                        .join(
                            t.integration_subjects,
                            t.integration_subjects.c.gate_id == t.gates.c.id,
                        )
                        .where(
                            t.integration_subjects.c.kind.in_(DEVELOPMENT_SUBJECT_KINDS),
                            t.integration_subjects.c.phase != SubjectPhase.DONE.value,
                            t.integration_subjects.c.next_due_at.is_(None),
                            t.gates.c.status == "resolved",
                        )
                        .order_by(t.gates.c.id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )

    async def seed(self, now: float) -> None:
        """Bounded creation from current project settings; never an activation.

        A project is seeded only once a reviewed, imported ``development``
        artifact pins its scope. Each new subject starts under the reconciler
        and retains that exact artifact throughout its lifecycle.
        """
        async with self.db._engine.connect() as conn:
            projects = (
                (
                    await conn.execute(
                        select(t.projects)
                        .where(
                            t.projects.c.id > self.cursor,
                            t.projects.c.hierarchical_integration_mode == DEVELOPMENT_MODE,
                            t.projects.c.integration_repository_id.is_not(None),
                        )
                        .order_by(t.projects.c.id)
                        .limit(self.page_size)
                    )
                )
                .mappings()
                .all()
            )
        self.cursor = projects[-1]["id"] if len(projects) == self.page_size else ""
        for project in projects:
            try:
                await self._seed_project(project, now)
            except (ValueError, FileNotFoundError, KeyError, BranchOwnershipError):
                logger.debug("development subject unavailable for %s", project["id"])

    async def _seed_project(self, project, now: float) -> None:
        pin = project_development_pin(project)
        repository_id = project["integration_repository_id"]
        if pin is None or self.policy is None:
            return
        pinned = await self.policy(pin.artifact_sha256)
        if pinned.definition.scope.project_id != project["id"]:
            return
        repository = await self.db.get_repo(repository_id)
        if repository is None:
            return
        key = subject_key(SubjectKind.ROOT_BATCH, repository_id, pin.artifact_sha256)
        async with self.db._engine.connect() as conn:
            prior = (
                await conn.execute(
                    select(t.integration_subjects.c.id).where(
                        t.integration_subjects.c.project_id == project["id"],
                        t.integration_subjects.c.subject_key == key,
                    )
                )
            ).first()
        if prior is not None:
            return
        subject = Subject(
            id="development-subject-"
            + key.removeprefix(f"{SubjectKind.ROOT_BATCH.value}:").replace(":", "-"),
            project_id=project["id"],
            repository_id=repository_id,
            kind=SubjectKind.ROOT_BATCH,
            subject_key=key,
            engine=SubjectEngine.RECONCILER,
            policy=pin,
            phase=SubjectPhase.ADMITTING,
            target_ref="refs/heads/" + repository.default_branch.removeprefix("refs/heads/"),
            schedule=SubjectSchedule.progress(now=now, max_wait_seconds=3600),
            created_at=now,
            updated_at=now,
        )
        async with self.db.immediate() as conn:
            await self.db.ensure_integration_subject_on(conn, subject.to_row())


def project_development_pin(project) -> PolicyArtifactPin | None:
    """The project's reviewed, imported Development artifact pin.

    Project settings cannot change an in-flight subject: the pin is what makes
    a subject load its own reviewed source. A project without one is never
    seeded.
    """
    policy = project.get("hierarchical_integration_policy") or {}
    boundary = policy.get(DEVELOPMENT_MODE) if isinstance(policy, dict) else None
    artifact = (boundary or {}).get("route", {}).get("artifact") if boundary else None
    if not artifact or not artifact.get("artifact_sha256"):
        return None
    return PolicyArtifactPin(
        playbook_id=artifact["playbook_id"], artifact_sha256=artifact["artifact_sha256"]
    )


async def development_repository(
    primitives, repo, binding: GitHubRepositoryBinding, settings
) -> RetainedRepository:
    """The existing retained clone, freshly fetched, bound to the pinned rebuild.

    A worker checkout is never a delivery source: the store is the repository's
    own retained clone through the existing delivery path (created on first use
    and fetched with every head), and the regenerate command and timeout are the
    ones the reviewed source froze.
    """
    return RetainedRepository(
        repository_id=repo.id,
        store=await primitives.store(repo),
        binding=binding,
        default_branch=repo.default_branch,
        regenerate=settings.regenerate if settings.validation != "none" else None,
        regenerate_timeout_seconds=settings.regenerate_timeout_seconds,
    )


def development_runtime_for(orchestrator):
    """Construct the Development adapter over the daemon's existing owners.

    Returns ``None`` when active visits are disabled. Pausing visits preserves
    durable reconciler ownership and never starts another publisher.
    """
    config = orchestrator.config.integration
    if not config.reconciler_active:
        return None
    from src.integration.observe import (
        DatabaseObservationReader,
        GitObservationReader,
        IntegrationObserver,
    )
    from src.playbooks.integration_policy import IntegrationPolicyFacts

    primitives = orchestrator.development_integration
    app = getattr(orchestrator, "integration_app_client", None)
    repository_binding = getattr(app, "repository", None)
    pinned_settings: dict[str, Any] = {}

    async def policy_for(artifact_sha256: str) -> PinnedDevelopmentPolicy:
        definition = await asyncio.to_thread(orchestrator._load_playbook_artifact, artifact_sha256)
        source = getattr(definition, "source", None)
        if source is None:
            raise ValueError("development artifact carries no reviewed source")
        return PinnedDevelopmentPolicy(definition, source)

    async def retained_repository(repository_id: str) -> RetainedRepository:
        repo = await orchestrator.db.get_repo(repository_id)
        binding = GitHubRepositoryBinding(
            repository_id=repository_binding.repository_id,
            full_name=repository_binding.full_name,
        )
        return await development_repository(
            primitives, repo, binding, pinned_settings.pop(repository_id)
        )

    async def repository_for(subject: Subject) -> RetainedRepository:
        # The rebuild command and timeout come from the subject's own pinned
        # reviewed source, never from current project settings.
        pinned_settings[subject.repository_id] = (
            await policy_for(subject.policy.artifact_sha256)
        ).settings
        repository = await retained_repository(subject.repository_id)
        await retain_candidate(orchestrator.git, repository.store, subject)
        return repository

    adapter = DevelopmentIntegrationAdapter(
        orchestrator.db,
        observe=IntegrationObserver(
            DatabaseObservationReader(orchestrator.db),
            GitObservationReader(orchestrator.git),
            facts_type=IntegrationPolicyFacts,
            session_probe=orchestrator._root_subject_session_probe,
        ).observe,
        frontier_for=DevelopmentFrontierReader(orchestrator.db),
        policy_for=policy_for,
        repository_for=repository_for,
        shared_ports=PrimitivePorts(),
        job_client=PublisherJobs(lambda: orchestrator._command_handler),
    )
    git_operations = GitOperations(
        orchestrator.db,
        git=orchestrator.git,
        repository=retained_repository,
        authority=SubjectGitAuthority(
            orchestrator.db, trusted_green=DevelopmentTrustedGreen(adapter.producer_for)
        ),
    )
    DevelopmentPrimitiveAdapters(
        orchestrator.db,
        development=primitives,
        git_operations=git_operations,
        commands=lambda: orchestrator._command_handler,
        backup_dir=primitives.backup_dir,
        frontier_for=adapter.frontier_for,
    ).bind(adapter.shared)
    return DevelopmentSubjectRuntime(
        orchestrator.db,
        adapter,
        policy=policy_for,
        active=config.reconciler_active,
        shadow=False,
    )


__all__ = [
    "CANDIDATE_REF",
    "DEVELOPMENT_MODE",
    "DEVELOPMENT_SUBJECT_KINDS",
    "DevelopmentFrontierReader",
    "DevelopmentPrimitiveAdapters",
    "DevelopmentSubjectRuntime",
    "DevelopmentTrustedGreen",
    "DevelopmentWriterProjection",
    "development_repository",
    "development_runtime_for",
    "landed_revisions",
    "parked_revisions",
    "project_development_pin",
    "publish_intent",
    "retain_candidate",
    "transfer_development_engine",
]
