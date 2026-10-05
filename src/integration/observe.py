"""Read-only facts for integration subjects (review revision 2, primitive 1).

The reader takes a consistent snapshot of existing rows; Git probes happen
after that transaction closes. Neither port can file work, record CI, recover
owners, fetch objects or change a ref. Unknown evidence remains explicit.
This module adds no command surface and does not activate the reconciler.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import or_, select, text

from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.task_queries import INTEGRATION_REWORK_AT_KEY
from src.database.tables import (
    archived_tasks,
    integration_child_dispositions,
    integration_episode_receipt_acceptances,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.git.manager import GitError, GitManager, RemoteRefState, _validate_ref
from src.integration.models import BranchKey, Fence, HierarchicalIntegrationPolicy
from src.integration.subjects import (
    CIEvidence,
    CIState,
    ConflictFacts,
    GateFacts,
    HeadIdentity,
    HoldFacts,
    MemberFacts,
    ObserveSubjectArgs,
    Primitive,
    PrimitiveOutcome,
    RemoteHead,
    Subject,
    SubjectFacts,
    SubjectKind,
    UnresolvedWrite,
    WriterBudget,
    WriterLease,
    WriterStatus,
)

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
Row = Mapping[str, Any]
logger = logging.getLogger(__name__)

#: A root frontier can name hundreds of source heads; one ls-remote per head
#: costs a second or more each, which outlived the visit budget. Heads are
#: read in batched round trips, a few at a time.
_REMOTE_REF_CHUNK = 200
_REMOTE_READS = 4
#: Ports without a batched read, and local ancestry probes, run concurrently.
_SINGLE_REMOTE_READS = 8
_ANCESTRY_READS = 8
#: Ancestry between two commits never changes; definitive answers are reused
#: across visits, bounded so a long-lived observer stays small.
_ANCESTRY_CACHE = 4096
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class ObservationRows:
    """Snapshot seam for deterministic fixtures and future observation adapters.

    Keys in ``rows`` are existing SQL table names, with unmodified row values.
    Missing tables mean no rows, not an unavailable database. An unavailable
    snapshot raises; the primitive then returns ``unknown(snapshot_unavailable)``.
    """

    subject: Subject
    project: Row
    repository: Row
    rows: Mapping[str, tuple[Row, ...]] = field(default_factory=dict)

    def all(self, table: str) -> tuple[Row, ...]:
        return self.rows.get(table, ())


class SubjectReadPort(Protocol):
    async def read(self, subject_id: str) -> ObservationRows | None: ...


class GitReadPort(Protocol):
    """An optional ``remote_heads(repository, refs) -> {ref: RemoteHead}``
    answers a visit's heads in one call; without it each head is read alone."""

    async def remote_head(self, repository: Row, ref: str) -> RemoteHead: ...

    async def is_ancestor(self, repository: Row, ancestor: str, descendant: str) -> bool | None: ...


SessionProbe = Callable[[Row], Awaitable[bool | None]]


class GitObservationReader:
    """GitManager's strict async reads only; missing objects are unknown.

    A caller may supply its authorized checkout resolver. The default uses the
    repository's base checkout. Full refs are converted to branch names for
    GitManager's ``als_remote_ref`` branch API.
    """

    def __init__(
        self, git: GitManager, *, checkout: Callable[[Row], str | None] | None = None
    ) -> None:
        self.git = git
        self.checkout = checkout or (lambda repository: repository.get("checkout_base_path"))

    async def remote_head(self, repository: Row, ref: str) -> RemoteHead:
        path = self.checkout(repository)
        if not path or not ref.startswith("refs/heads/"):
            return RemoteHead(ref=ref, state="unknown")
        result = await self.git.als_remote_ref(
            path, ref.removeprefix("refs/heads/"), repository_url=repository["url"]
        )
        return _remote_head(ref, result)

    async def remote_heads(self, repository: Row, refs: Sequence[str]) -> dict[str, RemoteHead]:
        """Read several heads in batched round trips; an unreadable one stays unknown.

        A ref GitManager would refuse, or a failed round trip, leaves only
        its own heads unknown rather than the whole visit's.
        """
        heads = {ref: RemoteHead(ref=ref, state="unknown") for ref in refs}
        path = self.checkout(repository)
        branches: dict[str, str] = {}
        for ref in refs:
            if not path or not ref.startswith("refs/heads/"):
                continue
            try:
                branches[_validate_ref(ref.removeprefix("refs/heads/"))] = ref
            except GitError:
                logger.debug("Subject remote head %s is not a readable branch", ref)
        names = list(branches)
        gate = asyncio.Semaphore(_REMOTE_READS)

        async def read(chunk: list[str]) -> None:
            async with gate:
                try:
                    results = await self.git.als_remote_refs(
                        path, chunk, repository_url=repository["url"]
                    )
                except Exception:
                    logger.debug("Subject remote heads unavailable", exc_info=True)
                    return
            for branch in chunk:
                if branch in results:
                    heads[branches[branch]] = _remote_head(branches[branch], results[branch])

        await asyncio.gather(
            *(
                read(names[start : start + _REMOTE_REF_CHUNK])
                for start in range(0, len(names), _REMOTE_REF_CHUNK)
            )
        )
        return heads

    async def is_ancestor(self, repository: Row, ancestor: str, descendant: str) -> bool | None:
        path = self.checkout(repository)
        if path is None:
            return None
        return await self.git.ais_ancestor(path, ancestor, descendant, strict=True)

class DatabaseObservationReader:
    """Project/repository scoped SELECTs over the existing integration tables.

    PostgreSQL enforces a read-only repeatable-read transaction. No row locks
    are taken, and a held subject is observed even when it has no due time.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    async def read(self, subject_id: str) -> ObservationRows | None:
        async with self.db._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))

                async def read(table, *conditions) -> tuple[Row, ...]:
                    result = await conn.execute(select(table).where(*conditions))
                    return tuple(dict(row) for row in result.mappings())

                subjects = await read(
                    t.integration_subjects, t.integration_subjects.c.id == subject_id
                )
                if not subjects:
                    return None
                subject = Subject.from_row(subjects[0])
                project = await read(t.projects, t.projects.c.id == subject.project_id)
                repository = await read(t.repos, t.repos.c.id == subject.repository_id)
                if not project or not repository:
                    raise ValueError("subject project or repository is missing")
                rows: dict[str, tuple[Row, ...]] = {}

                async def keep(table, *conditions):
                    rows[table.name] = await read(table, *conditions)
                    return rows[table.name]

                if subject.batch_id:
                    for table in (
                        t.integration_batches,
                        t.integration_batch_members,
                        t.integration_candidate_revisions,
                        t.integration_candidate_member_results,
                        t.integration_candidate_ref_mutations,
                        t.integration_candidate_resolutions,
                    ):
                        column = table.c.id if table is t.integration_batches else table.c.batch_id
                        await keep(table, column == subject.batch_id)
                # A root's admitting frontier is facts, not an admission decision.
                task_rows = await keep(t.tasks, t.tasks.c.project_id == subject.project_id)
                task_ids = sorted(
                    {row["id"] for row in task_rows}
                    | {row["task_id"] for row in rows.get("integration_batch_members", ())}
                )
                await keep(t.task_metadata, t.task_metadata.c.task_id.in_(task_ids))
                await keep(
                    t.task_integration_checkpoints,
                    t.task_integration_checkpoints.c.task_id.in_(task_ids),
                    t.task_integration_checkpoints.c.repository_id == subject.repository_id,
                )
                await keep(
                    t.task_branch_origins,
                    t.task_branch_origins.c.task_id.in_(task_ids),
                    t.task_branch_origins.c.repository_id == subject.repository_id,
                )
                await keep(
                    t.integration_review_evidence,
                    t.integration_review_evidence.c.source_task_id.in_(task_ids),
                    t.integration_review_evidence.c.repository_id == subject.repository_id,
                )
                await keep(
                    t.integration_source_ci,
                    t.integration_source_ci.c.task_id.in_(task_ids),
                    t.integration_source_ci.c.repository_id == subject.repository_id,
                )
                if subject.kind is SubjectKind.PARENT_EPISODE:
                    await keep(
                        t.integration_parent_episodes,
                        t.integration_parent_episodes.c.parent_task_id == subject.task_id,
                        t.integration_parent_episodes.c.repository_id == subject.repository_id,
                    )
                    await keep(
                        t.integration_child_dispositions,
                        t.integration_child_dispositions.c.parent_task_id == subject.task_id,
                    )
                operations = (
                    await keep(
                        t.integration_repair_operations,
                        t.integration_repair_operations.c.batch_id == subject.batch_id
                        if subject.batch_id
                        else t.integration_repair_operations.c.parent_task_id == subject.task_id,
                    )
                    if subject.batch_id or subject.kind is SubjectKind.PARENT_EPISODE
                    else ()
                )
                operation_ids = [row["id"] for row in operations]
                stages = await keep(
                    t.integration_repair_stages,
                    t.integration_repair_stages.c.operation_id.in_(operation_ids),
                )
                await keep(
                    t.integration_check_evidence,
                    or_(
                        t.integration_check_evidence.c.batch_id == subject.batch_id
                        if subject.batch_id
                        else False,
                        t.integration_check_evidence.c.parent_task_id.in_(task_ids),
                    ),
                )
                writer_ids = {
                    stage["repair_task_id"] for stage in stages if stage["repair_task_id"]
                }
                if subject.writer.task_id:
                    writer_ids.add(subject.writer.task_id)
                writer_ids.update(
                    row["verifier_task_id"]
                    for row in operations
                    if row.get("verifier_task_id")
                )
                writer_ids.update(
                    row["repair_task_id"]
                    for row in rows["integration_source_ci"]
                    if row.get("repair_task_id") and row["task_id"] == subject.task_id
                )
                await keep(
                    t.sessions,
                    t.sessions.c.project_id == subject.project_id,
                    t.sessions.c.task_id.in_(writer_ids),
                )
                await keep(
                    t.workspaces,
                    t.workspaces.c.project_id == subject.project_id,
                    t.workspaces.c.locked_by_task_id.in_(writer_ids),
                )
                await keep(
                    t.integration_branch_owners,
                    t.integration_branch_owners.c.repository_id == subject.repository_id,
                )
                await keep(
                    t.integration_owner_recoveries,
                    t.integration_owner_recoveries.c.repository_id == subject.repository_id,
                    t.integration_owner_recoveries.c.task_id.in_(writer_ids),
                )
                await keep(
                    t.integration_promotion_intents,
                    t.integration_promotion_intents.c.repository_id == subject.repository_id,
                    or_(
                        t.integration_promotion_intents.c.root_batch_id == subject.batch_id
                        if subject.batch_id
                        else False,
                        t.integration_promotion_intents.c.source_task_id == subject.task_id
                        if subject.task_id
                        else False,
                        t.integration_promotion_intents.c.target_task_id == subject.task_id
                        if subject.task_id
                        else False,
                    ),
                )
                await keep(
                    t.integration_subject_journal,
                    t.integration_subject_journal.c.subject_id == subject.id,
                )
                await keep(
                    t.gates,
                    t.gates.c.project_id == subject.project_id,
                    t.gates.c.id == subject.schedule.gate_id,
                )
                return await self.augment_on(
                    conn, ObservationRows(subject, project[0], repository[0], rows)
                )

    async def augment_on(self, conn, snapshot: ObservationRows) -> ObservationRows:
        """Extend a snapshot inside the same read-only repeatable-read transaction."""
        return snapshot


def _remote_head(ref: str, result) -> RemoteHead:
    if result.state is RemoteRefState.PRESENT:
        return RemoteHead(ref=ref, state="present", sha=result.oid)
    return RemoteHead(
        ref=ref, state="absent" if result.state is RemoteRefState.ABSENT else "unknown"
    )


def _latest(rows, time_key="created_at") -> Row | None:
    return max(rows, key=lambda row: (row.get(time_key) or 0, str(row.get("id", ""))), default=None)


def _ref(value: str) -> str:
    return value if value.startswith("refs/") else "refs/heads/" + value


def _operation(snapshot: ObservationRows) -> Row | None:
    episodes = {
        row["id"]
        for row in snapshot.all("integration_parent_episodes")
        if (
            row["id"] == snapshot.subject.parent_episode_id
            if snapshot.subject.parent_episode_id
            else row["generation"] == snapshot.subject.generation
        )
        and row["parent_task_id"] == snapshot.subject.task_id
    }
    return _latest(
        row
        for row in snapshot.all("integration_repair_operations")
        if snapshot.subject.kind is not SubjectKind.PARENT_EPISODE
        or row.get("episode_id") in episodes
    )


def _task_holds(snapshot: ObservationRows, task_ids: set[str]) -> list[HoldFacts]:
    holds = []
    if snapshot.project.get("status") != "ACTIVE":
        holds.append(HoldFacts(kind="project_inactive", reason=str(snapshot.project.get("status"))))
    for row in snapshot.all("task_metadata"):
        if row["task_id"] in task_ids and row["key"] == "manual_pause":
            holds.append(
                HoldFacts(kind="manual_pause", task_id=row["task_id"], reason="manual_pause")
            )
    for row in snapshot.all("integration_batches"):
        if row.get("human_abort_reason"):
            holds.append(HoldFacts(kind="operator_hold", reason=row["human_abort_reason"]))
    return holds


def _members(snapshot: ObservationRows) -> tuple[list[MemberFacts], dict[str, str]]:
    subject = snapshot.subject
    checkpoints = {row["task_id"]: row for row in snapshot.all("task_integration_checkpoints")}
    origins = {
        row["task_id"]: row
        for row in snapshot.all("task_branch_origins")
        if row.get("retired_at") is None
    }
    reviews = snapshot.all("integration_review_evidence")
    tasks = {row["id"]: row for row in snapshot.all("tasks")}
    batch_members = snapshot.all("integration_batch_members")
    revisions = snapshot.all("integration_candidate_revisions")
    revision = next((row for row in revisions if row["revision"] == subject.generation), None)
    manifest = revision.get("source_manifest") if revision else None
    active_ids = {row["task_id"] for row in manifest} if isinstance(manifest, list) else None
    if subject.batch_id:
        identities = [
            (row["task_id"], row) for row in sorted(batch_members, key=lambda r: r["ordinal"])
        ]
    elif subject.kind is SubjectKind.SOURCE:
        identities = [(subject.task_id, {})]
    else:
        identities = [
            (task_id, {})
            for task_id, task in sorted(tasks.items())
            if task.get("parent_task_id") == subject.task_id
            and (task_id in checkpoints or subject.kind is SubjectKind.PARENT_EPISODE)
        ]
    held_ids = {hold.task_id for hold in _task_holds(snapshot, {key for key, _ in identities})}
    members, refs = [], {}
    for task_id, member in identities:
        checkpoint, origin = checkpoints.get(task_id, {}), origins.get(task_id, {})
        sha = member.get("reviewed_head_sha") or checkpoint.get("checkpoint_sha")
        sealed_review = member.get("review_evidence") or {}
        generation = sealed_review.get("generation", checkpoint.get("generation", 0))
        if member.get("review_evidence_id"):
            sealed = next(
                (row for row in reviews if row["id"] == member["review_evidence_id"]), None
            )
            if sealed:
                generation = sealed["generation"]
        if subject.kind is SubjectKind.SOURCE:
            sha, generation = subject.head_sha or sha, subject.generation
        base = member.get("source_base_sha") or origin.get("base_sha")
        if subject.kind is SubjectKind.SOURCE:
            base = subject.base_sha or base
        review = _latest(
            row
            for row in reviews
            if row["source_task_id"] == task_id
            and row.get("reviewed_head_sha") == sha
            and row.get("source_base") == base
            and row.get("generation") == generation
        )
        verdict = review.get("verdict") if review else "none"
        state = {
            "approved": "approved",
            "rejected": "rejected",
            "pending": "pending",
        }.get(verdict, "none")
        dispositions = [
            row
            for row in snapshot.all("integration_child_dispositions")
            if row["child_task_id"] == task_id
            and any(
                episode["id"] == row["parent_episode_id"]
                and (
                    episode["id"] == subject.parent_episode_id
                    if subject.parent_episode_id
                    else episode["generation"] == subject.generation
                )
                for episode in snapshot.all("integration_parent_episodes")
            )
        ]
        ejected = (active_ids is not None and task_id not in active_ids) or any(
            row.get("disposition") in {"skipped", "ineligible"} for row in dispositions
        )
        members.append(
            MemberFacts(
                task_id=task_id,
                head_sha=sha,
                base_sha=base,
                generation=generation,
                review=state,
                held=task_id in held_ids,
                ejected=ejected,
            )
        )
        ref = member.get("source_ref") or checkpoint.get("branch") or origin.get("branch_name")
        if ref:
            refs[task_id] = _ref(ref)
    return members, refs


def _required(snapshot: ObservationRows) -> Row:
    boundary = "parent" if snapshot.subject.kind is SubjectKind.PARENT_EPISODE else "root"
    batches = snapshot.all("integration_batches")
    operation = _operation(snapshot)
    policy = (batches[0].get("policy_snapshot") if batches else None) or (
        operation.get("policy_snapshot") if operation else None
    )
    # Legacy sources pin their CI policy generation in integration_source_ci.
    if policy is None:
        policy = snapshot.project.get("hierarchical_integration_policy", {})
    return (policy or {}).get(boundary, {}).get("required_checks", {})


def _ci(
    snapshot: ObservationRows,
    identity: HeadIdentity,
    task_id: str | None,
    now: float,
    *,
    candidate: bool = False,
) -> CIEvidence:
    required = _required(snapshot)
    matching = []
    for row in snapshot.all("integration_check_evidence"):
        if candidate:
            exact = row.get("batch_id") == snapshot.subject.batch_id and (
                row.get("candidate_revision") == identity.generation
            )
        else:
            exact = (
                row.get("parent_task_id") == task_id
                and row.get("parent_generation") == identity.generation
                and row.get("parent_head_sha") == identity.sha
            )
        if exact:
            if (
                snapshot.subject.parent_episode_id
                and task_id == snapshot.subject.task_id
                and (operation := _operation(snapshot)) is not None
                and row.get("operation_id") != operation["id"]
            ):
                continue
            matching.append(row)
    row = _latest(matching, "observed_at")
    if candidate:
        revision = next(
            (
                item
                for item in snapshot.all("integration_candidate_revisions")
                if item["revision"] == identity.generation and item["head_sha"] == identity.sha
            ),
            None,
        )
        # A successful aggregate is bound by the candidate row, written by the
        # authenticated CI adapter with a CAS on the candidate's exact SHA.
        green_id = revision.get("ci_evidence_id") if revision else None
        aggregate = next((item for item in matching if item["id"] == green_id), None)
        if aggregate and (row is None or aggregate["observed_at"] >= row["observed_at"]):
            row = aggregate
    source = next(
        (
            row
            for row in snapshot.all("integration_source_ci")
            if row["task_id"] == task_id
            and row["repository_id"] == identity.repository_id
            and row["source_head"] == identity.sha
            and row["generation"] == identity.generation
            and row["source_base"] == identity.base_sha
        ),
        None,
    )
    if source is not None and (row is None or source["observed_at"] > row["observed_at"]):
        evidence = source.get("evidence") or {}
        checks = {
            check["name"]: check.get("conclusion") or "missing"
            for check in evidence.get("checks", ())
        }
        trusted = (
            source.get("policy_generation")
            == snapshot.project.get("hierarchical_integration_generation")
            and evidence.get("head_sha") == identity.sha
            and evidence.get("producer_id") == required.get("producer_id")
            and evidence.get("required_checks_version") == required.get("version")
            and set(evidence.get("required_checks", ())) == set(required.get("names", ()))
        )
        conclusion = {"green": "success", "red": "failure", "cancelled": "cancelled"}.get(
            source["state"], "pending"
        )
        observed, evidence_id, producer = source["observed_at"], None, evidence.get("producer_id")
    elif row is not None:
        checks, conclusion = row["checks"], row["conclusion"]
        trusted = (
            row["producer_id"] == required.get("producer_id")
            and row["required_check_version"] == required.get("version")
            and row.get("classification") in {"conclusive", "full_suite_fallback"}
        )
        observed, evidence_id, producer = row["observed_at"], row["id"], row["producer_id"]
    else:
        requests = [
            item
            for item in snapshot.all("integration_subject_journal")
            if item["primitive"] == Primitive.CI_REQUEST
            and item["mode"] == "active"
            and item["head_sha"] == identity.sha
            and item["generation"] == identity.generation
            and item.get("outcome") in {"requested", "already_running"}
        ]
        request = _latest(requests, "recorded_at")
        requested = request["recorded_at"] if request else None
        return CIEvidence(
            head_sha=identity.sha,
            state=CIState.PENDING if request else CIState.NONE,
            requested_at=requested,
            age_seconds=max(0, now - requested) if requested is not None else None,
        )
    names = set(required.get("names", ()))
    if not trusted or not names:
        state = CIState.UNTRUSTED
    elif conclusion == "pending":
        state = CIState.PENDING
    elif conclusion in {"cancelled", "inconclusive"}:
        state = CIState.INFRA
    elif conclusion == "failure" and any(checks.get(name) == "failure" for name in names):
        state = CIState.RED
    elif (
        conclusion == "success"
        and all(checks.get(name) == "success" for name in names)
        and (not candidate or row is not None and row["id"] == green_id)
    ):
        state = CIState.GREEN
    else:
        state = CIState.UNTRUSTED
    return CIEvidence(
        head_sha=identity.sha,
        state=state,
        evidence_id=evidence_id,
        producer=producer,
        observed_at=observed,
        age_seconds=max(0, now - observed),
    )


def _writer(
    snapshot: ObservationRows,
    unknown: list[str],
    writer_ref: str | None,
    liveness: Mapping[str, bool | None],
) -> tuple[WriterLease, WriterBudget | None]:
    subject = snapshot.subject
    operation = _operation(snapshot)
    stage = next(
        (
            row
            for row in snapshot.all("integration_repair_stages")
            if operation
            and row["operation_id"] == operation["id"]
            and row["ordinal"] == operation["active_stage"]
        ),
        None,
    )
    verifier_id = (
        operation.get("verifier_task_id")
        if operation and subject.kind is SubjectKind.PARENT_EPISODE
        else None
    )
    task_id = subject.writer.task_id or verifier_id or (stage.get("repair_task_id") if stage else None)
    if task_id is None and subject.kind is SubjectKind.SOURCE:
        source_ci = next(
            (
                row
                for row in snapshot.all("integration_source_ci")
                if row["task_id"] == subject.task_id
                and row["source_head"] == subject.head_sha
                and row["generation"] == subject.generation
            ),
            None,
        )
        task_id = source_ci.get("repair_task_id") if source_ci else None
    budget = subject.budget
    if budget is None and stage and stage.get("started_at") is not None:
        data = (stage.get("dossier") or {}).get("budget", {})
        if stage.get("intelligence_class") and stage.get("deadline_at") is not None:
            budget = WriterBudget(
                ordinal=stage["ordinal"],
                intelligence_class=stage["intelligence_class"],
                started_at=stage["started_at"],
                deadline_at=stage["deadline_at"],
                attempts=stage["attempts"],
                attempt_limit=data.get("attempt_limit"),
            )
    if task_id is None:
        return WriterLease(), budget
    owner = _latest(
        row
        for row in snapshot.all("integration_branch_owners")
        if row["owner_id"] == task_id and _ref(row["ref"]) == writer_ref
    )
    sessions = [row for row in snapshot.all("sessions") if row.get("task_id") == task_id]
    attached = [row for row in sessions if row.get("state") != "stopped"]
    live = _latest(
        (row for row in attached if liveness.get(row["id"]) is True),
        "started_at",
    )
    session_id = live["id"] if live else subject.writer.session_id
    fence = owner["fence_token"] if owner else subject.writer.fence_token
    claimed = live.get("started_at") if live else subject.writer.claimed_at
    pushes = [
        row
        for row in snapshot.all("integration_promotion_intents")
        if row.get("resolution_task_id") == task_id and row.get("resolution_push_evidence")
    ]
    pushes += [
        row
        for row in snapshot.all("integration_candidate_resolutions")
        if row.get("repair_task_id") == task_id and row.get("push_evidence")
    ]
    push_times = []
    for row in pushes:
        receipt = row.get("resolution_push_evidence") or row.get("push_evidence") or {}
        if receipt.get("pushed_at") is not None:
            push_times.append(receipt["pushed_at"])
    last_push = max(push_times + [subject.writer.last_push_at or 0]) or None
    audit = _latest(
        row
        for row in snapshot.all("integration_owner_recoveries")
        if row.get("task_id") == task_id
        and _ref(row["ref"]) == writer_ref
        and row["outcome"] in {"released", "preserved_and_released"}
        and (owner is None or row["owner_row_id"] == owner["id"])
    )
    workspaces = [
        row for row in snapshot.all("workspaces") if row.get("locked_by_task_id") == task_id
    ]
    proof = (audit.get("evidence") if audit else None) or subject.writer.stop_proof
    proof_at = (
        audit["created_at"]
        if audit
        else ((proof or {}).get("stop_proof") or {}).get("confirmed_at", 0)
    )
    latest_start = max((row.get("started_at") or 0 for row in sessions), default=0)
    if live:
        status, proof = (
            (WriterStatus.WORKING if last_push or pushes else WriterStatus.CLAIMED),
            None,
        )
    elif attached and any(liveness.get(row["id"]) is not False for row in attached):
        status = WriterStatus.UNKNOWN
        unknown.append("writer_liveness_unknown:" + task_id)
    elif (
        proof
        and proof.get("stop_proof")
        and not workspaces
        and (owner is None or owner["handoff_state"] == "released")
        and (audit is None or owner is None or audit["created_at"] >= owner["updated_at"])
        and proof_at >= latest_start
        and proof_at >= (last_push or 0)
    ):
        status = WriterStatus.STOPPED
    elif (
        not sessions
        and not subject.writer.session_id
        and not workspaces
        and (owner is None or owner["handoff_state"] in {"reserved", "released"})
        and any(
            row["id"] == task_id and row["status"] in {"DEFINED", "READY", "PAUSED"}
            for row in snapshot.all("tasks")
        )
    ):
        status = WriterStatus.FILED
    else:
        status = WriterStatus.UNKNOWN
        unknown.append("writer_stop_unproven:" + task_id)
    return WriterLease(
        status=status,
        task_id=task_id,
        session_id=session_id,
        fence_token=fence,
        claimed_at=claimed,
        last_push_at=last_push,
        stop_proof=proof,
    ), budget


def _writes(snapshot: ObservationRows) -> tuple[UnresolvedWrite, ...]:
    writes = []
    for row in snapshot.all("integration_promotion_intents"):
        if row["state"] in {"committed", "conflict", "superseded"}:
            continue
        writes.append(
            UnresolvedWrite(
                journal_id=row["id"],
                kind="promotion",
                target_ref=_ref(row["target_branch"]),
                expected_old_sha=row["expected_target"],
                desired_sha=row.get("prepared_sha"),
                state=row["state"],
                started_at=row.get("created_at"),
            )
        )
    for row in snapshot.all("integration_candidate_ref_mutations"):
        if row["state"] == "reserved":
            writes.append(
                UnresolvedWrite(
                    journal_id=row["id"],
                    kind=row["purpose"],
                    target_ref=_ref(row["target_branch"]),
                    expected_old_sha=row.get("expected_old_sha"),
                    desired_sha=row["desired_sha"],
                    state=row["state"],
                    started_at=row.get("prewrite_at"),
                )
            )
    # The foundation journal is append-only. A later entry for the same write
    # supersedes the earlier observation, never the stored audit itself.
    journal = {}
    for row in sorted(snapshot.all("integration_subject_journal"), key=lambda row: row["seq"]):
        if row["primitive"] != Primitive.GIT_PUBLISH or row["mode"] != "active":
            continue
        payload = row.get("payload") or {}
        if payload.get("target_ref"):
            journal[payload.get("journal_id", row["idempotency_key"])] = row
    for key, row in journal.items():
        if row.get("outcome") in {"published", "target_moved"}:
            continue
        payload = row["payload"]
        writes.append(
            UnresolvedWrite(
                journal_id=str(key),
                kind="git_publish",
                target_ref=_ref(payload["target_ref"]),
                expected_old_sha=payload.get("expected_old_sha"),
                desired_sha=payload.get("desired_sha") or row.get("head_sha"),
                state=row.get("outcome") or "journalled",
                started_at=row["recorded_at"],
            )
        )
    return tuple(sorted(writes, key=lambda write: (write.started_at or 0, write.journal_id)))


def _conflict_files(diagnostics: Row) -> tuple[str, ...]:
    files = set(diagnostics.get("files", diagnostics.get("paths", ())))
    for line in str(diagnostics.get("detail", "")).splitlines():
        if "\t" in line:
            files.add(line.rsplit("\t", 1)[1].strip())
    return tuple(sorted(path for path in files if path))


def _publisher_fence(snapshot: ObservationRows, ref: str, now: float) -> Fence | None:
    """Read the publish target's authority, never remap another ref's fence.

    A publisher owner may serve multiple subjects on its repository. A
    collector owner must belong to this subject's operation/batch. Repair and
    worker leases, unresolved handoffs and expired reservations are excluded.
    The executing adapter still rechecks the fence under its write lock.
    """
    subject = snapshot.subject
    operation = _operation(snapshot)
    collector_ids = {subject.id, subject.batch_id, operation["id"] if operation else None}
    owners = [
        row
        for row in snapshot.all("integration_branch_owners")
        if row["repository_id"] == subject.repository_id
        and _ref(row["ref"]) == ref
        and row["handoff_state"] == "reserved"
        and not row.get("session_id")
        and not row.get("workspace_id")
        and (row.get("expires_at") is None or row["expires_at"] > now)
        and row.get("owner_id")
        and row.get("fence_token", -1) >= 0
        and (
            row["owner_role"] == "publisher"
            or row["owner_role"] == "collector"
            and row["owner_id"] in collector_ids
        )
    ]
    if len(owners) != 1:
        return None
    owner = owners[0]
    return Fence(
        target=BranchKey(repository_id=subject.repository_id, branch=ref),
        owner_id=owner["owner_id"],
        token=owner["fence_token"],
    )


class IntegrationObserver:
    """Collect facts or implement the foundation's observe primitive port.

    ``session_probe`` is a read-only runtime liveness probe. Without it, a
    stored running/sleeping/quarantined state cannot establish a live writer.
    A dead process still needs a durable stop-and-preservation receipt.

    ``facts_type`` accepts a shared-compatible SubjectFacts extension, such
    as the policy owner's IntegrationPolicyFacts. If that type declares
    ``publisher_fence``, the observer fills it from the real publish-target
    owner. It has no dependency on the policy evaluator's module.
    """

    def __init__(
        self,
        reader: SubjectReadPort,
        git: GitReadPort | None = None,
        *,
        clock: Callable[[], float] = time.time,
        session_probe: SessionProbe | None = None,
        facts_type: type[SubjectFacts] = SubjectFacts,
        candidate_ci: Callable[[ObservationRows, HeadIdentity], Awaitable[CIEvidence]] | None = None,
    ) -> None:
        if not issubclass(facts_type, SubjectFacts):
            raise TypeError("facts_type must extend SubjectFacts")
        self.reader, self.git, self.clock = reader, git, clock
        self.session_probe = session_probe
        self.facts_type = facts_type
        self.candidate_ci = candidate_ci
        self._ancestry: dict[tuple[str, str, str], bool] = {}

    async def _remote_heads(self, repository: Row, refs: list[str]) -> list[RemoteHead]:
        """Every ref's head, in order; a failed or mismatched answer is unknown."""
        batched = getattr(self.git, "remote_heads", None)
        if batched is not None:
            try:
                answered = await batched(repository, refs)
                if not isinstance(answered, Mapping):
                    heads = list(answered)
                    if [head.ref for head in heads] != refs:
                        raise ValueError("remote answered different refs")
                    answered = dict(zip(refs, heads))
            except Exception:
                logger.debug("Subject remote heads unavailable", exc_info=True)
                answered = {}
        else:
            gate = asyncio.Semaphore(_SINGLE_REMOTE_READS)

            async def one(ref: str) -> RemoteHead | None:
                async with gate:
                    try:
                        return await self.git.remote_head(repository, ref)
                    except Exception:
                        logger.debug("Subject remote head unavailable", exc_info=True)
                        return None

            answered = dict(zip(refs, await asyncio.gather(*(one(ref) for ref in refs))))
        heads = []
        for ref in refs:
            remote = answered.get(ref)
            if remote is None or remote.ref != ref:
                remote = RemoteHead(ref=ref, state="unknown")
            heads.append(remote)
        return heads

    async def _is_ancestor(self, repository: Row, ancestor: str, descendant: str) -> bool | None:
        key = (repository["id"], ancestor, descendant)
        if key in self._ancestry:
            return self._ancestry[key]
        answer = await self.git.is_ancestor(repository, ancestor, descendant)
        if answer is not None and _OID.fullmatch(ancestor) and _OID.fullmatch(descendant):
            if len(self._ancestry) >= _ANCESTRY_CACHE:
                del self._ancestry[next(iter(self._ancestry))]
            self._ancestry[key] = answer
        return answer

    async def observe(self, subject: Subject) -> SubjectFacts:
        """The reconciler callable: current facts for its supplied subject.

        The loop owns timeout and id/version validation. A disappeared row
        raises so its existing isolated observation-failure path can retry.
        """
        facts = await self.observe_subject(subject.id)
        if facts is None:
            raise LookupError("integration subject not found: " + subject.id)
        return facts

    async def __call__(self, subject: Subject, args: ObserveSubjectArgs, /) -> PrimitiveOutcome:
        try:
            facts = await self.observe_subject(subject.id, include_remote=args.include_remote)
        except Exception:
            logger.debug("Subject snapshot unavailable", exc_info=True)
            return PrimitiveOutcome.unknown(Primitive.OBSERVE_SUBJECT, "snapshot_unavailable")
        if facts is None:
            return PrimitiveOutcome(primitive=Primitive.OBSERVE_SUBJECT, outcome="not_found")
        return PrimitiveOutcome(
            primitive=Primitive.OBSERVE_SUBJECT,
            outcome="observed",
            detail={"facts": facts.model_dump(mode="json")},
        )

    async def observe_subject(
        self, subject_id: str, *, include_remote: bool = True
    ) -> SubjectFacts | None:
        snapshot = await self.reader.read(subject_id)
        if snapshot is None:
            return None
        subject, now = snapshot.subject, self.clock()
        unknown: list[str] = []
        members, member_refs = _members(snapshot)
        unknown.extend(
            "member_head_missing:" + member.task_id for member in members if member.head_sha is None
        )
        candidate_row = next(
            (
                row
                for row in snapshot.all("integration_candidate_revisions")
                if row["revision"] == subject.generation
            ),
            None,
        )
        batch = next(iter(snapshot.all("integration_batches")), {})
        candidate = None
        if candidate_row and candidate_row.get("head_sha") and batch.get("integration_branch"):
            candidate = HeadIdentity(
                repository_id=subject.repository_id,
                ref=_ref(batch["integration_branch"]),
                sha=candidate_row["head_sha"],
                generation=candidate_row["revision"],
                base_sha=candidate_row["construction_base_sha"],
            )
        if subject.batch_id and batch.get("current_revision") != subject.generation:
            unknown.append("subject_generation_moved")
        default_ref = _ref(snapshot.repository["default_branch"])
        refs = {default_ref, *member_refs.values()}
        if subject.target_ref:
            refs.add(subject.target_ref)
        if candidate:
            refs.add(candidate.ref)
        if not include_remote or self.git is None:
            remote_heads = [RemoteHead(ref=ref, state="unknown") for ref in sorted(refs)]
        else:
            remote_heads = await self._remote_heads(snapshot.repository, sorted(refs))
        unknown.extend(
            "remote_unknown:" + head.ref for head in remote_heads if head.state == "unknown"
        )
        default = next(head.sha for head in remote_heads if head.ref == default_ref)
        target = candidate or subject.head
        ci = []
        if target:
            if candidate and self.candidate_ci and subject.engine.value == "reconciler":
                try:
                    evidence = await self.candidate_ci(snapshot, target)
                    if evidence.head_sha != target.sha:
                        raise ValueError("CI answered a different candidate SHA")
                    # Legacy repair attempt accounting still names durable CI
                    # rows. Retain that ID only if live checks agree; it never
                    # supplies or overrides the current state.
                    recorded = _ci(snapshot, target, subject.task_id, now, candidate=True)
                    if recorded.state is evidence.state and recorded.evidence_id:
                        evidence = evidence.model_copy(update={"evidence_id": recorded.evidence_id})
                except Exception:
                    logger.warning("Candidate CI observation unavailable for %s", target.sha,
                                   exc_info=True)
                    evidence = CIEvidence(head_sha=target.sha, state=CIState.INFRA,
                                          observed_at=now)
                ci.append(evidence)
            else:
                ci.append(_ci(snapshot, target, subject.task_id, now,
                              candidate=candidate is not None))
        # Before construction there is no candidate: members relate to the
        # publication target's observed tip (main for a root batch), not to
        # an absent head. An unread tip still leaves ancestry unknown.
        relative_to = (
            target.sha
            if target
            else next(
                (
                    head.sha
                    for head in remote_heads
                    if head.ref == (subject.target_ref or default_ref)
                ),
                None,
            )
        )
        gate = asyncio.Semaphore(_ANCESTRY_READS)

        async def ancestry_of(member: MemberFacts) -> str:
            if not (include_remote and self.git and relative_to and member.head_sha):
                return "unknown"
            async with gate:
                try:
                    contained = await self._is_ancestor(
                        snapshot.repository, member.head_sha, relative_to
                    )
                    if contained is not False:
                        return "contained" if contained else "unknown"
                    ahead = await self._is_ancestor(
                        snapshot.repository, relative_to, member.head_sha
                    )
                except Exception:
                    logger.debug("Subject ancestry unavailable", exc_info=True)
                    return "unknown"
            return {True: "ahead", False: "diverged"}.get(ahead, "unknown")

        ancestries = await asyncio.gather(*(ancestry_of(member) for member in members))
        observed_members = []
        for member, ancestry in zip(members, ancestries):
            state = CIState.NONE
            if member.head_sha:
                identity = HeadIdentity(
                    repository_id=subject.repository_id,
                    ref=member_refs.get(member.task_id, default_ref),
                    sha=member.head_sha,
                    generation=member.generation,
                    base_sha=member.base_sha,
                )
                evidence = _ci(snapshot, identity, member.task_id, now)
                state = evidence.state
                if not any(item.head_sha == evidence.head_sha for item in ci):
                    ci.append(evidence)
            if ancestry == "unknown" and member.head_sha:
                unknown.append("ancestry_unknown:" + member.task_id)
            observed_members.append(member.model_copy(update={"ci": state, "ancestry": ancestry}))
        # An unsealed root's frontier is admission input: a paused or rejected
        # source stays out of the seal and keeps its branch, while the rest of
        # the train advances. Sealed members and a subject's own task bind it.
        bound = [] if subject.kind is SubjectKind.ROOT_BATCH and not subject.batch_id else members
        holds = _task_holds(snapshot, {member.task_id for member in bound} | {subject.task_id})
        holds += [
            HoldFacts(
                kind="review_rejected", task_id=member.task_id, reason="exact_head_review_rejected"
            )
            for member in bound
            if member.review == "rejected"
        ]
        gate = None
        if subject.schedule.gate_id:
            row = next(
                (row for row in snapshot.all("gates") if row["id"] == subject.schedule.gate_id),
                None,
            )
            gate = (
                GateFacts(gate_id=subject.schedule.gate_id, status="missing")
                if row is None
                else (
                    GateFacts(
                        gate_id=row["id"],
                        status={"resolved": "answered"}.get(row["status"], row["status"]),
                        question=row["question"],
                        answer=row.get("resolution"),
                        timeout_at=row.get("timeout_at"),
                        no_default=row.get("timeout_at") is None,
                    )
                )
            )
            if gate.status == "missing":
                unknown.append("gate_missing:" + gate.gate_id)
            if gate.status == "open" or gate.answer in {"hold", "reject", "abort"}:
                holds.append(HoldFacts(kind="operator_hold", reason="gate:" + gate.gate_id))
            config = _latest(
                (
                    row
                    for row in snapshot.all("integration_subject_journal")
                    if row["primitive"] == Primitive.GATE
                    and (row.get("payload") or {}).get("gate_id") == gate.gate_id
                ),
                "seq",
            )
            if config:
                payload = config["payload"]
                gate = gate.model_copy(
                    update={
                        key: payload[key]
                        for key in ("choices", "default_choice", "no_default")
                        if key in payload
                    }
                )
        conflicts = []
        by_ordinal = {
            row["ordinal"]: row["task_id"] for row in snapshot.all("integration_batch_members")
        }
        for row in snapshot.all("integration_candidate_member_results"):
            if row["revision"] == subject.generation and row["result"] == "conflict":
                diagnostics = row.get("conflict_evidence") or {}
                conflicts.append(
                    ConflictFacts(
                        member_task_id=by_ordinal[row["member_ordinal"]],
                        files=_conflict_files(diagnostics),
                    )
                )
        for row in snapshot.all("integration_promotion_intents"):
            if row["state"] == "conflict" and row.get("source_task_id"):
                conflicts.append(
                    ConflictFacts(
                        member_task_id=row["source_task_id"],
                        files=_conflict_files(row.get("conflict_diagnostics") or {}),
                    )
                )
        for member in members:
            if any(
                row["task_id"] == member.task_id
                and row["repository_id"] == subject.repository_id
                and row["source_head"] == member.head_sha
                and row["source_base"] == member.base_sha
                and row["generation"] == member.generation
                and row["state"] == "conflict"
                for row in snapshot.all("integration_source_ci")
            ):
                conflicts.append(ConflictFacts(member_task_id=member.task_id))
        liveness = {}
        for session in snapshot.all("sessions"):
            if session.get("state") == "stopped":
                continue
            try:
                liveness[session["id"]] = (
                    await self.session_probe(session) if self.session_probe else None
                )
            except Exception:
                logger.debug("Subject writer liveness unavailable", exc_info=True)
                liveness[session["id"]] = None
        writer, budget = _writer(
            snapshot, unknown, target.ref if target else subject.target_ref, liveness
        )
        operation = _operation(snapshot)
        stages = [
            row
            for row in snapshot.all("integration_repair_stages")
            if operation and row["operation_id"] == operation["id"]
        ]
        if writer.status is WriterStatus.CLAIMED and target and self.git and include_remote:
            owner = next(
                (
                    row
                    for row in snapshot.all("integration_branch_owners")
                    if row["owner_id"] == writer.task_id
                    and _ref(row["ref"]) == target.ref
                    and row.get("session_id") == writer.session_id
                    and row["handoff_state"] == "attached"
                ),
                None,
            )
            stage = next(
                (
                    row
                    for row in stages
                    if row["repair_task_id"] == writer.task_id
                    and row["ordinal"] == operation["active_stage"]
                ),
                None,
            )
            published = next((row.sha for row in remote_heads if row.ref == target.ref), None)
            # A batch builder can publish a partial candidate after the stage
            # starts, before its repair writer claims. Only an advance beyond
            # the builder's last journalled write can be the writer's push.
            builder_write = _latest(
                row
                for row in snapshot.all("integration_candidate_ref_mutations")
                if row["batch_id"] == subject.batch_id
                and row["repository_id"] == subject.repository_id
                and row["revision"] == subject.generation
                and _ref(row["target_branch"]) == target.ref
                and row["purpose"] in {"candidate_partial", "candidate_final", "repair_handoff"}
                and (
                    row["state"] == "applied"
                    or row["state"] == "reserved" and row.get("prewrite_at") is not None
                )
            )
            starting_sha = (
                builder_write["desired_sha"]
                if builder_write
                else stage["starting_sha"] if stage else None
            )
            if owner and stage and published and starting_sha and published != starting_sha:
                try:
                    advanced = await self._is_ancestor(
                        snapshot.repository, starting_sha, published
                    )
                except Exception:
                    logger.debug("Subject writer push ancestry unavailable", exc_info=True)
                    advanced = None
                if advanced is True:
                    # The fenced live writer owns the ref and has advanced it
                    # beyond construction. The push timestamp remains unknown.
                    writer = writer.model_copy(update={"status": WriterStatus.WORKING})
                elif advanced is None:
                    unknown.append("writer_push_ancestry_unknown:" + writer.task_id)
        no_progress = False
        for stage in stages:
            incident = (stage.get("dossier") or {}).get("supervisor_recovery") or {}
            identity = incident.get("subject") or {}
            incident_head = identity.get("candidate_sha") or identity.get("head_sha")
            if (
                target
                and incident_head == target.sha
                and incident.get("attempts") == (budget.attempts if budget else stage["attempts"])
            ):
                no_progress = True
        # A ladder's length is policy. Only an explicitly recorded exhaustion
        # fact is reported; the observer never invents a new ordinal/limit.
        ladder_exhausted = any(
            stage["ordinal"] == operation["active_stage"]
            and (stage.get("dossier") or {}).get("ladder_exhausted") is True
            for stage in stages
        )
        base = target.base_sha if target else subject.base_sha
        extra = {}
        if "publisher_fence" in self.facts_type.model_fields:
            publish_ref = (
                default_ref if subject.kind is SubjectKind.ROOT_BATCH else subject.target_ref
            )
            extra["publisher_fence"] = (
                _publisher_fence(snapshot, publish_ref, now) if publish_ref else None
            )
            if extra["publisher_fence"] is None:
                unknown.append("publisher_fence_unavailable")
        return self.facts_type(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=now,
            head=subject.head,
            candidate=candidate,
            default_branch_head=default,
            remote_heads=tuple(remote_heads),
            members=tuple(observed_members),
            ci=tuple(ci),
            conflicts=tuple(conflicts),
            writer=writer,
            budget=budget,
            unresolved_writes=_writes(snapshot),
            gate=gate,
            holds=tuple(
                sorted(holds, key=lambda hold: (hold.kind, hold.task_id or "", hold.reason))
            ),
            base_moved=default is not None and base is not None and default != base,
            wait_overdue=subject.wait_overdue(now),
            no_progress=no_progress,
            ladder_exhausted=ladder_exhausted,
            unknown=tuple(sorted(set(unknown))),
            **extra,
        )


class ParentReadiness:
    """Shared receipt and trusted-verification invariants for parent subjects."""

    def __init__(
        self, db, *, git_manager=None, clock: Callable[[], float] = time.time
    ) -> None:
        self.db = db
        self.git_manager = git_manager
        self.clock = clock


    async def readiness(self, task_id: str) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            return await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )


    async def readiness_on(
        self,
        conn,
        *,
        parent: dict[str, Any],
        project: dict[str, Any],
        checkpoint: dict[str, Any],
        operation: dict[str, Any],
        additional_extension: dict[str, Any] | None = None,
        receipt_covered_extensions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        episode = (
            await conn.execute(
                select(integration_parent_episodes).where(
                    integration_parent_episodes.c.id == checkpoint["episode_id"]
                )
            )
        ).mappings().one_or_none()
        if episode is None or episode["parent_task_id"] != parent["id"]:
            raise HierarchyError("invariant_error", "parent episode is missing")

        child_rows = (
            await conn.execute(
                select(
                    tasks.c.id,
                    tasks.c.status,
                    tasks.c.pr_url,
                    task_integration_checkpoints.c.checkpoint_sha.label("source_head"),
                )
                .outerjoin(
                    task_integration_checkpoints,
                    task_integration_checkpoints.c.task_id == tasks.c.id,
                )
                .where(tasks.c.parent_task_id == parent["id"])
                .order_by(tasks.c.id)
            )
        ).mappings().all()
        origins = {
            row["task_id"]: dict(row)
            for row in (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id.in_([row["id"] for row in child_rows]),
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings().all()
        } if child_rows else {}
        receipts = [
            dict(row)
            for row in (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.target_task_id == parent["id"],
                        task_delivery_receipts.c.repository_id == checkpoint["repository_id"],
                        task_delivery_receipts.c.target_branch == checkpoint["branch"],
                    )
                    .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
                )
            ).mappings().all()
        ]
        rework_cutoffs = {}
        if child_rows:
            markers = (
                await conn.execute(
                    select(task_metadata.c.task_id, task_metadata.c.value).where(
                        task_metadata.c.task_id.in_([row["id"] for row in child_rows]),
                        task_metadata.c.key == INTEGRATION_REWORK_AT_KEY,
                    )
                )
            ).all()
            rework_cutoffs = {task_id: float(value) for task_id, value in markers}
        dispositions = {
            row["child_task_id"]: dict(row)
            for row in (
                await conn.execute(
                    select(integration_child_dispositions).where(
                        integration_child_dispositions.c.parent_task_id == parent["id"]
                    )
                )
            ).mappings().all()
        }

        blockers: list[dict[str, str]] = []
        selected: list[dict[str, Any]] = []
        terminal = {"COMPLETED", "FAILED"}
        by_child: dict[str, list[dict[str, Any]]] = {}
        carried_receipt_ids = set(
            (
                await conn.execute(
                    select(integration_episode_receipt_acceptances.c.receipt_id)
                    .select_from(
                        integration_episode_receipt_acceptances.join(
                            integration_parent_verifications,
                            integration_parent_verifications.c.id
                            == integration_episode_receipt_acceptances.c.previous_verification_id,
                        ).join(
                            integration_repair_operations,
                            integration_repair_operations.c.id
                            == integration_episode_receipt_acceptances.c.previous_operation_id,
                        ).join(
                            integration_parent_operation_completions,
                            integration_parent_operation_completions.c.operation_id
                            == integration_episode_receipt_acceptances.c.previous_operation_id,
                        )
                    )
                    .where(
                        integration_episode_receipt_acceptances.c.episode_id
                        == checkpoint["episode_id"],
                        integration_episode_receipt_acceptances.c.operation_id
                        == operation["id"],
                        integration_episode_receipt_acceptances.c.ancestry_to_sha
                        == episode["pre_collection_checkpoint_sha"],
                        integration_parent_verifications.c.operation_id
                        == integration_episode_receipt_acceptances.c.previous_operation_id,
                        integration_parent_verifications.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_parent_verifications.c.parent_task_id == parent["id"],
                        integration_parent_verifications.c.head_sha
                        == integration_episode_receipt_acceptances.c.ancestry_from_sha,
                        integration_parent_operation_completions.c.verification_id
                        == integration_episode_receipt_acceptances.c.previous_verification_id,
                        integration_parent_operation_completions.c.parent_task_id
                        == parent["id"],
                        integration_parent_operation_completions.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_repair_operations.c.state == "completed",
                    )
                )
            ).scalars().all()
        )
        for receipt in receipts:
            directly_bound = (
                receipt["parent_operation_id"] == operation["id"]
                and receipt["parent_episode_id"] == checkpoint["episode_id"]
            )
            if receipt["source_task_id"] and (
                directly_bound or receipt["id"] in carried_receipt_ids
            ):
                by_child.setdefault(receipt["source_task_id"], []).append(receipt)
        for child in child_rows:
            child_id = child["id"]
            origin = origins.get(child_id)
            if (
                origin is None
                or origin["parent_task_id"] != parent["id"]
                or origin["parent_repository_id"] != checkpoint["repository_id"]
                or origin["parent_ref"] != checkpoint["branch"]
            ):
                blockers.append({"task_id": child_id, "reason": "origin_mismatch"})
                continue
            candidates = by_child.get(child_id, [])
            code = [
                row
                for row in candidates
                if row["disposition"] == "code"
                and row["reviewed_head_sha"] == child["source_head"]
                and row["created_at"] >= rework_cutoffs.get(child_id, 0)
            ]
            if len(code) == 1 and child["status"] == "COMPLETED":
                selected.append(code[0])
                continue
            current = dispositions.get(child_id)
            disposed = [
                row
                for row in candidates
                if row["disposition"] in {"noop", "ineligible", "skipped"}
                and current is not None
                and row["disposition"] == current["disposition"]
                and row["disposition_revision"] == current["revision"]
                and row["reviewed_head_sha"] == child["source_head"]
                and row["resolution_evidence"]
                and (row["disposition"] != "noop" or row["verification_evidence"])
            ]
            if len(disposed) == 1 and child["status"] in terminal:
                selected.append(disposed[0])
                continue
            blockers.append(
                {
                    "task_id": child_id,
                    "reason": "failed_child" if child["status"] == "FAILED" else "receipt_missing",
                }
            )

        code_chain = sorted(
            (
                row
                for row in selected
                if row["disposition"] == "code"
                and row["parent_operation_id"] == operation["id"]
                and row["parent_episode_id"] == checkpoint["episode_id"]
            ),
            key=lambda row: (row["created_at"], row["id"]),
        )
        head_sha = episode["pre_collection_checkpoint_sha"]
        from src.integration.parent_repair_heads import extensions_on

        try:
            extensions = await extensions_on(conn, operation, checkpoint)
        except ValueError:
            extensions = []
            blockers.append({"task_id": parent["id"], "reason": "repair_head_proof"})
        if receipt_covered_extensions:
            # Operator recovery proves these duplicate records against their
            # receipts and Git before persisting the coverage on apply.
            extensions = [edge for edge in extensions if edge not in receipt_covered_extensions]
        if additional_extension is not None:
            # Recovery previews a strictly audited edge before committing it.
            # Ordinary readiness reads only the durable stage dossiers.
            extensions.append(additional_extension)

        def extend_head(head):
            while True:
                matching = [edge for edge in extensions if edge["before_sha"] == head]
                if not matching:
                    return head
                if len(matching) != 1:
                    blockers.append({"task_id": parent["id"], "reason": "repair_head_chain"})
                    return head
                edge = matching[0]
                extensions.remove(edge)
                head = edge["after_sha"]

        for row in code_chain:
            if row["before_sha"] != head_sha:
                head_sha = extend_head(head_sha)
            if row["before_sha"] != head_sha or not self._trusted_code_receipt(row):
                blockers.append({"task_id": row["source_task_id"], "reason": "receipt_chain"})
                break
            head_sha = row["after_sha"]
        head_sha = extend_head(head_sha)
        if extensions:
            blockers.append({"task_id": parent["id"], "reason": "repair_head_chain"})
        from src.integration.failed_verification_recovery import FAILED_AGGREGATE_META_KEY

        failed_aggregate = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == parent["id"],
            task_metadata.c.key == FAILED_AGGREGATE_META_KEY,
        ))
        if failed_aggregate is not None:
            import json

            failed_aggregate = json.loads(failed_aggregate)
            if (failed_aggregate["operation_id"] == operation["id"]
                    and failed_aggregate["episode_id"] == checkpoint["episode_id"]
                    and failed_aggregate["head_sha"] == head_sha):
                blockers.append({"task_id": parent["id"], "reason": "failed_aggregate_head_unchanged"})
        outcome = "ready" if not blockers else (
            "failed" if any(row["reason"] == "failed_child" for row in blockers) else "waiting"
        )
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        return {
            "outcome": outcome,
            "task_id": parent["id"],
            "episode_id": checkpoint["episode_id"],
            "operation_id": operation["id"],
            "generation": int(checkpoint["generation"]),
            "checkpoint_sha": episode["pre_collection_checkpoint_sha"],
            "head_sha": head_sha,
            "receipts": sorted(selected, key=lambda row: row["source_task_id"]),
            "blockers": blockers,
            "required_checks": policy.parent.required_checks.model_dump(mode="json"),
            "on_failed_child": policy.on_failed_child,
        }


    @staticmethod
    def _trusted_code_receipt(receipt: dict[str, Any]) -> bool:
        """Accept clean squash edges or the exact conflict-resolution proof shape."""
        if receipt["squash_sha"] is not None:
            return bool(
                receipt["after_sha"] == receipt["squash_sha"]
                and receipt["resolution_evidence"] is None
            )
        evidence = receipt["resolution_evidence"]
        if not isinstance(evidence, dict) or evidence.get("kind") != "conflict_resolution":
            return False
        review_snapshot = receipt["review_evidence"]
        review = review_snapshot.get("review") if isinstance(review_snapshot, dict) else None
        authoring = evidence.get("authoring")
        fence = authoring.get("fence") if isinstance(authoring, dict) else None
        proof = evidence.get("remote_proof")
        commits = evidence.get("repair_commit_shas")
        if (
            not isinstance(review, dict)
            or not isinstance(authoring, dict)
            or not isinstance(fence, dict)
            or not isinstance(proof, dict)
            or not isinstance(commits, list)
            or not commits
            or len(set(commits)) != len(commits)
            or any(not isinstance(oid, str) or not _OID.fullmatch(oid) for oid in commits)
        ):
            return False
        required_strings = (
            authoring.get("repair_task_id"),
            authoring.get("repair_session_id"),
            authoring.get("repair_session_instance_token"),
            authoring.get("repair_workspace_id"),
        )
        return bool(
            evidence.get("original_source_base") == review.get("source_base")
            and evidence.get("original_source_head") == receipt["reviewed_head_sha"]
            and evidence.get("original_source_tree") == receipt["reviewed_tree_sha"]
            and evidence.get("original_expected_target") == receipt["before_sha"]
            and evidence.get("resolved_head_sha") == receipt["after_sha"]
            and isinstance(evidence.get("resolved_tree_sha"), str)
            and _OID.fullmatch(evidence["resolved_tree_sha"])
            and commits[-1] == receipt["after_sha"]
            and authoring.get("operation_id") == receipt["parent_operation_id"]
            and isinstance(authoring.get("stage_ordinal"), int)
            and not isinstance(authoring.get("stage_ordinal"), bool)
            and authoring["stage_ordinal"] >= 0
            and all(isinstance(value, str) and value for value in required_strings)
            and fence.get("repository_id") == receipt["repository_id"]
            and fence.get("branch") == receipt["target_branch"]
            and fence.get("owner_id") == authoring.get("repair_task_id")
            and isinstance(fence.get("token"), int)
            and not isinstance(fence.get("token"), bool)
            and fence["token"] >= 0
            and proof
            == {
                "kind": "exact_resolution_tip",
                "remote_sha": receipt["after_sha"],
                "resolved_tree_sha": evidence["resolved_tree_sha"],
                "repair_commit_shas": commits,
            }
        )


    async def _locked_context_on(self, conn, task_id: str):
        parent = (
            await conn.execute(select(tasks).where(tasks.c.id == task_id))
        ).mappings().one_or_none()
        archived_parent = False
        if parent is None:
            # A verifier can replay its close after the completed parent has
            # legitimately archived.  Preserve the historical identity just
            # long enough to confirm that exact completed operation below;
            # no active hierarchy mutation may proceed against this row.
            parent = (
                await conn.execute(select(archived_tasks).where(archived_tasks.c.id == task_id))
            ).mappings().one_or_none()
            archived_parent = parent is not None
        if parent is None:
            raise HierarchyError("invariant_error", "parent task does not exist")
        project = (
            await conn.execute(select(projects).where(projects.c.id == parent["project_id"]))
        ).mappings().one()
        if project["hierarchical_integration_mode"] not in {"hierarchy", "train"}:
            raise HierarchyError("invariant_error", "hierarchical integration is disabled")
        await self.db.lock_hierarchy_project(conn, project["id"])
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if checkpoint is None or checkpoint["episode_id"] is None:
            raise HierarchyError("invariant_error", "parent has no active integration episode")
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(
                    integration_repair_operations.c.parent_task_id == task_id,
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if operation is None:
            raise HierarchyError("invariant_error", "parent episode operation is missing")
        if archived_parent and operation["state"] != "completed":
            raise HierarchyError("invariant_error", "archived parent has a live integration operation")
        result_parent = dict(parent)
        result_parent["_archived"] = archived_parent
        return result_parent, dict(project), dict(checkpoint), dict(operation)
